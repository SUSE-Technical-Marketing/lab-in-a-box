#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install kagent (Kubernetes AI Agent framework)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "kagent" — configurable keys:
#   kagent_version      : [OPTIONAL] Helm chart version (empty = latest)
#   kagent_ns           : [OPTIONAL] namespace (default: kagent-system)
#   kagent_shorthn      : [OPTIONAL] hostname prefix (default: kagent)
#   kagent_rel          : [OPTIONAL] Helm repo alias (default: kagent)
#   kagent_repo_url     : [OPTIONAL] Helm repo URL (default: https://kagent-ai.github.io/helm-charts)
#
# NOTE: kagent is a new CNCF project. The Helm chart URL may change as the project
# matures. Check https://github.com/kagent-ai/kagent for the latest install instructions.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kagent",
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
    v.vns("kagent")
    v.vver("kagent")
    v.vurl("kagent")


def setup_kagent_repo(hostname, kagent_rel=None, kagent_repo_url=None):
    """Add the kagent Helm repo. Mirrors setup_kagent_repo (bash)."""
    helm_repo_add(hostname, kagent_rel or "kagent", kagent_repo_url or "https://kagent-ai.github.io/helm-charts")


def setup_kagent(hostname, clu_name, mydomain, kagent_rel=None, kagent_ns=None,
                  kagent_version=None, kagent_shorthn=None):
    """Install kagent. Mirrors setup_kagent (bash)."""
    rel = kagent_rel or "kagent"
    ns = kagent_ns or "kagent-system"
    ver_arg = "--version {}".format(kagent_version) if kagent_version else ""
    ssh_run(hostname,
            "helm upgrade -i kagent {}/kagent --namespace {} --create-namespace {}".format(rel, ns, ver_arg))
    print("kagent installed. Namespace: {}".format(ns))
    print("UI available at: http://{}.{}.{}".format(kagent_shorthn or "kagent", clu_name, mydomain))


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
    kagent_cfg = definition.get("kagent", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_kagent_repo(vm_name, kagent_rel=kagent_cfg.get("kagent_rel"), kagent_repo_url=kagent_cfg.get("kagent_repo_url"))
    setup_kagent(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        kagent_rel=kagent_cfg.get("kagent_rel"), kagent_ns=kagent_cfg.get("kagent_ns"),
        kagent_version=kagent_cfg.get("kagent_version"), kagent_shorthn=kagent_cfg.get("kagent_shorthn"),
    )


if __name__ == "__main__":
    main()
