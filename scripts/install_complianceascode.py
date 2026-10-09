#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install the ComplianceAsCode compliance operator
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "complianceascode" — configurable keys:
#   complianceascode_version : [OPTIONAL] operator version tag (empty = latest, e.g. "0.1.69")
#   complianceascode_ns      : [OPTIONAL] namespace (default: compliance-operator)
#   complianceascode_profile : [OPTIONAL] scan profile to apply (default: cis)
#                              Examples: cis, stig, pci-dss, moderate
#   complianceascode_rel     : [OPTIONAL] Helm repo alias (default: compliance-operator)
#   complianceascode_repo_url: [OPTIONAL] Helm repo URL
#                              (default: https://openshift.github.io/compliance-operator)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "complianceascode",
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
    v.vns("complianceascode")
    v.vver("complianceascode")
    v.vurl("complianceascode")


def setup_complianceascode_repo(hostname, complianceascode_rel=None, complianceascode_repo_url=None):
    """Add the ComplianceAsCode Helm repo. Mirrors setup_complianceascode_repo (bash)."""
    helm_repo_add(hostname, complianceascode_rel or "compliance-operator",
                  complianceascode_repo_url or "https://openshift.github.io/compliance-operator")


def setup_complianceascode(hostname, complianceascode_rel=None, complianceascode_ns=None,
                            complianceascode_version=None, complianceascode_profile=None):
    """Install compliance-operator. Mirrors setup_complianceascode (bash)."""
    rel = complianceascode_rel or "compliance-operator"
    ns = complianceascode_ns or "compliance-operator"
    profile = complianceascode_profile or "cis"
    ver_arg = "--version {}".format(complianceascode_version) if complianceascode_version else ""

    ssh_run(hostname,
            "helm upgrade -i compliance-operator {}/compliance-operator "
            "--namespace {} --create-namespace {}".format(rel, ns, ver_arg))

    print("ComplianceAsCode operator installed. Namespace: {}".format(ns))
    print("Default scan profile: {}".format(profile))
    print("Create scans with: kubectl -n {} apply -f your-scan.yaml".format(ns))


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
    cfg = definition.get("complianceascode", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_complianceascode_repo(vm_name, complianceascode_rel=cfg.get("complianceascode_rel"),
                                 complianceascode_repo_url=cfg.get("complianceascode_repo_url"))
    setup_complianceascode(
        vm_name, complianceascode_rel=cfg.get("complianceascode_rel"),
        complianceascode_ns=cfg.get("complianceascode_ns"),
        complianceascode_version=cfg.get("complianceascode_version"),
        complianceascode_profile=cfg.get("complianceascode_profile"),
    )


if __name__ == "__main__":
    main()
