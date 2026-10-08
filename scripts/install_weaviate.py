#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Weaviate (vector database, for RAG/embedding-search demos)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "weaviate" — configurable keys:
#   weaviate_version  : [OPTIONAL] Helm chart version (empty = latest)
#   weaviate_ns       : [OPTIONAL] namespace (default: weaviate)
#   weaviate_rel      : [OPTIONAL] Helm repo alias (default: weaviate)
#   weaviate_repo_url : [OPTIONAL] Helm repo URL (default: https://weaviate.github.io/weaviate-helm)
#   weaviate_replicas : [OPTIONAL] StatefulSet replica count (default: 1 — chart default)
#
# Since chart v17.1.0 the chart sets no default CPU or memory request or limit. Set resources.requests
# and resources.limits through a values override on a host without generous defaults, or the pod may
# be unbounded.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "weaviate",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
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
    v.vns("weaviate")
    v.vver("weaviate")


def setup_weaviate_repo(hostname, weaviate_rel=None, weaviate_repo_url=None):
    """Add the Weaviate Helm repo."""
    helm_repo_add(hostname, weaviate_rel or "weaviate", weaviate_repo_url or "https://weaviate.github.io/weaviate-helm")


def setup_weaviate(hostname, weaviate_rel=None, weaviate_ns=None, weaviate_version=None, weaviate_replicas=None):
    """Install Weaviate."""
    rel = weaviate_rel or "weaviate"
    ns = weaviate_ns or "weaviate"
    ver_arg = "--version {}".format(shlex.quote(weaviate_version)) if weaviate_version else ""
    replicas_arg = "--set replicas={}".format(int(weaviate_replicas)) if weaviate_replicas else ""

    ssh_run(hostname,
            "helm upgrade -i weaviate {}/weaviate --namespace {} --create-namespace "
            "{} {}".format(rel, ns, replicas_arg, ver_arg))

    print("Weaviate installed. Namespace: {}".format(ns))
    print("In-cluster endpoint: weaviate.{}.svc.cluster.local:80".format(ns))


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
    cfg = definition.get("weaviate", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_weaviate_repo(vm_name, weaviate_rel=cfg.get("weaviate_rel"), weaviate_repo_url=cfg.get("weaviate_repo_url"))
    setup_weaviate(vm_name, weaviate_rel=cfg.get("weaviate_rel"), weaviate_ns=cfg.get("weaviate_ns"),
                   weaviate_version=cfg.get("weaviate_version"), weaviate_replicas=cfg.get("weaviate_replicas"))


if __name__ == "__main__":
    main()
