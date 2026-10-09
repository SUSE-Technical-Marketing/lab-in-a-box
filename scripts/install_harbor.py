#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Harbor container registry
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "harbor" — configurable keys:
#   harbor_version       : [OPTIONAL] Helm chart version (empty = latest, e.g. "1.15.0")
#   harbor_ns            : [OPTIONAL] namespace (default: harbor)
#   harbor_shorthn       : [OPTIONAL] hostname prefix (default: harbor)
#   harbor_rel           : [OPTIONAL] Helm repo alias (default: harbor)
#   harbor_repo_url      : [OPTIONAL] Helm repo URL (default: https://helm.goharbor.io)
#   harbor_admin_password: [OPTIONAL] admin password (default: Harbor12345)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "harbor",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

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
    v.vns("harbor")
    v.vver("harbor")
    v.vurl("harbor")


def setup_harbor_repo(hostname, harbor_rel=None, harbor_repo_url=None):
    """Add the Harbor Helm repo. Mirrors setup_harbor_repo (bash)."""
    helm_repo_add(hostname, harbor_rel or "harbor", harbor_repo_url or "https://helm.goharbor.io")


def setup_harbor(hostname, clu_name, mydomain, harbor_rel=None, harbor_ns=None, harbor_version=None,
                  harbor_shorthn=None, harbor_admin_password=None):
    """Install Harbor. Mirrors setup_harbor (bash)."""
    rel = harbor_rel or "harbor"
    ns = harbor_ns or "harbor"
    ver_arg = "--version {}".format(harbor_version) if harbor_version else ""
    fqdn = "{}.{}.{}".format(harbor_shorthn or "harbor", clu_name, mydomain)
    admin_pwd = harbor_admin_password or "Harbor12345"

    ssh_run(hostname,
            "helm upgrade -i harbor {}/harbor --namespace {} --create-namespace "
            "--set expose.type=ingress "
            "--set expose.ingress.hosts.core={} "
            "--set expose.tls.enabled=false "
            "--set externalURL=http://{} "
            "--set harborAdminPassword={} "
            "{}".format(rel, ns, fqdn, fqdn, admin_pwd, ver_arg))
    print("Harbor available at: http://{}  (admin / {})".format(fqdn, admin_pwd))


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
    cfg = definition.get("harbor", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_harbor_repo(vm_name, harbor_rel=cfg.get("harbor_rel"), harbor_repo_url=cfg.get("harbor_repo_url"))
    setup_harbor(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        harbor_rel=cfg.get("harbor_rel"), harbor_ns=cfg.get("harbor_ns"), harbor_version=cfg.get("harbor_version"),
        harbor_shorthn=cfg.get("harbor_shorthn"), harbor_admin_password=cfg.get("harbor_admin_password"),
    )


if __name__ == "__main__":
    main()
