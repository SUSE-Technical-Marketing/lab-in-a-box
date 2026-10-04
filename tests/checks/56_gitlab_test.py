#!/usr/bin/env python3
# Unit tests for scripts/install_gitlab.py — no real podman/kubectl available
# in this container. Verifies the podman-mode Omnibus container invocation
# (real image, port mapping, GITLAB_OMNIBUS_CONFIG assembly and its Ruby-DSL
# escaping, credential resolution) and the kubernetes-mode Helm invocation
# (chart values taken from charts.gitlab.io's values.yaml, the Let's Encrypt/public-domain die() guard). Run from
# 56_gitlab.sh, in its own container — see tests/run_tests.sh.
import io
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_gitlab as igl  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def omnibus_config(run_cmd):
    """
    Extracts the real, decoded GITLAB_OMNIBUS_CONFIG value from a full
    `podman run ...` command string — via shlex.split(), not a raw
    substring search, since shlex.quote() (correctly) re-escapes any
    embedded single quote in the Ruby-DSL value, so its shell-quoted form
    in the command never matches the plain, unescaped Ruby text.
    """
    tokens = shlex.split(run_cmd)
    for tok in tokens:
        if tok.startswith("GITLAB_OMNIBUS_CONFIG="):
            return tok[len("GITLAB_OMNIBUS_CONFIG="):]
    return None


# ── setup_gitlab_podman: Omnibus container invocation ───────────────────────
# The password file is written about 20s after container start, while GitLab is still running
# reconfigure (rails database migrations) and is not reachable over HTTP. Readiness therefore
# waits for an HTTP 200 from the login page, and the password file is read only after that.
# These tests mock both the readiness curl and the password file read, and fast-forward
# time.time() as a safety net against a real,
# unbounded-looking 300s busy-wait if a future change breaks that mock.
igl.time.sleep = lambda s: None
_fake_now = [0]
igl.time.time = lambda: _fake_now.__setitem__(0, _fake_now[0] + 100) or _fake_now[0]
ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if "curl -sk -o /dev/null" in cmd and "/users/sign_in" in cmd:
        return mock.Mock(returncode=0, stdout="200", stderr="")
    if "initial_root_password" in cmd:
        return mock.Mock(returncode=0, stdout="Password: generated-pw123\n", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


igl.ssh_run = _fake_ssh_run

ssh_calls.clear()
with mock.patch.object(igl.primary, "find_service_credential_for_kind", return_value=(None, [])):
    igl.setup_gitlab_podman("host1", {})
cmds = [c[1] for c in ssh_calls]
run_cmd = next(c for c in cmds if c.startswith("podman run"))
check("setup_gitlab_podman: uses the real upstream Omnibus CE image by default",
      "gitlab/gitlab-ce:latest" in run_cmd)
check("setup_gitlab_podman: publishes the real 443/80/22 container ports, ssh mapped off-22 by "
      "default (GitLab's own documented reasoning — 22 is almost always the host's own sshd)",
      "-p 443:443" in run_cmd and "-p 80:80" in run_cmd and "-p 2222:22" in run_cmd)
check("setup_gitlab_podman: mounts the real /etc/gitlab, /var/log/gitlab, /var/opt/gitlab paths "
      "(GitLab's own documented persistence volumes)",
      "/etc/gitlab" in run_cmd and "/var/log/gitlab" in run_cmd and "/var/opt/gitlab" in run_cmd)
check("setup_gitlab_podman: sets external_url via the real GITLAB_OMNIBUS_CONFIG mechanism, "
      "defaulting the hostname to the target host itself",
      "external_url 'http://host1:80'" in omnibus_config(run_cmd))
check("setup_gitlab_podman: creates a systemd unit so the container survives a reboot",
      any("gitlab.service" in c and "podman start -a gitlab" in c for c in cmds))

# A real root password reaches GITLAB_OMNIBUS_CONFIG's initial_root_password
# — only takes effect on first boot, a real documented GitLab limitation,
# not something this addon can work around, but it must still be SENT.
ssh_calls.clear()
with mock.patch.object(igl.primary, "find_service_credential_for_kind", return_value=(None, [])):
    igl.setup_gitlab_podman("host1", {"gitlab_root_password": "S3cr3t!"})
run_cmd = next(c[1] for c in ssh_calls if c[1].startswith("podman run"))
check("setup_gitlab_podman: a configured root password reaches initial_root_password",
      "gitlab_rails['initial_root_password'] = 'S3cr3t!'" in omnibus_config(run_cmd))

# Ruby-DSL escaping is real string escaping, not shell escaping — an
# embedded single quote must not break out of the Ruby string literal.
ssh_calls.clear()
with mock.patch.object(igl.primary, "find_service_credential_for_kind", return_value=(None, [])):
    igl.setup_gitlab_podman("host1", {"gitlab_root_password": "pass'word"})
run_cmd = next(c[1] for c in ssh_calls if c[1].startswith("podman run"))
check("setup_gitlab_podman: escapes an embedded single quote as a Ruby string, not shell syntax",
      "pass\\'word" in omnibus_config(run_cmd))

# Credential-store path: the "gitlab" kind is auto-discovered when
# gitlab_account is unset — same convention as install_ds389.py.
ssh_calls.clear()
with mock.patch.object(igl.primary, "find_service_credential_for_kind",
                        return_value=("my-gitlab-creds", ["my-gitlab-creds"])), \
     mock.patch.object(igl.primary, "load_service_credential",
                        return_value={"gitlab_root_password": "pw-from-store"}):
    igl.setup_gitlab_podman("host1", {})
run_cmd = next(c[1] for c in ssh_calls if c[1].startswith("podman run"))
check("setup_gitlab_podman: auto-discovers a single matching credential-store file",
      "gitlab_rails['initial_root_password'] = 'pw-from-store'" in omnibus_config(run_cmd))

# Custom hostname/ports/image/version are honored end to end.
ssh_calls.clear()
with mock.patch.object(igl.primary, "find_service_credential_for_kind", return_value=(None, [])):
    igl.setup_gitlab_podman("host1", {
        "gitlab_hostname": "git.mydemo.lab", "gitlab_https_port": "8443",
        "gitlab_http_port": "8080", "gitlab_ssh_port": "22022",
        "gitlab_image": "registry.example.com/gitlab-ce", "gitlab_version": "17.0.0-ce.0",
    })
run_cmd = next(c[1] for c in ssh_calls if c[1].startswith("podman run"))
check("setup_gitlab_podman: a custom hostname/ports/image/version reach the real container "
      "invocation",
      "--hostname git.mydemo.lab" in run_cmd and "-p 8443:443" in run_cmd
      and "-p 8080:80" in run_cmd and "-p 22022:22" in run_cmd
      and "registry.example.com/gitlab-ce:17.0.0-ce.0" in run_cmd
      and "external_url 'http://git.mydemo.lab:8080'" in omnibus_config(run_cmd))

# The password file appearing does NOT mean GitLab is ready. It is written ~20s into a
# reconfigure that keeps running for several more minutes. This must NOT be
# treated as ready when the real HTTP readiness check keeps failing: the
# password file must never even be read, and a clear warning must print
# (not a false "ready" claim).
ssh_calls.clear()


def _fake_never_ready(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if "curl -sk -o /dev/null" in cmd and "/users/sign_in" in cmd:
        return mock.Mock(returncode=0, stdout="503", stderr="")  # still reconfiguring
    if "initial_root_password" in cmd:
        return mock.Mock(returncode=0, stdout="Password: generated-pw123\n", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


igl.ssh_run = _fake_never_ready
out = io.StringIO()
with mock.patch.object(igl.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     redirect_stdout(out):
    igl.setup_gitlab_podman("host1", {})
printed = out.getvalue()
check("setup_gitlab_podman: does NOT treat an early password file as 'ready' when the real "
      "HTTP readiness check keeps failing",
      not any("initial_root_password" in c[1] for c in ssh_calls))
check("setup_gitlab_podman: prints a clear warning instead of falsely claiming success",
      "WARNING" in printed and "still not answering real HTTP requests" in printed)
check("setup_gitlab_podman: does NOT print an 'Initial root password:' line when it was never "
      "confirmed ready", "Initial root password:" not in printed)
igl.ssh_run = _fake_ssh_run


# ── setup_gitlab_kubernetes: real Helm chart invocation ─────────────────────
ssh_calls = []
igl.ssh_run = lambda hostname, cmd, **kw: ssh_calls.append((hostname, cmd, kw)) or mock.Mock(returncode=0)
igl.helm_repo_add = lambda hostname, rel, url: ssh_calls.append((hostname, "helm-repo-add:{}:{}".format(rel, url), {}))

ssh_calls.clear()
igl.setup_gitlab_kubernetes("host1", {})
cmds = [c[1] for c in ssh_calls]
check("setup_gitlab_kubernetes: adds the real official Helm repo (charts.gitlab.io)",
      any("helm-repo-add:gitlab:https://charts.gitlab.io/" in c for c in cmds))
install_cmd = next(c for c in cmds if c.startswith("helm upgrade --install"))
check("setup_gitlab_kubernetes: installs the real 'gitlab/gitlab' chart into the default "
      "'gitlab' namespace", "gitlab/gitlab -n gitlab" in install_cmd)
check("setup_gitlab_kubernetes: defaults to the free/open-source CE edition, NOT the chart's own "
      "default 'ee' — a deliberate override matching this project's own tooling preference",
      "global.edition=ce" in install_cmd)
check("setup_gitlab_kubernetes: TLS/cert-manager/Let's Encrypt are disabled by default — real "
      "reason: this project's own *.mydemo.lab domain convention is never publicly resolvable, "
      "so Let's Encrypt's HTTP01 challenge would just hang forever",
      "global.hosts.https=false" in install_cmd and "installCertmanager=false" in install_cmd
      and "global.ingress.tls.enabled=false" in install_cmd
      and "certmanager-issuer.email" not in install_cmd)
check("setup_gitlab_kubernetes: domain defaults to the target host's own hostname",
      "global.hosts.domain=host1" in install_cmd)

# https=true requires a real cert-manager-issuer email — Let's Encrypt itself
# requires a real contact address, this isn't optional.
died = False
try:
    igl.setup_gitlab_kubernetes("host1", {"gitlab_https": "true"})
except SystemExit:
    died = True
check("setup_gitlab_kubernetes: dies when gitlab_https is true but no "
      "gitlab_cert_manager_email is given", died)

ssh_calls.clear()
igl.setup_gitlab_kubernetes("host1", {
    "gitlab_https": "true", "gitlab_cert_manager_email": "admin@example.com",
    "gitlab_hostname": "gitlab.example.com", "gitlab_namespace": "custom-gitlab",
    "gitlab_edition": "ee",
})
install_cmd = next(c[1] for c in ssh_calls if c[1].startswith("helm upgrade --install"))
check("setup_gitlab_kubernetes: with a real public domain + email, enables real TLS/cert-manager",
      "global.hosts.https=true" in install_cmd and "installCertmanager=true" in install_cmd
      and "certmanager-issuer.email=admin@example.com" in install_cmd)
check("setup_gitlab_kubernetes: a custom namespace/domain/edition reach the real invocation",
      "-n custom-gitlab" in install_cmd and "global.hosts.domain=gitlab.example.com" in install_cmd
      and "global.edition=ee" in install_cmd)

died = False
try:
    igl.setup_gitlab_kubernetes("host1", {"gitlab_edition": "bogus"})
except SystemExit:
    died = True
check("setup_gitlab_kubernetes: dies on an invalid gitlab_edition", died)

# gitlab_extra_values passes arbitrary --set overrides straight through.
ssh_calls.clear()
igl.setup_gitlab_kubernetes("host1", {"gitlab_extra_values": {"gitlab.gitaly.persistence.size": "20Gi"}})
install_cmd = next(c[1] for c in ssh_calls if c[1].startswith("helm upgrade --install"))
check("setup_gitlab_kubernetes: gitlab_extra_values reach the real helm invocation as --set",
      "--set gitlab.gitaly.persistence.size=20Gi" in install_cmd)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all gitlab checks passed")
