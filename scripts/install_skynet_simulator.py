#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will self-host Skynet Simulator (an open-source browser puzzle/idle game)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/edisgreat/skynet-simulator, also listed at
# https://edisgreat.itch.io/skynet-simulator
#
# Schema version: 1.0
#
# JSON section: "skynet_simulator" — configurable keys:
#   skynet_simulator_ns      : [OPTIONAL] namespace (default: skynet-simulator)
#   skynet_simulator_shorthn : [OPTIONAL] hostname prefix for ingress (default: skynet-simulator)
#
# LICENSE: MIT. Self-hosting the game is permitted.
#
# This deploys the game's own real files directly from its git repository (a plain static HTML/
# CSS/JS app with index.html, css/ and script/ at the repo root and no build step) via a
# Kubernetes initContainer that downloads the repo's own GitHub archive tarball and extracts it
# into a shared volume; an ordinary nginx container serves it. The repo also ships a small optional
# PHP/MySQL logging feature (update.php/log.sql) — deliberately NOT wired up here (no PHP/DB
# runtime in this addon's nginx-only pod); the game itself works fully without it.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "skynet_simulator",
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
import static_webgame_common  # noqa: E402
from lab_creation import ssh_run  # noqa: E402

_FETCH_SCRIPT = """
set -e
apk add --no-cache curl tar >/tmp/fetch.log 2>&1
curl -fsSL "https://github.com/edisgreat/skynet-simulator/archive/refs/heads/master.tar.gz" -o /tmp/g.tar.gz
mkdir -p /tmp/extract
tar -xzf /tmp/g.tar.gz -C /tmp/extract
cp -a /tmp/extract/*/. /shared/
rm -rf /tmp/g.tar.gz /tmp/extract
""".strip()


def _validate(v):
    v.vns("skynet_simulator")


def setup_skynet_simulator(hostname, clu_name, mydomain, cfg):
    """Self-host Skynet Simulator's real static files."""
    ns = cfg.get("skynet_simulator_ns") or "skynet-simulator"
    shorthn = cfg.get("skynet_simulator_shorthn") or "skynet-simulator"
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)

    manifest = static_webgame_common.render_static_webgame(
        "skynet-simulator", ns, host, _FETCH_SCRIPT)
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("Skynet Simulator deployed. Namespace: {}".format(ns))
    print("Available at: http://{}".format(host))


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
    cfg = definition.get("skynet_simulator", {}) or {}

    setup_skynet_simulator(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
