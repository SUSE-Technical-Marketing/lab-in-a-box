#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Linkerd service mesh
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "linkerd" — configurable keys:
#   linkerd_version     : [OPTIONAL] Helm chart version (empty = latest, e.g. "2.14.10")
#   linkerd_ns          : [OPTIONAL] control plane namespace (default: linkerd)
#   linkerd_viz_ns      : [OPTIONAL] viz extension namespace (default: linkerd-viz)
#   linkerd_rel         : [OPTIONAL] Helm repo alias (default: linkerd)
#   linkerd_repo_url    : [OPTIONAL] Helm repo URL (default: https://helm.linkerd.io/stable)
#   linkerd_install_viz : [OPTIONAL] install viz dashboard (default: true)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "linkerd",
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
    v.vns("linkerd")
    v.vver("linkerd")
    v.vurl("linkerd")


def setup_linkerd_repo(hostname, linkerd_rel=None, linkerd_repo_url=None):
    """Add the Linkerd Helm repo. Mirrors setup_linkerd_repo (bash)."""
    helm_repo_add(hostname, linkerd_rel or "linkerd", linkerd_repo_url or "https://helm.linkerd.io/stable")


def setup_linkerd(hostname, linkerd_rel=None, linkerd_ns=None, linkerd_viz_ns=None,
                   linkerd_version=None, linkerd_install_viz=None):
    """Install Linkerd (CRDs -> control plane -> viz). Mirrors setup_linkerd (bash)."""
    rel = linkerd_rel or "linkerd"
    ns = linkerd_ns or "linkerd"
    viz_ns = linkerd_viz_ns or "linkerd-viz"
    ver_arg = "--version {}".format(linkerd_version) if linkerd_version else ""

    print("# Installing Linkerd CRDs")
    ssh_run(hostname,
            "helm upgrade -i linkerd-crds {}/linkerd-crds "
            "--namespace {} --create-namespace {}".format(rel, ns, ver_arg))

    print("# Installing Linkerd control plane")
    ssh_run(hostname,
            "helm upgrade -i linkerd-control-plane {}/linkerd-control-plane "
            "--namespace {} "
            "--set-file identityTrustAnchorsPEM=/dev/null "
            "--set identity.issuer.scheme=kubernetes.io/tls "
            "{}".format(rel, ns, ver_arg))

    if str(linkerd_install_viz or "true") != "false":
        print("# Installing Linkerd viz extension")
        ssh_run(hostname,
                "helm upgrade -i linkerd-viz {}/linkerd-viz "
                "--namespace {} --create-namespace {}".format(rel, viz_ns, ver_arg))
        print("Linkerd viz dashboard installed. Namespace: {}".format(viz_ns))

    print("Linkerd installed. Namespace: {}".format(ns))


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
    cfg = definition.get("linkerd", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_linkerd_repo(vm_name, linkerd_rel=cfg.get("linkerd_rel"), linkerd_repo_url=cfg.get("linkerd_repo_url"))
    setup_linkerd(
        vm_name, linkerd_rel=cfg.get("linkerd_rel"), linkerd_ns=cfg.get("linkerd_ns"),
        linkerd_viz_ns=cfg.get("linkerd_viz_ns"), linkerd_version=cfg.get("linkerd_version"),
        linkerd_install_viz=cfg.get("linkerd_install_viz"),
    )


if __name__ == "__main__":
    main()
