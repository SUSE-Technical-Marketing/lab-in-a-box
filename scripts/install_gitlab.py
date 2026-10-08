#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs GitLab either as a standalone podman container on a host or VM, with no Kubernetes, or on a
# Kubernetes cluster with GitLab's official Helm chart.
# Author/s: Raul Mahiques
# License: GPLv3
#
# References: the chart's values.yaml (https://gitlab.com/gitlab-org/charts/gitlab/-/raw/master/values.yaml) and GitLab's Omnibus
# Docker deployment. The container follows the Omnibus Docker shape: GITLAB_OMNIBUS_CONFIG carries the configuration, and the initial
# root password is written to /etc/gitlab/initial_root_password inside the container. The script reads that file back, so the
# password it prints is the one in use.
#
# JSON section: "gitlab"
#   gitlab_deployment    : "podman" (default) runs one Omnibus container on the target host. "kubernetes" deploys the official
#                          Helm chart once, to the server node of the cluster.
#
# podman fields
#   gitlab_image         : container image (default gitlab/gitlab-ce, the upstream Omnibus CE image)
#   gitlab_version       : image tag (default latest)
#   gitlab_hostname      : external_url hostname (default: the node's own hostname). GitLab uses it in every link and redirect. A later
#                          change needs gitlab-ctl reconfigure.
#   gitlab_https_port    : host port for container port 443 (default 443)
#   gitlab_http_port     : host port for container port 80 (default 80)
#   gitlab_ssh_port      : host port for container port 22 (default 2222). Port 22 is normally the host's own sshd.
#   gitlab_root_password : initial root password. GitLab applies it only on the first boot of the container. When it is unset, the
#                          script reads the password GitLab generated in /etc/gitlab/initial_root_password. That file expires after 24 hours.
#   gitlab_account       : name of an encrypted credential file of kind "gitlab" under /etc/lab_creation/credentials/ to read
#                          gitlab_root_password from. It is auto-discovered when exactly one such file exists and this is unset.
#
# kubernetes fields
#   gitlab_rel           : Helm repo alias (default gitlab)
#   gitlab_repo_url      : Helm repo URL (default https://charts.gitlab.io/)
#   gitlab_chart_version : Helm chart version (empty = latest)
#   gitlab_namespace     : Kubernetes namespace (default gitlab)
#   gitlab_edition       : "ce" (default, free and open source) or "ee". The chart default is ee.
#   gitlab_https         : "true" or "false" (default false). The chart's default TLS uses a public Let's Encrypt certificate through
#                          cert-manager HTTP-01, which needs a publicly resolvable hostname. Set it to true only for a public domain.
#   gitlab_cert_manager_email : contact e-mail for the Let's Encrypt issuer. Required when gitlab_https is true.
#   gitlab_extra_values  : a dict of extra key=value pairs for helm upgrade --install, for settings this addon has no field for,
#                          for example {"gitlab.gitaly.persistence.size": "20Gi"}.
#
# Resources: GitLab needs at least 4 vCPU and 8 GB of RAM. 4 vCPU and 16 GB is more comfortable. GitLab runs its own PostgreSQL,
# Redis and Gitaly services.

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

    print("- Waiting for GitLab to actually finish reconfiguring and start serving real HTTP "
          "requests (up to 5 minutes — GitLab's first boot is genuinely slow)")
    # Wait for a real HTTP response from the login page before reading the root password. GitLab writes the password file early,
    # while database migrations still run, so the file alone does not show that GitLab is ready. The file is read after the response,
    # when Omnibus has finished writing it.
    gitlab_ready = False
    deadline = time.time() + 300
    while time.time() < deadline:
        r = ssh_run(hostname, "curl -sk -o /dev/null -w '%{{http_code}}' http://localhost/users/sign_in",
                    check=False, capture=True)
        if r.returncode == 0 and (r.stdout or "").strip() == "200":
            gitlab_ready = True
            break
        time.sleep(10)

    password_file = None
    if gitlab_ready:
        r = ssh_run(hostname, "podman exec gitlab cat /etc/gitlab/initial_root_password 2>/dev/null",
                    check=False, capture=True)
        if r.returncode == 0 and "Password:" in (r.stdout or ""):
            password_file = r.stdout

    print("GitLab available at: http://{}:{}".format(gitlab_hostname, http_port))
    print("SSH (git over ssh):  ssh -p {} git@{}".format(ssh_port, gitlab_hostname))
    if not gitlab_ready:
        print("WARNING: GitLab was still not answering real HTTP requests after 5 minutes — it "
              "may just need more time (a slower host, or a cold image pull, can both push first "
              "boot past 5 minutes). Check progress with: podman logs gitlab")
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
              "applied yet)".format(root_password))
    else:
        print("Could not read back GitLab's real generated root password ({}). Check later "
              "with: podman exec gitlab cat /etc/gitlab/initial_root_password".format(
                  "GitLab wasn't ready yet" if not gitlab_ready else
                  "it was ready but the password file wasn't found — unexpected, worth "
                  "investigating directly"))


def setup_gitlab_kubernetes(hostname, cfg):
    """
    Installs GitLab via its own real, official Helm chart (charts.gitlab.io)
    — see this file's own top-of-file reference for the exact values keys,
    which match the chart's current values.yaml. Deployed ONCE
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
