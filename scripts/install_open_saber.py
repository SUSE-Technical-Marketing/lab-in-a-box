#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will self-host Open Saber (an open-source rhythm/block-cutting game)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/leandrodreamer/BeepSaber, also listed at
# https://leandrodreamer.itch.io/open-saber
#
# JSON section: "open_saber" — configurable keys:
#   open_saber_ns      : [OPTIONAL] namespace (default: open-saber)
#   open_saber_shorthn : [OPTIONAL] hostname prefix for ingress (default: open-saber)
#   open_saber_version : [OPTIONAL] release tag to fetch (default: v0.5.0)
#
# LICENSE: MIT. Self-hosting the game is permitted.
#
# This deploys the project's own real WebXR/web export, published as a GitHub Release asset
# (OpenSaber<version>.WebXR.zip.zip — note the double ".zip" in the real filename itself, not a
# typo here) — a Kubernetes initContainer downloads it and unzips it (handling the outer/inner zip
# nesting defensively) into a shared volume; an ordinary nginx container serves it. No Godot build
# toolchain needed; this is the developer's own already-exported web build.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "open_saber",
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
apk add --no-cache curl unzip findutils >/tmp/fetch.log 2>&1
curl -fsSL "$1" -o /tmp/g.zip
mkdir -p /tmp/e1
unzip -oq /tmp/g.zip -d /tmp/e1
inner=$(find /tmp/e1 -maxdepth 2 -iname '*.zip' | head -n1)
if [ -n "$inner" ]; then
  unzip -oq "$inner" -d /shared
else
  cp -a /tmp/e1/. /shared/
fi
if [ ! -f /shared/index.html ]; then
  d=$(find /shared -maxdepth 3 -iname index.html | head -n1)
  if [ -n "$d" ]; then cp -a "$(dirname "$d")/." /shared/; fi
fi
rm -rf /tmp/g.zip /tmp/e1
""".strip()


def _validate(v):
    v.vns("open_saber")


def setup_open_saber(hostname, clu_name, mydomain, cfg):
    """Self-host Open Saber's real WebXR/web export."""
    ns = cfg.get("open_saber_ns") or "open-saber"
    shorthn = cfg.get("open_saber_shorthn") or "open-saber"
    version = cfg.get("open_saber_version") or "v0.5.0"
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)
    url = "https://github.com/leandrodreamer/BeepSaber/releases/download/{}/OpenSaber{}.WebXR.zip.zip".format(
        version, version.lstrip("v"))

    manifest = static_webgame_common.render_static_webgame(
        "open-saber", ns, host, _FETCH_SCRIPT, args=[url])
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("Open Saber deployed. Namespace: {}".format(ns))
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
    cfg = definition.get("open_saber", {}) or {}

    setup_open_saber(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
