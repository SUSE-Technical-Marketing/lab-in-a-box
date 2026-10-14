#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Kubewarden policy engine
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "kubewarden" — configurable keys:
#   kubewarden_version  : [OPTIONAL] Helm chart version (empty = latest, e.g. "2.0.0")
#   kubewarden_ns       : [OPTIONAL] namespace (default: kubewarden)
#   kubewarden_rel      : [OPTIONAL] Helm repo alias (default: kubewarden)
#   kubewarden_repo_url : [OPTIONAL] Helm repo URL (default: https://charts.kubewarden.io)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kubewarden",
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
    v.vns("kubewarden")
    v.vver("kubewarden")
    v.vurl("kubewarden")


def setup_kubewarden_repo(hostname, kubewarden_rel=None, kubewarden_repo_url=None):
    """Add the Kubewarden Helm repo. Mirrors setup_kubewarden_repo (bash)."""
    helm_repo_add(hostname, kubewarden_rel or "kubewarden", kubewarden_repo_url or "https://charts.kubewarden.io")


def setup_kubewarden(hostname, kubewarden_rel=None, kubewarden_ns=None, kubewarden_version=None):
    """Install Kubewarden (crds -> controller -> defaults). Mirrors setup_kubewarden (bash)."""
    rel = kubewarden_rel or "kubewarden"
    ns = kubewarden_ns or "kubewarden"
    ver_arg = "--version {}".format(kubewarden_version) if kubewarden_version else ""

    print("# Installing Kubewarden CRDs")
    ssh_run(hostname,
            "helm upgrade -i kubewarden-crds {}/kubewarden-crds "
            "--namespace {} --create-namespace --wait {}".format(rel, ns, ver_arg))

    print("# Installing Kubewarden controller")
    ssh_run(hostname,
            "helm upgrade -i kubewarden-controller {}/kubewarden-controller "
            "--namespace {} --wait {}".format(rel, ns, ver_arg))

    print("# Installing Kubewarden defaults (recommended policies)")
    ssh_run(hostname,
            "helm upgrade -i kubewarden-defaults {}/kubewarden-defaults "
            "--namespace {} {}".format(rel, ns, ver_arg))

    print("Kubewarden installed. Namespace: {}".format(ns))


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
    cfg = definition.get("kubewarden", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_kubewarden_repo(vm_name, kubewarden_rel=cfg.get("kubewarden_rel"),
                           kubewarden_repo_url=cfg.get("kubewarden_repo_url"))
    setup_kubewarden(vm_name, kubewarden_rel=cfg.get("kubewarden_rel"), kubewarden_ns=cfg.get("kubewarden_ns"),
                      kubewarden_version=cfg.get("kubewarden_version"))


if __name__ == "__main__":
    main()
