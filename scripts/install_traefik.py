#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Traefik Ingress Controller
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "traefik" — configurable keys:
#   traefik_version     : Helm chart version (empty = latest, e.g. "28.3.0")
#   traefik_ns          : namespace (default: traefik)
#   traefik_rel         : Helm repo alias (default: traefik)
#   traefik_repo_url    : Helm repo URL (default: https://traefik.github.io/charts)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "traefik",
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
    v.vns("traefik")
    v.vver("traefik")
    v.vurl("traefik")


def setup_traefik_repo(hostname, traefik_rel=None, traefik_repo_url=None):
    """Add the Traefik Helm repo. Mirrors setup_traefik_repo (bash)."""
    helm_repo_add(hostname, traefik_rel or "traefik", traefik_repo_url or "https://traefik.github.io/charts")


def setup_traefik(hostname, traefik_rel=None, traefik_ns=None, traefik_version=None):
    """
    Install a standalone Traefik Helm release. Mirrors setup_traefik (bash).

    Distinct from k8s.setup_traefik_rke2/_k3s, which reconfigure a cluster's
    BUNDLED Traefik ingress — this addon installs a separate Traefik release
    in its own namespace via the upstream chart.
    """
    rel = traefik_rel or "traefik"
    ns = traefik_ns or "traefik"
    ver_arg = "--version {}".format(traefik_version) if traefik_version else ""
    ssh_run(hostname,
            "helm upgrade -i traefik {}/traefik --namespace {} --create-namespace {}".format(rel, ns, ver_arg))
    print("Traefik installed. Namespace: {}".format(ns))


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
    cfg = definition.get("traefik", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_traefik_repo(vm_name, traefik_rel=cfg.get("traefik_rel"), traefik_repo_url=cfg.get("traefik_repo_url"))
    setup_traefik(vm_name, traefik_rel=cfg.get("traefik_rel"), traefik_ns=cfg.get("traefik_ns"),
                  traefik_version=cfg.get("traefik_version"))


if __name__ == "__main__":
    main()
