#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install NeuVector
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "neuvector" — SUSE NeuVector container security platform
#
#   nv_shorthn   : [OPTIONAL] Short hostname for the manager UI ingress (default: neuvector)
#   nv_rel       : [OPTIONAL] Helm repo alias                           (default: neuvector)
#   nv_repo_url  : [OPTIONAL] Helm repo URL                            (default: https://neuvector.github.io/neuvector-helm)
#   nv_version   : [OPTIONAL] Helm chart version                        (empty = latest)

__version__ = "526bc48"

PLUGIN = {
    "name": "neuvector",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vns("neuvector")
    v.vver("neuvector")
    v.vurl("neuvector")


def setup_nv_repo(hostname, nv_rel=None, nv_repo_url=None):
    """Add the SUSE NeuVector Helm repo. Mirrors setup_nv_repo (bash)."""
    helm_repo_add(hostname, nv_rel or "neuvector", nv_repo_url or "https://neuvector.github.io/neuvector-helm")


def setup_nv(hostname, clu_name, mydomain, rancher_shorthn, nv_shorthn=None):
    """Install SUSE NeuVector. Mirrors setup_nv (bash)."""
    fqdn = "{}.{}.{}".format(nv_shorthn or "neuvector", clu_name, mydomain)
    rancher_url = "https://{}.{}.{}".format(rancher_shorthn, clu_name, mydomain)

    ssh_run(hostname, "kubectl create namespace cattle-neuvector-system")
    ssh_run(hostname,
            "helm upgrade -i neuvector neuvector/core --namespace cattle-neuvector-system "
            "--set k3s.enabled=true "
            "--set k3s.runtimePath=/run/k3s/containerd/containerd.sock "
            "--set manager.ingress.enabled=true "
            "--set manager.svc.type=ClusterIP "
            "--set controller.pvc.enabled=true "
            "--set manager.ingress.host={} "
            "--set global.cattle.url={} "
            "--set controller.ranchersso.enabled=true "
            "--set rbac=true".format(fqdn, rancher_url))
    print("NeuVector should be available in a few minutes in: {}".format(fqdn))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    nv_cfg = definition.get("neuvector", {}) or {}
    rancher_shorthn = (definition.get("rancher", {}) or {}).get("rancher_shorthn", "")
    online = definition.get("common", {}).get("online") == "1"

    # NOTE: unlike most addons, bash's loop here has no break/exit after a
    # match — it runs setup_helm/setup_nv_repo/setup_nv on EVERY node whose
    # KUBERNETES_NODE_TYPE (or deprecated INSTALL_RKE2_TYPE) is "server" or unset,
    # not just the first one found. That means multiple server nodes (HA control
    # planes, or multiple clusters in the same lab) each get NeuVector installed independently.
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        # Support both KUBERNETES_NODE_TYPE (new) and INSTALL_RKE2_TYPE (deprecated)
        node_type = node_cfg.get("KUBERNETES_NODE_TYPE") or node_cfg.get("INSTALL_RKE2_TYPE", "")
        if node_type not in ("server", ""):
            continue
        print("# Using node: {}".format(vm_name))
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}

        setup_helm(vm_name, clu_name, online=online)
        setup_nv_repo(vm_name, nv_rel=nv_cfg.get("nv_rel"), nv_repo_url=nv_cfg.get("nv_repo_url"))
        setup_nv(vm_name, clu_name, clu_cfg.get("mydomain", ""), rancher_shorthn,
                 nv_shorthn=nv_cfg.get("nv_shorthn"))
        time.sleep(60)


if __name__ == "__main__":
    main()
