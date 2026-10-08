#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will self-host SuperTux Classic (an open-source Godot platformer)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/Alzter/SuperTux-Classic (a from-scratch Godot remake of SuperTux
# Milestone 1), also listed at https://alzter-s.itch.io/supertux-classic
#
# JSON section: "supertux_classic" — configurable keys:
#   supertux_classic_ns      : [OPTIONAL] namespace (default: supertux-classic)
#   supertux_classic_shorthn : [OPTIONAL] hostname prefix for ingress (default: supertux-classic)
#   supertux_classic_version : [OPTIONAL] release tag to fetch (default: v0.4.2)
#
# LICENSE: GPL-3.0. The game's itch.io page states that the game and its source files may be
# redistributed, so self-hosting the game is permitted.
#
# This deploys the REAL, pre-built HTML5 export the project itself publishes as a GitHub Release
# asset (SuperTuxClassic-<version>-HTML.zip) — a Kubernetes initContainer downloads and unzips it
# into a shared volume, an ordinary nginx container serves it. No Godot build toolchain needed;
# this is the developer's own already-exported web build, not something built from source here.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "supertux_classic",
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
apk add --no-cache curl unzip >/tmp/fetch.log 2>&1
curl -fsSL "$1" -o /tmp/g.zip
unzip -oq /tmp/g.zip -d /shared
if [ ! -f /shared/index.html ]; then
  d=$(find /shared -maxdepth 3 -iname index.html | head -n1)
  if [ -n "$d" ]; then cp -a "$(dirname "$d")/." /shared/; fi
fi
rm -f /tmp/g.zip
""".strip()


def _validate(v):
    v.vns("supertux_classic")


def setup_supertux_classic(hostname, clu_name, mydomain, cfg):
    """Self-host SuperTux Classic's real HTML5 export."""
    ns = cfg.get("supertux_classic_ns") or "supertux-classic"
    shorthn = cfg.get("supertux_classic_shorthn") or "supertux-classic"
    version = cfg.get("supertux_classic_version") or "v0.4.2"
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)
    url = "https://github.com/Alzter/SuperTux-Classic/releases/download/{}/SuperTuxClassic-{}-HTML.zip".format(
        version, version.lstrip("v"))

    manifest = static_webgame_common.render_static_webgame(
        "supertux-classic", ns, host, _FETCH_SCRIPT, args=[url])
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("SuperTux Classic deployed. Namespace: {}".format(ns))
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
    cfg = definition.get("supertux_classic", {}) or {}

    setup_supertux_classic(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
