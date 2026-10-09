#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUSE Phoebe AI resource optimizer
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "phoebe" — configurable keys:
#   phoebe_version      : [OPTIONAL] release tag to deploy, e.g. "v0.1.0" (default: latest)
#   phoebe_ns           : [OPTIONAL] namespace (default: phoebe-system)
#   phoebe_repo_url     : [OPTIONAL] base URL for manifests
#                         (default: https://github.com/SUSE/phoebe/releases/latest/download)
#
# NOTE: Phoebe is a SUSE research project for AI-driven Kubernetes resource recommendations.
# It may not have stable releases. Check https://github.com/SUSE/phoebe for current status.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "phoebe",
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
from lab_creation import ssh_run  # noqa: E402


def _validate(v):
    v.vns("phoebe")
    v.vver("phoebe")


def setup_phoebe(hostname, phoebe_ns=None, phoebe_version=None, phoebe_repo_url=None):
    """Install Phoebe via kubectl apply. Mirrors setup_phoebe (bash)."""
    ns = phoebe_ns or "phoebe-system"
    base = phoebe_repo_url or "https://github.com/SUSE/phoebe/releases/latest/download"

    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)

    if phoebe_version:
        base = "https://github.com/SUSE/phoebe/releases/download/{}".format(phoebe_version)

    ssh_run(hostname, "kubectl apply -n {} -f {}/phoebe.yaml".format(ns, base))
    print("Phoebe installed. Namespace: {}".format(ns))
    print("Check status: kubectl -n {} get pods".format(ns))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    # Mirrors `on_first_server setup_phoebe` (bash): always targets the first
    # server node found in the definition — note that bash's on_first_server
    # uses `_vm_name` as its OWN loop variable, so it ignores/overwrites any
    # _vm_name the caller (setup_lab.sh) already exported; matched here by
    # never consulting the environment for a target node.
    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    phoebe_cfg = definition.get("phoebe", {}) or {}
    setup_phoebe(
        vm_name,
        phoebe_ns=phoebe_cfg.get("phoebe_ns"),
        phoebe_version=phoebe_cfg.get("phoebe_version"),
        phoebe_repo_url=phoebe_cfg.get("phoebe_repo_url"),
    )


if __name__ == "__main__":
    main()
