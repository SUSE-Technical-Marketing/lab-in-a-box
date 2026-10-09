#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will add the SUSE Application Collection Helm repository
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.1
#
# JSON section: "appcollection" — configurable keys:
#   appcollection_rel       : [OPTIONAL] Helm repo alias (default: suse-application-collection)
#   appcollection_repo_url  : [OPTIONAL] Helm repo URL
#                             (default: https://charts.suse.com/application-collection)
#   appcollection_ns        : [OPTIONAL] default namespace for installs (default: app-collection)
#   appcollection_chart     : [OPTIONAL] specific chart to install from the collection; when unset, none is installed
#   appcollection_release   : [OPTIONAL] Helm release name when installing a chart; when unset, the chart name
#   appcollection_version   : [OPTIONAL] chart version when appcollection_chart is set (empty = latest)
#
# This script adds the SUSE Application Collection repository to Helm and optionally
# installs one specific chart from it. To install multiple charts, call this script
# multiple times with different appcollection_chart values or use 'helm install' directly.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "appcollection",
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
    v.vver("appcollection")
    v.vurl("appcollection")


def setup_appcollection_repo(hostname, appcollection_rel=None, appcollection_repo_url=None):
    """Add the SUSE Application Collection Helm repo. Mirrors setup_appcollection_repo (bash)."""
    helm_repo_add(hostname, appcollection_rel or "suse-application-collection",
                  appcollection_repo_url or "https://charts.suse.com/application-collection")


def setup_appcollection(hostname, appcollection_rel=None, appcollection_ns=None, appcollection_chart=None,
                         appcollection_release=None, appcollection_version=None):
    """Add the repo and optionally install a single chart from it. Mirrors setup_appcollection (bash)."""
    rel = appcollection_rel or "suse-application-collection"
    if appcollection_chart:
        ns = appcollection_ns or "app-collection"
        ver_arg = "--version {}".format(appcollection_version) if appcollection_version else ""
        release = appcollection_release or appcollection_chart
        ssh_run(hostname,
                "helm upgrade -i {} {}/{} --namespace {} --create-namespace {}".format(
                    release, rel, appcollection_chart, ns, ver_arg))
        print("Installed '{}' from SUSE Application Collection.".format(appcollection_chart))
    else:
        print("SUSE Application Collection repo added.")
        print("List available charts with:")
        print("  helm search repo {}".format(rel))


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
    cfg = definition.get("appcollection", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_appcollection_repo(vm_name, appcollection_rel=cfg.get("appcollection_rel"),
                              appcollection_repo_url=cfg.get("appcollection_repo_url"))
    setup_appcollection(
        vm_name, appcollection_rel=cfg.get("appcollection_rel"), appcollection_ns=cfg.get("appcollection_ns"),
        appcollection_chart=cfg.get("appcollection_chart"), appcollection_release=cfg.get("appcollection_release"),
        appcollection_version=cfg.get("appcollection_version"),
    )


if __name__ == "__main__":
    main()
