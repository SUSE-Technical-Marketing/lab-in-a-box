#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Agones (dedicated game-server hosting/scaling for Kubernetes)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "agones" — configurable keys:
#   agones_version : [OPTIONAL] Helm chart version (empty = latest, e.g. "1.60.0")
#   agones_ns      : [OPTIONAL] namespace (default: agones-system)
#   agones_rel     : [OPTIONAL] Helm repo alias (default: agones)
#   agones_repo_url: [OPTIONAL] Helm repo URL (default: https://agones.dev/chart/stable)
#
# Agones (agones.dev, a Google/OSS project — no SUSE involvement) manages dedicated game-server
# processes as Kubernetes resources: scheduling, health-checking, and scaling GameServer/Fleet
# objects. Production deployments normally schedule Agones onto a DEDICATED node pool (tainted/
# labeled agones.dev/agones-system=true) — not realistic to require in a small lab, so this addon
# deliberately does NOT attempt to taint/label any node; Agones' own chart falls back to running on
# ordinary nodes when no dedicated pool exists (its own documented behavior, not a workaround).
#
# Chart repo and name, and the fallback behavior on ordinary nodes, follow agones.dev's install docs
# and the chart's README.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "agones",
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
    v.vns("agones")
    v.vver("agones")


def setup_agones_repo(hostname, agones_rel=None, agones_repo_url=None):
    """Add the Agones Helm repo."""
    helm_repo_add(hostname, agones_rel or "agones", agones_repo_url or "https://agones.dev/chart/stable")


def setup_agones(hostname, agones_rel=None, agones_ns=None, agones_version=None):
    """Install Agones."""
    rel = agones_rel or "agones"
    ns = agones_ns or "agones-system"
    ver_arg = "--version {}".format(shlex.quote(agones_version)) if agones_version else ""

    ssh_run(hostname,
            "helm upgrade -i agones {}/agones --namespace {} --create-namespace "
            "{}".format(rel, ns, ver_arg))

    print("Agones installed. Namespace: {}".format(ns))
    print("Define GameServer/Fleet resources to run dedicated game-server workloads.")
    print("Docs: https://agones.dev/site/docs/")


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
    cfg = definition.get("agones", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_agones_repo(vm_name, agones_rel=cfg.get("agones_rel"), agones_repo_url=cfg.get("agones_repo_url"))
    setup_agones(vm_name, agones_rel=cfg.get("agones_rel"), agones_ns=cfg.get("agones_ns"),
                 agones_version=cfg.get("agones_version"))


if __name__ == "__main__":
    main()
