#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install ArgoCD GitOps controller
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "argocd" — configurable keys:
#   argocd_version      : [OPTIONAL] Helm chart version (empty = latest, e.g. "7.6.8")
#   argocd_ns           : [OPTIONAL] namespace (default: argocd)
#   argocd_shorthn      : [OPTIONAL] hostname prefix (default: argocd)
#   argocd_rel          : [OPTIONAL] Helm repo alias (default: argo)
#   argocd_repo_url     : [OPTIONAL] Helm repo URL (default: https://argoproj.github.io/argo-helm)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "argocd",
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
    v.vns("argocd")
    v.vver("argocd")
    v.vurl("argocd")


def setup_argocd_repo(hostname, argocd_rel=None, argocd_repo_url=None):
    """Add the ArgoCD Helm repo. Mirrors setup_argocd_repo (bash)."""
    helm_repo_add(hostname, argocd_rel or "argo", argocd_repo_url or "https://argoproj.github.io/argo-helm")


def setup_argocd(hostname, clu_name, mydomain, argocd_rel=None, argocd_ns=None,
                  argocd_version=None, argocd_shorthn=None):
    """Install ArgoCD. Mirrors setup_argocd (bash)."""
    rel = argocd_rel or "argo"
    ns = argocd_ns or "argocd"
    ver_arg = "--version {}".format(argocd_version) if argocd_version else ""
    fqdn = "{}.{}.{}".format(argocd_shorthn or "argocd", clu_name, mydomain)
    ssh_run(hostname,
            "helm upgrade -i argocd {}/argo-cd "
            "--namespace {} --create-namespace "
            "--set server.ingress.enabled=true "
            "--set server.ingress.hostname={} "
            "--set configs.params.server\\.insecure=true "
            "{}".format(rel, ns, fqdn, ver_arg))
    print("ArgoCD available at: http://{}".format(fqdn))
    print("Initial admin password: kubectl -n {} get secret argocd-initial-admin-secret "
          "-o jsonpath='{{.data.password}}' | base64 -d".format(ns))


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
    argocd_cfg = definition.get("argocd", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_argocd_repo(vm_name, argocd_rel=argocd_cfg.get("argocd_rel"), argocd_repo_url=argocd_cfg.get("argocd_repo_url"))
    setup_argocd(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        argocd_rel=argocd_cfg.get("argocd_rel"), argocd_ns=argocd_cfg.get("argocd_ns"),
        argocd_version=argocd_cfg.get("argocd_version"), argocd_shorthn=argocd_cfg.get("argocd_shorthn"),
    )


if __name__ == "__main__":
    main()
