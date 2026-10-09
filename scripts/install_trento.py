#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Trento SAP landscape monitoring
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.1
#
# JSON section: "trento" — configurable keys:
#   trento_version      : [OPTIONAL] Helm chart version (empty = latest, e.g. "2.4.0")
#   trento_ns           : [OPTIONAL] namespace (default: trento)
#   trento_shorthn      : [OPTIONAL] hostname prefix (default: trento)
#   trento_rel          : [OPTIONAL] Helm repo alias (default: trento)
#   trento_repo_url     : [OPTIONAL] Helm repo URL (default: https://trento-project.io/helm)
#   trento_admin        : [OPTIONAL] admin email (default: admin@lab.local)
#   trento_password     : [OPTIONAL] admin password (default: Trento12345)
#   trento_secret_key   : [OPTIONAL] secret key base for sessions; when unset, generated

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "trento",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import subprocess
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vns("trento")
    v.vver("trento")
    v.vurl("trento")


def setup_trento_repo(hostname, trento_rel=None, trento_repo_url=None):
    """Add the Trento Helm repo. Mirrors setup_trento_repo (bash)."""
    helm_repo_add(hostname, trento_rel or "trento", trento_repo_url or "https://trento-project.io/helm")


def setup_trento(hostname, clu_name, mydomain, trento_rel=None, trento_ns=None, trento_version=None,
                  trento_shorthn=None, trento_admin=None, trento_password=None, trento_secret_key=None):
    """Install Trento. Mirrors setup_trento (bash)."""
    rel = trento_rel or "trento"
    ns = trento_ns or "trento"
    ver_arg = "--version {}".format(trento_version) if trento_version else ""
    fqdn = "{}.{}.{}".format(trento_shorthn or "trento", clu_name, mydomain)
    admin = trento_admin or "admin@lab.local"
    password = trento_password or "Trento12345"

    if trento_secret_key:
        secret = trento_secret_key
    else:
        # bash runs `openssl rand -hex 32` LOCALLY (not over ssh) with a
        # hardcoded fallback string if openssl fails.
        r = subprocess.run(["openssl", "rand", "-hex", "32"], capture_output=True, text=True, check=False)
        secret = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "changeme-generate-a-real-secret"

    ssh_run(hostname,
            "helm upgrade -i trento {}/trento-server --namespace {} --create-namespace "
            "--set trento-web.adminUser.email={} "
            "--set trento-web.adminUser.password={} "
            "--set trento-web.secretKeyBase={} "
            "--set trento-web.ingress.enabled=true "
            "--set trento-web.ingress.host={} "
            "{}".format(rel, ns, admin, password, secret, fqdn, ver_arg))
    print("Trento available at: http://{}  ({} / {})".format(fqdn, admin, password))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("trento", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_trento_repo(vm_name, trento_rel=cfg.get("trento_rel"), trento_repo_url=cfg.get("trento_repo_url"))
    setup_trento(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        trento_rel=cfg.get("trento_rel"), trento_ns=cfg.get("trento_ns"), trento_version=cfg.get("trento_version"),
        trento_shorthn=cfg.get("trento_shorthn"), trento_admin=cfg.get("trento_admin"),
        trento_password=cfg.get("trento_password"), trento_secret_key=cfg.get("trento_secret_key"),
    )


if __name__ == "__main__":
    main()
