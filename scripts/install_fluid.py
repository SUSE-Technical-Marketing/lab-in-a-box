#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Fluid data orchestration framework
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "fluid" — configurable keys:
#   fluid_version       : Helm chart version (empty = latest, e.g. "0.9.5")
#   fluid_ns            : namespace (default: fluid-system)
#   fluid_rel           : Helm repo alias (default: fluid)
#   fluid_repo_url      : Helm repo URL (default: https://fluid-cloudnative.github.io/charts)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "fluid",
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
    v.vns("fluid")
    v.vver("fluid")
    v.vurl("fluid")


def setup_fluid_repo(hostname, fluid_rel=None, fluid_repo_url=None):
    """Add the Fluid Helm repo. Mirrors setup_fluid_repo (bash)."""
    helm_repo_add(hostname, fluid_rel or "fluid", fluid_repo_url or "https://fluid-cloudnative.github.io/charts")


def setup_fluid(hostname, fluid_rel=None, fluid_ns=None, fluid_version=None):
    """Install Fluid. Mirrors setup_fluid (bash)."""
    rel = fluid_rel or "fluid"
    ns = fluid_ns or "fluid-system"
    ver_arg = "--version {}".format(fluid_version) if fluid_version else ""
    ssh_run(hostname,
            "helm upgrade -i fluid {}/fluid --namespace {} --create-namespace {}".format(rel, ns, ver_arg))
    print("Fluid installed. Namespace: {}".format(ns))


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
    cfg = definition.get("fluid", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_fluid_repo(vm_name, fluid_rel=cfg.get("fluid_rel"), fluid_repo_url=cfg.get("fluid_repo_url"))
    setup_fluid(vm_name, fluid_rel=cfg.get("fluid_rel"), fluid_ns=cfg.get("fluid_ns"),
                fluid_version=cfg.get("fluid_version"))


if __name__ == "__main__":
    main()
