#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install GitLab — either as a standalone podman
# container directly on a host/VM (no Kubernetes needed) or on a real
# Kubernetes cluster via GitLab's own official Helm chart.
# Author/s: Raul Mahiques
# License: GPLv3
#
# References (fetched raw, ground-truthed 2026-09-26, not guessed):
#   https://gitlab.com/gitlab-org/charts/gitlab/-/raw/master/values.yaml
#     (the real, current chart's own default values — confirms global.edition,
#     global.hosts.domain/https, installCertmanager, certmanager-issuer.email,
#     and that nginx-ingress.enabled defaults to false — this chart assumes
#     the cluster already has an ingress controller, matching RKE2/K3s's own
#     bundled one, so this addon does not need to bundle a second one itself)
#   GitLab's own long-documented Omnibus Docker deployment shape (single
#   all-in-one container, GITLAB_OMNIBUS_CONFIG env var for Ruby-DSL config
#   overrides, real root password readable from
#   /etc/gitlab/initial_root_password inside the container after first boot —
#   this addon reads that file back directly rather than assuming any
#   particular override env var took effect, so the password it prints is
#   always the one actually in use, not a guess)
#
# ─── JSON section: "gitlab" ───────────────────────────────────────────────────
#
#   gitlab_deployment    : "podman" (default) = a single standalone Omnibus
#                           container directly on the target host/VM, no
#                           Kubernetes involved at all — matches GitLab's own
#                           official Docker quickstart exactly. "kubernetes" =
#                           the real official Helm chart (charts.gitlab.io),
#                           deployed once to the cluster's server node.
#
# ── "podman" fields ───────────────────────────────────────────────────────────
#   gitlab_image          : container image                (default: "gitlab/gitlab-ce" —
#                           the real upstream Omnibus CE image; there is no SUSE-branded
#                           GitLab image)
#   gitlab_version         : image tag                      (default: "latest")
#   gitlab_hostname         : external_url hostname to configure GitLab with
#                           (default: the target node's own hostname). Real GitLab behavior:
#                           this becomes the literal external_url GitLab redirects to and signs
#                           links with — get it right up front, changing it later needs a real
#                           `gitlab-ctl reconfigure`.
#   gitlab_https_port       : host port -> container 443    (default: 443)
#   gitlab_http_port        : host port -> container 80     (default: 80)
#   gitlab_ssh_port         : host port -> container 22     (default: 2222 — NOT the real 22,
#                           since that's almost always already the HOST's own sshd; GitLab's
#                           own docs use 2222 as their own example for exactly this reason)
#   gitlab_root_password    : initial root password. GitLab's real Omnibus behavior: this is
#                           injected via GITLAB_OMNIBUS_CONFIG's gitlab_rails
#                           ['initial_root_password'], which only takes effect on the container's
#                           very FIRST boot (a real, documented GitLab limitation — it does not
#                           reset an existing instance's password on a later restart). Left
#                           unset, this addon reads back whatever GitLab itself auto-generated
#                           at /etc/gitlab/initial_root_password (valid 24h only, GitLab's own
#                           real expiry) and prints that instead, rather than assuming any
#                           particular value took effect.
#   gitlab_account          : name of an encrypted credential_kind "gitlab" file under
#                           /etc/lab_creation/credentials/ (see README's Credentials section) to
#                           read gitlab_root_password from instead of this section's own
#                           plaintext field — auto-discovered if exactly one "gitlab" credential
#                           file exists and this is left unset.
#
# ── "kubernetes" fields ───────────────────────────────────────────────────────
#   gitlab_rel              : Helm repo alias               (default: "gitlab")
#   gitlab_repo_url         : Helm repo URL                 (default: "https://charts.gitlab.io/")
#   gitlab_chart_version     : Helm chart version             (empty = latest)
#   gitlab_namespace        : Kubernetes namespace           (default: "gitlab")
#   gitlab_edition          : "ce" (default, free/open-source) or "ee" — the chart's own
#                           default is "ee"; this addon defaults to "ce" instead, matching this
#                           project's own general preference for free/open-source tooling
#                           (see README/CLAUDE.md).
#   gitlab_https            : "true"/"false"                 (default: "false" — REAL reason:
#                           the chart's own default TLS setup provisions real Let's Encrypt
#                           certs via cert-manager's HTTP01 challenge, which requires the
#                           hostname to be a REAL, PUBLICLY RESOLVABLE domain pointed at the
#                           cluster's ingress IP; this project's own lab domain convention
#                           (*.mydemo.lab) is never publicly resolvable, so Let's Encrypt would
#                           just hang/fail forever. Set "true" only against a real public domain.)
#   gitlab_cert_manager_email : email for the Let's Encrypt ACME issuer — REQUIRED if
#                           gitlab_https is "true" (the chart's own certmanager-issuer.email;
#                           Let's Encrypt itself requires a real contact address).
#   gitlab_extra_values      : [OPTIONAL] a dict of additional raw `--set key=value` pairs to
#                           pass straight through to `helm upgrade --install`, for anything this
#                           addon doesn't expose its own field for — e.g.
#                           {"gitlab.gitaly.persistence.size": "20Gi"}.
#
# Real minimum resource requirements (GitLab's own documented reference architecture, even for a
# single-node "just try it" install): 4 vCPU / 8GB RAM at an absolute minimum, considerably more
# comfortable at 4 vCPU / 16GB — GitLab bundles its own PostgreSQL, Redis, Gitaly and (in
# Kubernetes mode) object storage-backed registry/pages services internally; this is a genuinely
# heavy application, not a lightweight one.
#
# NOT live-tested in Kubernetes mode — no real Kubernetes cluster was available in this session
# to deploy the Helm chart against. The chart values above are ground-truthed against the real,
# current upstream values.yaml (see the reference at the top of this file), but the full
# kubernetes-mode pipeline has only been exercised via the mocked test suite. Podman mode follows
# GitLab's own long-stable, extremely widely used Docker deployment shape closely enough that
# it's expected to work as documented, but was also not live-tested against real infrastructure
# in this session (no spare host with the real resource budget above was available).

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "gitlab",
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "standalone-container"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import secrets
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, helm_repo_add, die  # noqa: E402

_DEFAULT_IMAGE = "gitlab/gitlab-ce"


def _validate(v):
    v.vver("gitlab")


def setup_gitlab_podman(hostname, cfg):
    """
    Deploys GitLab as a single standalone Omnibus podman container directly
    on `hostname` — no Kubernetes, matching install_grafana.py/
    install_prometheus.py's own style (raw SSH, a host-level systemd unit
    for persistence) and GitLab's own official Docker deployment shape
    exactly (one all-in-one container; GitLab runs its own internal
    PostgreSQL/Redis/Gitaly/nginx inside it — no external dependencies).
    """
    image = cfg.get("gitlab_image") or _DEFAULT_IMAGE
    version = cfg.get("gitlab_version") or "latest"
    gitlab_hostname = cfg.get("gitlab_hostname") or hostname
    https_port = int(cfg.get("gitlab_https_port") or 443)
    http_port = int(cfg.get("gitlab_http_port") or 80)
    ssh_port = int(cfg.get("gitlab_ssh_port") or 2222)

    creds = ac.resolve_credential(cfg, "gitlab", {"gitlab_root_password": "gitlab_root_password"},
                                   account_key="gitlab_account")
    root_password = creds["gitlab_root_password"]

    print("- Installing podman (if not already present)")
    ssh_run(hostname, "command -v podman >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y podman 2>/dev/null || "
                       "apt-get install -y podman 2>/dev/null || "
                       "dnf install -y podman 2>/dev/null)", check=False)
    ssh_run(hostname, "systemctl enable --now podman.socket", check=False)

    # GITLAB_OMNIBUS_CONFIG is GitLab's own real mechanism for injecting
    # Ruby-DSL Omnibus config lines at first boot — external_url is
    # REQUIRED (GitLab signs/redirects every link with it), and
    # initial_root_password (if given) only ever takes effect on the very
    # first boot, a real, documented GitLab limitation, not a bug here.
    def _ruby_sq(value):
        """Escapes `value` as a Ruby single-quoted string literal (backslash and the
        quote itself) — for embedding in the Ruby-DSL GITLAB_OMNIBUS_CONFIG value,
        NOT shell quoting (that's applied once, to the whole assembled string, below)."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    omnibus_lines = ["external_url 'http://{}:{}'".format(_ruby_sq(gitlab_hostname), http_port)]
    if root_password:
        omnibus_lines.append("gitlab_rails['initial_root_password'] = '{}'".format(
            _ruby_sq(root_password)))
    omnibus_config = "; ".join(omnibus_lines)

    print("- Deploying the GitLab container (this pulls a large image and runs its own internal "
          "reconfigure on first boot — real GitLab startup genuinely takes several minutes)")
    ssh_run(hostname, "podman rm -f gitlab 2>/dev/null", check=False)
    ssh_run(hostname,
            "podman run -d --name gitlab --restart=always --hostname {gitlab_hostname} "
            "--shm-size 256m "
            "-p {https_port}:443 -p {http_port}:80 -p {ssh_port}:22 "
            "-e GITLAB_OMNIBUS_CONFIG={omnibus_config} "
            "-v gitlab-config:/etc/gitlab "
            "-v gitlab-logs:/var/log/gitlab "
            "-v gitlab-data:/var/opt/gitlab "
            "{image}:{version}".format(
                gitlab_hostname=shlex.quote(gitlab_hostname),
                https_port=https_port, http_port=http_port, ssh_port=ssh_port,
                omnibus_config=shlex.quote(omnibus_config),
                image=shlex.quote(image), version=shlex.quote(version)))

    print("- Generating a systemd unit so the container survives a reboot")
    unit = (
        "[Unit]\n"
        "Description=GitLab (lab-in-a-box)\n"
        "After=network-online.target podman.socket\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Restart=always\n"
        "TimeoutStartSec=300\n"
        "ExecStart=/usr/bin/podman start -a gitlab\n"
        "ExecStop=/usr/bin/podman stop -t 30 gitlab\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    ssh_run(hostname, "cat > /etc/systemd/system/gitlab.service <<'EOF'\n{}EOF".format(unit))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable gitlab.service", check=False)

    print("- Waiting for GitLab's own internal reconfigure to finish and write its real root "
          "password file (up to 5 minutes — GitLab's first boot is genuinely slow)")
    password_file = None
    deadline = time.time() + 300
    while time.time() < deadline:
        r = ssh_run(hostname, "podman exec gitlab cat /etc/gitlab/initial_root_password 2>/dev/null",
                    check=False, capture=True)
        if r.returncode == 0 and "Password:" in (r.stdout or ""):
            password_file = r.stdout
            break
        time.sleep(10)

    print("GitLab available at: http://{}:{}".format(gitlab_hostname, http_port))
    print("SSH (git over ssh):  ssh -p {} git@{}".format(ssh_port, gitlab_hostname))
    if password_file:
        for line in password_file.splitlines():
            if line.startswith("Password:"):
                print("Initial root password: {}".format(line.split(":", 1)[1].strip()))
                print("(from /etc/gitlab/initial_root_password inside the container — GitLab's "
                      "own real file; it stops being readable there 24 hours after first boot, "
                      "per GitLab's own documented behavior — change the password before then)")
                break
    elif root_password:
        print("Initial root password: {} (as configured — could not confirm it was actually "
              "applied; GitLab was still reconfiguring after 5 minutes)".format(root_password))
    else:
        print("WARNING: could not read back GitLab's real generated root password within 5 "
              "minutes — GitLab may still be reconfiguring. Check later with: "
              "podman exec gitlab cat /etc/gitlab/initial_root_password")


def setup_gitlab_kubernetes(hostname, cfg):
    """
    Installs GitLab via its own real, official Helm chart (charts.gitlab.io)
    — see this file's own top-of-file reference for the exact values keys
    ground-truthed against the chart's current values.yaml. Deployed ONCE
    to the cluster's server node, matching install_jenkins.py/
    install_ds389.py's own convention for a cluster-wide Kubernetes app
    (not per-node, unlike the podman path above).
    """
    ns = ac.require_k8s_name(cfg, "gitlab_namespace", "gitlab")
    domain = cfg.get("gitlab_hostname") or hostname
    edition = (cfg.get("gitlab_edition") or "ce").lower()
    if edition not in ("ce", "ee"):
        die("gitlab: gitlab_edition must be 'ce' or 'ee', got '{}'".format(edition))
    https = (cfg.get("gitlab_https") or "false").lower() == "true"
    cert_email = cfg.get("gitlab_cert_manager_email")
    if https and not cert_email:
        die("gitlab: gitlab_cert_manager_email is required when gitlab_https is \"true\" — "
            "Let's Encrypt itself requires a real contact address")

    helm_repo_add(hostname, cfg.get("gitlab_rel") or "gitlab", cfg.get("gitlab_repo_url") or
                  "https://charts.gitlab.io/")

    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)

    set_args = [
        "--set global.edition={}".format(edition),
        "--set global.hosts.domain={}".format(domain),
        "--set global.hosts.https={}".format("true" if https else "false"),
        "--set installCertmanager={}".format("true" if https else "false"),
        "--set global.ingress.tls.enabled={}".format("true" if https else "false"),
    ]
    if https:
        set_args.append("--set certmanager-issuer.email={}".format(cert_email))
    for key, value in (cfg.get("gitlab_extra_values") or {}).items():
        set_args.append("--set {}={}".format(shlex.quote(str(key)), shlex.quote(str(value))))

    version_arg = "--version {}".format(shlex.quote(cfg["gitlab_chart_version"])) \
        if cfg.get("gitlab_chart_version") else ""

    print("- Installing GitLab via its own official Helm chart into namespace '{}' (this pulls "
          "and starts several sub-charts — webservice, sidekiq, gitaly, postgresql, redis — real "
          "GitLab first-boot is genuinely slow, expect several minutes)".format(ns))
    ssh_run(hostname, "helm upgrade --install gitlab {rel}/gitlab -n {ns} {version} --timeout 600s "
                       "{sets}".format(
                           rel=cfg.get("gitlab_rel") or "gitlab", ns=ns, version=version_arg,
                           sets=" ".join(set_args)))

    print("GitLab installed in namespace '{}'.".format(ns))
    print("Real root password: GitLab generates one and stores it as a Kubernetes Secret named "
          "'gitlab-gitlab-initial-root-password' (24h validity, GitLab's own real behavior) — "
          "read it with: kubectl get secret gitlab-gitlab-initial-root-password -n {} "
          "-ojsonpath='{{.data.password}}' | base64 --decode".format(ns))
    if not https:
        print("TLS is disabled (gitlab_https left at its default \"false\") — reachable over "
              "plain http:// only. This is correct for this project's own non-public *.mydemo.lab "
              "domain convention; set gitlab_https: \"true\" with a real public domain + "
              "gitlab_cert_manager_email for real Let's Encrypt certs.")


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    cfg = definition.get("gitlab", {}) or {}

    if (cfg.get("gitlab_deployment") or "podman") == "kubernetes":
        target = k8s.first_server_node(definition)
        if not target:
            sys.exit(1)
        vm_name, _ssh_cmd = target
        setup_gitlab_kubernetes(vm_name, cfg)
        return

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "gitlab", vm_name=env_vm_name):
        setup_gitlab_podman(vm_name, cfg)


if __name__ == "__main__":
    main()
