#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install the KIWI image build operator
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "kiwi" — configurable keys:
#   kiwi_version        : [OPTIONAL] operator version tag (empty = latest, e.g. "v1.3.0")
#   kiwi_ns             : [OPTIONAL] namespace (default: kiwi-system)
#   kiwi_manifest_url   : [OPTIONAL] URL for the operator manifest
#                         (default: https://github.com/OSInside/kiwi-operator/releases/latest/download/kiwi-operator.yaml)
#
# KIWI-NG Operator runs KIWI image builds as Kubernetes Jobs.
# See: https://github.com/OSInside/kiwi-operator

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kiwi",
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
    v.vns("kiwi")
    v.vver("kiwi")


def setup_kiwi(hostname, kiwi_ns=None, kiwi_version=None, kiwi_manifest_url=None):
    """Install the KIWI operator via manifest. Mirrors setup_kiwi (bash)."""
    ns = kiwi_ns or "kiwi-system"

    if kiwi_manifest_url:
        url = kiwi_manifest_url
    elif kiwi_version:
        url = "https://github.com/OSInside/kiwi-operator/releases/download/{}/kiwi-operator.yaml".format(kiwi_version)
    else:
        url = "https://github.com/OSInside/kiwi-operator/releases/latest/download/kiwi-operator.yaml"

    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)
    ssh_run(hostname, "kubectl apply -f {}".format(url))

    print("KIWI operator installed. Namespace: {}".format(ns))
    print("Submit builds by creating KiwiJob resources in namespace: {}".format(ns))
    print("Docs: https://github.com/OSInside/kiwi-operator")


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

    kiwi_cfg = definition.get("kiwi", {}) or {}
    setup_kiwi(vm_name, kiwi_ns=kiwi_cfg.get("kiwi_ns"), kiwi_version=kiwi_cfg.get("kiwi_version"),
               kiwi_manifest_url=kiwi_cfg.get("kiwi_manifest_url"))


if __name__ == "__main__":
    main()
