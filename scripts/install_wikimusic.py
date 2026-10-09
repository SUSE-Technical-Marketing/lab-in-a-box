#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install WikiMusic (a musical-knowledge encyclopaedia/CMS on GNU Artanis)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://codeberg.org/jjba23/wikimusic (jjba23, docs at jointhefreeworld.org)
#
# Schema version: 1.0
#
# JSON section: "wikimusic" — configurable keys:
#   wikimusic_image   : [MANDATORY] a pre-built image reference for WikiMusic. Unlike Colt,
#                       WikiMusic's own repo has NO Dockerfile at all —
#                       it's built via GNU Guix (channels.scm/manifest.scm) for reproducible
#                       builds. Build one yourself first, e.g. via Guix's own Docker-image export:
#                         git clone https://codeberg.org/jjba23/wikimusic.git && cd wikimusic
#                         guix pack -f docker -m manifest.scm   # produces a loadable tarball
#                       then load/push it to a registry this cluster can pull from (this project's
#                       own "harbor" addon is one option).
#   wikimusic_ns      : [OPTIONAL] namespace (default: wikimusic)
#   wikimusic_shorthn : [OPTIONAL] hostname prefix for ingress (default: wikimusic)
#   wikimusic_port    : [OPTIONAL] container port WikiMusic listens on, Artanis' own default
#                       (default: 3000)
#
# Uses the shared Artanis-app manifest shape (libs/artanis_common.py, also used by install_colt.py).
# WikiMusic is database-backed (SQLite, per its own resources/migrations/sqlite directory) rather
# than git-backed like Colt — the shared manifest's data_path is pointed at where that database
# file needs to persist.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "wikimusic",
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
import artanis_common  # noqa: E402
from lab_creation import ssh_run  # noqa: E402


def _validate(v):
    v.vreq("wikimusic", "wikimusic_image")
    v.vns("wikimusic")
    v.vport("wikimusic", "wikimusic_port")


def setup_wikimusic(hostname, clu_name, mydomain, cfg):
    """Deploy WikiMusic via the shared Artanis-app manifest shape."""
    image = cfg.get("wikimusic_image")
    if not image:
        print("ERROR: wikimusic_image is mandatory — see this script's own header comment for how "
              "to build one via Guix.", file=sys.stderr)
        sys.exit(1)

    ns = cfg.get("wikimusic_ns") or "wikimusic"
    shorthn = cfg.get("wikimusic_shorthn") or "wikimusic"
    port = int(cfg.get("wikimusic_port") or 3000)
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)

    manifest = artanis_common.render_artanis_app("wikimusic", image, ns, host, port=port, data_path="/app/db")
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("WikiMusic deployed. Namespace: {}".format(ns))
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
    cfg = definition.get("wikimusic", {}) or {}

    setup_wikimusic(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
