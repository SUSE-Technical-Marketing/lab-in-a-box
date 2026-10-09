#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Kucero (Kubernetes Certificate Rotation)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "kucero" — configurable keys:
#   kucero_version      : Helm chart version (empty = latest, e.g. "1.5.0")
#   kucero_ns           : namespace (default: kube-system)
#   kucero_rel          : Helm repo alias (default: reactive-tech)
#   kucero_repo_url     : Helm repo URL (default: https://charts.reactive-tech.io)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kucero",
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
    v.vns("kucero")
    v.vver("kucero")
    v.vurl("kucero")


def setup_kucero_repo(hostname, kucero_rel=None, kucero_repo_url=None):
    """Add the Kucero Helm repo. Mirrors setup_kucero_repo (bash)."""
    helm_repo_add(hostname, kucero_rel or "reactive-tech", kucero_repo_url or "https://charts.reactive-tech.io")


def setup_kucero(hostname, kucero_rel=None, kucero_ns=None, kucero_version=None):
    """Install Kucero. Mirrors setup_kucero (bash)."""
    rel = kucero_rel or "reactive-tech"
    ns = kucero_ns or "kube-system"
    ver_arg = "--version {}".format(kucero_version) if kucero_version else ""
    ssh_run(hostname,
            "helm upgrade -i kucero {}/kucero --namespace {} --create-namespace {}".format(rel, ns, ver_arg))
    print("Kucero installed. Namespace: {}".format(ns))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    # bash inlines the on_first_server loop directly here instead of calling
    # the helper function — functionally identical, ported the same way.
    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    cfg = definition.get("kucero", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_kucero_repo(vm_name, kucero_rel=cfg.get("kucero_rel"), kucero_repo_url=cfg.get("kucero_repo_url"))
    setup_kucero(vm_name, kucero_rel=cfg.get("kucero_rel"), kucero_ns=cfg.get("kucero_ns"),
                 kucero_version=cfg.get("kucero_version"))


if __name__ == "__main__":
    main()
