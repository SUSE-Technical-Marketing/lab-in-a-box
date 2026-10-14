#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Milvus (vector database, for RAG/embedding-search demos)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "milvus" — configurable keys:
#   milvus_version    : [OPTIONAL] Helm chart version (empty = latest)
#   milvus_ns         : [OPTIONAL] namespace (default: milvus)
#   milvus_shorthn    : [OPTIONAL] hostname prefix (default: milvus)
#   milvus_rel        : [OPTIONAL] Helm repo alias (default: milvus)
#   milvus_repo_url   : [OPTIONAL] Helm repo URL (default: https://zilliztech.github.io/milvus-helm)
#   milvus_cluster    : [OPTIONAL] "true"/"false" (default: false) — false installs Milvus STANDALONE
#                       (etcd+minio embedded, single pod, the right default for a lab); "true" installs
#                       the full multi-component cluster topology, needs real resources to be worth it
#
# This is the community/upstream Milvus chart (zilliztech/milvus-helm) — SUSE's own re-packaged build
# used by the "suse_ai" addon is a SEPARATE chart (oci://dp.apps.rancher.io/charts/milvus); don't run
# both against the same cluster/namespace.
#
# Chart and standalone-mode --set flags follow zilliztech/milvus-helm's README and values.yaml.
# The milvus-io.github.io/milvus-helm repo is archived and is not used.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "milvus",
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
    v.vns("milvus")
    v.vver("milvus")
    v.vbool("milvus", "milvus_cluster")


def setup_milvus_repo(hostname, milvus_rel=None, milvus_repo_url=None):
    """Add the Milvus Helm repo."""
    helm_repo_add(hostname, milvus_rel or "milvus", milvus_repo_url or "https://zilliztech.github.io/milvus-helm")


def setup_milvus(hostname, milvus_rel=None, milvus_ns=None, milvus_version=None, milvus_cluster=None):
    """Install Milvus, standalone by default (single pod — the right lab-sized default)."""
    rel = milvus_rel or "milvus"
    ns = milvus_ns or "milvus"
    ver_arg = "--version {}".format(shlex.quote(milvus_version)) if milvus_version else ""
    cluster_enabled = "true" if str(milvus_cluster) == "true" else "false"

    standalone_args = ""
    if cluster_enabled == "false":
        standalone_args = (
            " --set cluster.enabled=false"
            " --set etcd.replicaCount=1"
            " --set minio.mode=standalone"
            " --set pulsarv3.enabled=false"
        )

    ssh_run(hostname,
            "helm upgrade -i milvus {}/milvus --namespace {} --create-namespace "
            "--set cluster.enabled={}{} "
            "{}".format(rel, ns, cluster_enabled, standalone_args, ver_arg))

    print("Milvus installed ({}). Namespace: {}".format("cluster" if cluster_enabled == "true" else "standalone", ns))
    print("In-cluster endpoint: milvus.{}.svc.cluster.local:19530".format(ns))


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
    cfg = definition.get("milvus", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_milvus_repo(vm_name, milvus_rel=cfg.get("milvus_rel"), milvus_repo_url=cfg.get("milvus_repo_url"))
    setup_milvus(vm_name, milvus_rel=cfg.get("milvus_rel"), milvus_ns=cfg.get("milvus_ns"),
                 milvus_version=cfg.get("milvus_version"), milvus_cluster=cfg.get("milvus_cluster"))


if __name__ == "__main__":
    main()
