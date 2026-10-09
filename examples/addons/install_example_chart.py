#!/usr/bin/env python3.11
# Part of lab-in-a-box: example add-on that installs one Helm chart (podinfo) on a Kubernetes cluster.
# See docs/addons.html (Add-on developers' guide). Copy it to scripts/install_<name>.py to start a new add-on.
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "example_chart" — podinfo web application installed with Helm
#
#   example_chart_ns       : [OPTIONAL] Namespace                           (default: podinfo)
#   example_chart_repo_url : [OPTIONAL] Helm repo URL                       (default: https://stefanprodan.github.io/podinfo)
#   example_chart_version  : [OPTIONAL] Helm chart version                  (empty = latest, e.g. 6.15.0)
#   example_chart_replicas : [OPTIONAL] Number of replicas                  (default: 1) (min: 1) (max: 5)
#   example_chart_message  : [OPTIONAL] Message shown on the podinfo page   (default: Hello from lab-in-a-box)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "example_chart",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
    # Version matrix (libs/versions.py): chart versions with each chart's kubeVersion minimum
    # (https://stefanprodan.github.io/podinfo/index.yaml).
    "versions": {
        "example_chart_version": [
            {"version": "6.15", "kubernetes": {"rke2": {"min": "1.23"}, "k3s": {"min": "1.23"}}},
            {"version": "6.14", "kubernetes": {"rke2": {"min": "1.23"}, "k3s": {"min": "1.23"}}},
        ],
    },
}

import os
import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent.parent / "libs"),
                   str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import versions  # noqa: E402
from lab_creation import die, helm_repo_add, setup_helm, ssh_run  # noqa: E402

SECTION = "example_chart"


def _validate(v: ac.Validator) -> None:
    """--validate: the namespace and repo URL formats (the version is checked against PLUGIN["versions"])."""
    v.vns(SECTION)
    v.vurl(SECTION)


def install(node: str, cfg: dict) -> None:
    """Install or upgrade podinfo through `node`, a server node of the kcluster, and wait for its rollout."""
    ns = cfg.get("example_chart_ns") or "podinfo"
    helm_repo_add(node, "podinfo", shlex.quote(cfg.get("example_chart_repo_url") or "https://stefanprodan.github.io/podinfo"))
    ssh_run(node, "helm upgrade --install podinfo podinfo/podinfo {} --namespace {} --create-namespace "
                  "--set replicaCount={} --set ui.message={}".format(
                      versions.helm_version_flag(cfg.get("example_chart_version")), shlex.quote(ns),
                      shlex.quote(str(cfg.get("example_chart_replicas") or 1)),
                      shlex.quote(cfg.get("example_chart_message") or "Hello from lab-in-a-box")))
    ssh_run(node, "kubectl -n {} rollout status deployment/podinfo --timeout=300s".format(shlex.quote(ns)))


def main() -> None:
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)
    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    definition = primary.load_definition(sys.argv[1])
    node, clu_name = os.environ.get("_vm_name", ""), os.environ.get("clu_name", "")
    if not node or not clu_name:
        die("{}: run by setup_lab.py for a kcluster ($_vm_name and $clu_name are not set)".format(SECTION))
    online = (definition.get("common") or {}).get("online") == "1"
    setup_helm(node, clu_name, online=online)
    install(node, definition.get(SECTION) or {})
    print("podinfo installed on Kubernetes cluster {}".format(clu_name))


if __name__ == "__main__":
    main()
