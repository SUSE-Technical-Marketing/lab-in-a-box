#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Qdrant (vector database, for RAG/embedding-search demos)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "qdrant" — configurable keys:
#   qdrant_version    : [OPTIONAL] Helm chart version (empty = latest)
#   qdrant_ns         : [OPTIONAL] namespace (default: qdrant)
#   qdrant_rel        : [OPTIONAL] Helm repo alias (default: qdrant)
#   qdrant_repo_url   : [OPTIONAL] Helm repo URL (default: https://qdrant.github.io/qdrant-helm)
#   qdrant_replicas   : [OPTIONAL] StatefulSet replica count (default: 1 — chart default)
#
# Chart repo and version follow qdrant/qdrant-helm's README. The chart's README notes that it needs
# Kubernetes v1.24+ (gRPC readiness probe support) and a PersistentVolume provisioner. Both are already
# assumed by the RKE2 and K3s addons of this project.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "qdrant",
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
    v.vns("qdrant")
    v.vver("qdrant")


def setup_qdrant_repo(hostname, qdrant_rel=None, qdrant_repo_url=None):
    """Add the Qdrant Helm repo."""
    helm_repo_add(hostname, qdrant_rel or "qdrant", qdrant_repo_url or "https://qdrant.github.io/qdrant-helm")


def setup_qdrant(hostname, qdrant_rel=None, qdrant_ns=None, qdrant_version=None, qdrant_replicas=None):
    """Install Qdrant."""
    rel = qdrant_rel or "qdrant"
    ns = qdrant_ns or "qdrant"
    ver_arg = "--version {}".format(shlex.quote(qdrant_version)) if qdrant_version else ""
    replicas_arg = "--set replicaCount={}".format(int(qdrant_replicas)) if qdrant_replicas else ""

    ssh_run(hostname,
            "helm upgrade -i qdrant {}/qdrant --namespace {} --create-namespace "
            "{} {}".format(rel, ns, replicas_arg, ver_arg))

    print("Qdrant installed. Namespace: {}".format(ns))
    print("In-cluster endpoint: qdrant.{}.svc.cluster.local:6333 (REST), :6334 (gRPC)".format(ns))


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
    cfg = definition.get("qdrant", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_qdrant_repo(vm_name, qdrant_rel=cfg.get("qdrant_rel"), qdrant_repo_url=cfg.get("qdrant_repo_url"))
    setup_qdrant(vm_name, qdrant_rel=cfg.get("qdrant_rel"), qdrant_ns=cfg.get("qdrant_ns"),
                 qdrant_version=cfg.get("qdrant_version"), qdrant_replicas=cfg.get("qdrant_replicas"))


if __name__ == "__main__":
    main()
