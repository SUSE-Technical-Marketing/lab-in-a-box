#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install nginx Ingress Controller
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "nginx" — configurable keys:
#   nginx_version       : Helm chart version (empty = latest, e.g. "4.10.1")
#   nginx_ns            : namespace (default: ingress-nginx)
#   nginx_rel           : Helm repo alias (default: ingress-nginx)
#   nginx_repo_url      : Helm repo URL (default: https://kubernetes.github.io/ingress-nginx)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "nginx",
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
    v.vns("nginx")
    v.vver("nginx")
    v.vurl("nginx")


def setup_nginx_repo(hostname, nginx_rel=None, nginx_repo_url=None):
    """Add the nginx Ingress Controller Helm repo. Mirrors setup_nginx_repo (bash)."""
    helm_repo_add(hostname, nginx_rel or "ingress-nginx", nginx_repo_url or "https://kubernetes.github.io/ingress-nginx")


def setup_nginx(hostname, nginx_rel=None, nginx_ns=None, nginx_version=None):
    """Install nginx Ingress Controller. Mirrors setup_nginx (bash)."""
    rel = nginx_rel or "ingress-nginx"
    ns = nginx_ns or "ingress-nginx"
    ver_arg = "--version {}".format(nginx_version) if nginx_version else ""
    ssh_run(hostname,
            "helm upgrade -i ingress-nginx {}/ingress-nginx --namespace {} --create-namespace {}".format(
                rel, ns, ver_arg))
    print("nginx Ingress Controller installed. Namespace: {}".format(ns))


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
    cfg = definition.get("nginx", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_nginx_repo(vm_name, nginx_rel=cfg.get("nginx_rel"), nginx_repo_url=cfg.get("nginx_repo_url"))
    setup_nginx(vm_name, nginx_rel=cfg.get("nginx_rel"), nginx_ns=cfg.get("nginx_ns"),
                nginx_version=cfg.get("nginx_version"))


if __name__ == "__main__":
    main()
