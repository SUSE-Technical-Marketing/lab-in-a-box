#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Rancher
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "harvester" — SUSE Virtualization (Harvester/KubeVirt) on Kubernetes
#   NOTE: section keys use the "virt_" prefix, not "harvester_".
#
#   virt_namespace  : [OPTIONAL] Kubernetes namespace                  (default: harvester-system)
#   virt_helm_rel   : [OPTIONAL] Helm release name                    (default: harvester)
#   virt_repo_name  : [OPTIONAL] Helm repo alias                      (default: harvester)
#   virt_repo_url   : [OPTIONAL] Helm repo URL                        (default: https://charts.harvesterhci.io)
#   virt_version    : [OPTIONAL] Helm chart version                   (empty = latest)
#   virt_replicas   : [OPTIONAL] Number of replicas                   (default: 1)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "harvester",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run, die  # noqa: E402


def _validate(v):
    pass  # bash's --validate block defines the usual helpers but never calls any of them


def setup_virt_repo(hostname, virt_repo_name=None, virt_repo_url=None):
    """Add the SUSE Virtualization (Harvester) Helm repo. Mirrors setup_virt_repo (bash)."""
    helm_repo_add(hostname, virt_repo_name or "harvester", virt_repo_url or "https://charts.harvesterhci.io")


def setup_suse_virtualization(hostname, clu_name, virt_namespace=None, virt_helm_rel=None,
                               virt_repo_name=None, virt_version=None, virt_replicas=None):
    """
    Install SUSE Virtualization (Harvester) CRDs then the main chart, and
    wait for it to become ready. Mirrors setup_suse_virtualization (bash).
    """
    print("# Setup SUSE Virtualization ({}) in cluster \"{}\"".format(virt_helm_rel or "harvester", clu_name))

    ns = virt_namespace or "harvester-system"
    rel = virt_helm_rel or "harvester"
    repo = virt_repo_name or "harvester"
    ver_arg = "--version {}".format(virt_version) if virt_version else ""

    result = ssh_run(hostname,
                      "helm upgrade -i {}-crd {}/harvester-crd --create-namespace --namespace '{}' {}".format(
                          rel, repo, ns, ver_arg), check=False)
    if result.returncode != 0:
        die("Failed to install Virtualization CRDs")

    result = ssh_run(hostname,
                      "helm upgrade -i {} {}/harvester --create-namespace --namespace '{}' {} "
                      "--set replicas={}".format(rel, repo, ns, ver_arg, virt_replicas or "1"), check=False)
    if result.returncode != 0:
        die("Failed to install Virtualization Chart")

    print("Wait for SUSE Virtualization controllers to be ready")
    ssh_run(hostname,
            "kubectl wait pods -n '{}' -l app.kubernetes.io/instance={} --for condition=Ready "
            "--timeout=300s 2>/dev/null".format(ns, rel), check=False)

    print("# SUSE Virtualization successfully deployed.")


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    # NOTE: unlike almost every other addon, bash NEVER scans for a node
    # here — it calls load_vm_vars with whatever _vm_name/clu_name it
    # inherited from the environment (set by setup_lab.sh's cluster-addon
    # invocation: `_vm_name=... clu_name=... install_harvester "$json"`).
    # There is no fallback if these are unset. Matched exactly: read from
    # the environment, don't scan.
    vm_name = os.environ.get("_vm_name", "")
    clu_name = os.environ.get("clu_name", "")
    node_cfg = definition.get("nodes", {}).get(vm_name, {})

    # Support both KUBERNETES_NODE_TYPE (new) and INSTALL_RKE2_TYPE (deprecated)
    node_type = node_cfg.get("KUBERNETES_NODE_TYPE") or node_cfg.get("INSTALL_RKE2_TYPE", "")
    if node_type in ("server", ""):
        print("Using node: \"{}\"".format(vm_name))
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        cfg = definition.get("harvester", {}) or {}
        online = definition.get("common", {}).get("online") == "1"

        setup_helm(vm_name, clu_name, online=online)
        print("Setup_virt_repo")
        setup_virt_repo(vm_name, virt_repo_name=cfg.get("virt_repo_name"), virt_repo_url=cfg.get("virt_repo_url"))
        setup_suse_virtualization(
            vm_name, clu_name, virt_namespace=cfg.get("virt_namespace"), virt_helm_rel=cfg.get("virt_helm_rel"),
            virt_repo_name=cfg.get("virt_repo_name"), virt_version=cfg.get("virt_version"),
            virt_replicas=cfg.get("virt_replicas"),
        )


if __name__ == "__main__":
    main()
