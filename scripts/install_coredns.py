#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a standalone CoreDNS instance
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "coredns" — configurable keys:
#   coredns_version     : [OPTIONAL] Helm chart version (empty = latest, e.g. "1.29.0")
#   coredns_ns          : [OPTIONAL] namespace (default: coredns)
#   coredns_rel         : [OPTIONAL] Helm repo alias (default: coredns)
#   coredns_repo_url    : [OPTIONAL] Helm repo URL (default: https://coredns.github.io/helm)
#
# NOTE: Kubernetes clusters already run CoreDNS in kube-system. This installs an
# additional standalone instance in its own namespace (useful for custom DNS zones,
# split-horizon DNS, or forwarding experiments).

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "coredns",
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
    v.vns("coredns")
    v.vver("coredns")
    v.vurl("coredns")


def setup_coredns_repo(hostname, coredns_rel=None, coredns_repo_url=None):
    """Add the CoreDNS Helm repo. Mirrors setup_coredns_repo (bash)."""
    helm_repo_add(hostname, coredns_rel or "coredns", coredns_repo_url or "https://coredns.github.io/helm")


def setup_coredns(hostname, coredns_rel=None, coredns_ns=None, coredns_version=None):
    """Install standalone CoreDNS. Mirrors setup_coredns (bash)."""
    rel = coredns_rel or "coredns"
    ns = coredns_ns or "coredns"
    ver_arg = "--version {}".format(coredns_version) if coredns_version else ""
    ssh_run(hostname,
            "helm upgrade -i coredns {}/coredns --namespace {} --create-namespace "
            "--set service.clusterIP='' {}".format(rel, ns, ver_arg))
    print("CoreDNS standalone instance installed. Namespace: {}".format(ns))


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
    coredns_cfg = definition.get("coredns", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_coredns_repo(vm_name, coredns_rel=coredns_cfg.get("coredns_rel"),
                        coredns_repo_url=coredns_cfg.get("coredns_repo_url"))
    setup_coredns(vm_name, coredns_rel=coredns_cfg.get("coredns_rel"),
                  coredns_ns=coredns_cfg.get("coredns_ns"), coredns_version=coredns_cfg.get("coredns_version"))


if __name__ == "__main__":
    main()
