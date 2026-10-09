#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Fluentd log aggregator
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "fluentd" — configurable keys:
#   fluentd_version     : Helm chart version (empty = latest, e.g. "0.5.2")
#   fluentd_ns          : namespace (default: fluentd)
#   fluentd_rel         : Helm repo alias (default: fluent)
#   fluentd_repo_url    : Helm repo URL (default: https://fluent.github.io/helm-charts)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "fluentd",
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
    v.vns("fluentd")
    v.vver("fluentd")
    v.vurl("fluentd")


def setup_fluentd_repo(hostname, fluentd_rel=None, fluentd_repo_url=None):
    """Add the Fluent Helm repo. Mirrors setup_fluentd_repo (bash)."""
    helm_repo_add(hostname, fluentd_rel or "fluent", fluentd_repo_url or "https://fluent.github.io/helm-charts")


def setup_fluentd(hostname, fluentd_rel=None, fluentd_ns=None, fluentd_version=None):
    """Install Fluentd. Mirrors setup_fluentd (bash)."""
    rel = fluentd_rel or "fluent"
    ns = fluentd_ns or "fluentd"
    ver_arg = "--version {}".format(fluentd_version) if fluentd_version else ""
    ssh_run(hostname,
            "helm upgrade -i fluentd {}/fluentd --namespace {} --create-namespace {}".format(rel, ns, ver_arg))
    print("Fluentd installed. Namespace: {}".format(ns))


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
    cfg = definition.get("fluentd", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_fluentd_repo(vm_name, fluentd_rel=cfg.get("fluentd_rel"), fluentd_repo_url=cfg.get("fluentd_repo_url"))
    setup_fluentd(vm_name, fluentd_rel=cfg.get("fluentd_rel"), fluentd_ns=cfg.get("fluentd_ns"),
                  fluentd_version=cfg.get("fluentd_version"))


if __name__ == "__main__":
    main()
