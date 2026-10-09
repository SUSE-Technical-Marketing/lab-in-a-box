#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install StackState Kubernetes Agent
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.1
#
# JSON section: "stackpack" — configurable keys:
#   stackpack_api_key         : [MANDATORY] StackState API key for this cluster
#   stackpack_url             : [MANDATORY] StackState receiver URL
#                               (e.g. https://stackstate.mydemo.lab:8080/receiver/sinks/topology)
#   stackpack_cluster_name    : [OPTIONAL] name reported to StackState; when unset, the kcluster's name
#   stackpack_version         : [OPTIONAL] Helm chart version (empty = latest, e.g. "1.0.8")
#   stackpack_ns              : [OPTIONAL] namespace (default: stackstate)
#   stackpack_rel             : [OPTIONAL] Helm repo alias (default: stackstate)
#   stackpack_repo_url        : [OPTIONAL] Helm repo URL (default: https://helm.stackstate.io)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "stackpack",
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
    v.vreq("stackpack", "stackpack_api_key")
    v.vreq("stackpack", "stackpack_url")
    v.vns("stackpack")
    v.vver("stackpack")


def setup_stackpack_repo(hostname, stackpack_rel=None, stackpack_repo_url=None):
    """Add the StackState Helm repo. Mirrors setup_stackpack_repo (bash)."""
    helm_repo_add(hostname, stackpack_rel or "stackstate", stackpack_repo_url or "https://helm.stackstate.io")


def setup_stackpack(hostname, clu_name, stackpack_api_key=None, stackpack_url=None, stackpack_rel=None,
                     stackpack_ns=None, stackpack_version=None, stackpack_cluster_name=None):
    """Install the StackState Kubernetes Agent. Mirrors setup_stackpack (bash)."""
    if not stackpack_api_key:
        print("ERROR: stackpack_api_key is mandatory. Set it in the JSON 'stackpack' section.", file=sys.stderr)
        sys.exit(1)
    if not stackpack_url:
        print("ERROR: stackpack_url is mandatory. Set it in the JSON 'stackpack' section.", file=sys.stderr)
        sys.exit(1)

    rel = stackpack_rel or "stackstate"
    ns = stackpack_ns or "stackstate"
    ver_arg = "--version {}".format(stackpack_version) if stackpack_version else ""
    cluster = stackpack_cluster_name or clu_name

    ssh_run(hostname,
            "helm upgrade -i stackstate-k8s-agent {}/stackstate-k8s-agent --namespace {} --create-namespace "
            "--set-string global.receiverApiKey={} "
            "--set-string global.stackstate.url={} "
            "--set-string global.cluster.name={} "
            "{}".format(rel, ns, stackpack_api_key, stackpack_url, cluster, ver_arg))
    print("StackState K8s Agent installed. Namespace: {}".format(ns))
    print("Reporting cluster '{}' to: {}".format(cluster, stackpack_url))


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
    cfg = definition.get("stackpack", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_stackpack_repo(vm_name, stackpack_rel=cfg.get("stackpack_rel"), stackpack_repo_url=cfg.get("stackpack_repo_url"))
    setup_stackpack(
        vm_name, clu_name,
        stackpack_api_key=cfg.get("stackpack_api_key"), stackpack_url=cfg.get("stackpack_url"),
        stackpack_rel=cfg.get("stackpack_rel"), stackpack_ns=cfg.get("stackpack_ns"),
        stackpack_version=cfg.get("stackpack_version"), stackpack_cluster_name=cfg.get("stackpack_cluster_name"),
    )


if __name__ == "__main__":
    main()
