#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Istio service mesh
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "istio" — configurable keys:
#   istio_version       : [OPTIONAL] Helm chart version (empty = latest, e.g. "1.22.3")
#   istio_ns            : [OPTIONAL] control plane namespace (default: istio-system)
#   istio_gateway_ns    : [OPTIONAL] ingress gateway namespace (default: istio-ingress)
#   istio_rel           : [OPTIONAL] Helm repo alias (default: istio)
#   istio_repo_url      : [OPTIONAL] Helm repo URL (default: https://istio-release.storage.googleapis.com/charts)
#   istio_install_gateway: [OPTIONAL] install ingress gateway (default: true)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "istio",
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
    v.vns("istio")
    v.vver("istio")


def setup_istio_repo(hostname, istio_rel=None, istio_repo_url=None):
    """Add the Istio Helm repo. Mirrors setup_istio_repo (bash)."""
    helm_repo_add(hostname, istio_rel or "istio", istio_repo_url or "https://istio-release.storage.googleapis.com/charts")


def setup_istio(hostname, istio_rel=None, istio_ns=None, istio_gateway_ns=None, istio_version=None,
                 istio_install_gateway=None):
    """Install Istio (base -> istiod -> gateway). Mirrors setup_istio (bash)."""
    rel = istio_rel or "istio"
    ns = istio_ns or "istio-system"
    gw_ns = istio_gateway_ns or "istio-ingress"
    ver_arg = "--version {}".format(istio_version) if istio_version else ""

    print("# Installing Istio base (CRDs)")
    ssh_run(hostname,
            "helm upgrade -i istio-base {}/base --namespace {} --create-namespace {}".format(rel, ns, ver_arg))

    print("# Installing istiod control plane")
    ssh_run(hostname, "helm upgrade -i istiod {}/istiod --namespace {} --wait {}".format(rel, ns, ver_arg))

    if str(istio_install_gateway or "true") != "false":
        print("# Installing Istio ingress gateway")
        ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(gw_ns), check=False)
        ssh_run(hostname, "helm upgrade -i istio-ingress {}/gateway --namespace {} {}".format(rel, gw_ns, ver_arg))
        print("Istio ingress gateway installed. Namespace: {}".format(gw_ns))

    print("Istio installed. Control plane namespace: {}".format(ns))


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
    cfg = definition.get("istio", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_istio_repo(vm_name, istio_rel=cfg.get("istio_rel"), istio_repo_url=cfg.get("istio_repo_url"))
    setup_istio(
        vm_name, istio_rel=cfg.get("istio_rel"), istio_ns=cfg.get("istio_ns"),
        istio_gateway_ns=cfg.get("istio_gateway_ns"), istio_version=cfg.get("istio_version"),
        istio_install_gateway=cfg.get("istio_install_gateway"),
    )


if __name__ == "__main__":
    main()
