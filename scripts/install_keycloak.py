#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Keycloak identity provider
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "keycloak" — configurable keys:
#   keycloak_version    : [OPTIONAL] Helm chart version (empty = latest, e.g. "22.1.0")
#   keycloak_ns         : [OPTIONAL] namespace (default: keycloak)
#   keycloak_shorthn    : [OPTIONAL] hostname prefix (default: keycloak)
#   keycloak_rel        : [OPTIONAL] Helm repo alias (default: bitnami)
#   keycloak_repo_url   : [OPTIONAL] Helm repo URL (default: https://charts.bitnami.com/bitnami)
#   keycloak_admin      : [OPTIONAL] admin username (default: admin)
#   keycloak_password   : [OPTIONAL] admin password (default: keycloak123)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "keycloak",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
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
    v.vns("keycloak")
    v.vver("keycloak")
    v.vurl("keycloak")


def setup_keycloak_repo(hostname, keycloak_rel=None, keycloak_repo_url=None):
    """Add the Keycloak Helm repo. Mirrors setup_keycloak_repo (bash)."""
    helm_repo_add(hostname, keycloak_rel or "bitnami", keycloak_repo_url or "https://charts.bitnami.com/bitnami")


def setup_keycloak(hostname, clu_name, mydomain, keycloak_rel=None, keycloak_ns=None,
                    keycloak_version=None, keycloak_shorthn=None, keycloak_admin=None,
                    keycloak_password=None):
    """Install Keycloak. Mirrors setup_keycloak (bash)."""
    rel = keycloak_rel or "bitnami"
    ns = keycloak_ns or "keycloak"
    ver_arg = "--version {}".format(keycloak_version) if keycloak_version else ""
    fqdn = "{}.{}.{}".format(keycloak_shorthn or "keycloak", clu_name, mydomain)
    ssh_run(hostname,
            "helm upgrade -i keycloak {}/keycloak "
            "--namespace {} --create-namespace "
            "--set auth.adminUser={} "
            "--set auth.adminPassword={} "
            "--set ingress.enabled=true "
            "--set ingress.hostname={} "
            "{}".format(shlex.quote(rel), shlex.quote(ns),
                        shlex.quote(keycloak_admin or "admin"),
                        shlex.quote(keycloak_password or "keycloak123"),
                        shlex.quote(fqdn), ver_arg))
    print("Keycloak available at: http://{}".format(fqdn))


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
    cfg = definition.get("keycloak", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_keycloak_repo(vm_name, keycloak_rel=cfg.get("keycloak_rel"), keycloak_repo_url=cfg.get("keycloak_repo_url"))
    setup_keycloak(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        keycloak_rel=cfg.get("keycloak_rel"), keycloak_ns=cfg.get("keycloak_ns"),
        keycloak_version=cfg.get("keycloak_version"), keycloak_shorthn=cfg.get("keycloak_shorthn"),
        keycloak_admin=cfg.get("keycloak_admin"), keycloak_password=cfg.get("keycloak_password"),
    )


if __name__ == "__main__":
    main()
