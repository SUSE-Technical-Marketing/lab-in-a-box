#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Colt (a git-backed blog engine on GNU Artanis)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://gitlab.com/NalaGinrut/colt (NalaGinrut/Roy Mu, GPLv3)
#
# JSON section: "colt" — configurable keys:
#   colt_image   : [MANDATORY] a pre-built image reference for Colt. Colt's own GitLab repo has a
#                  Dockerfile at its root but publishes no image
#                  to any registry — build and push one yourself first, e.g.:
#                    git clone https://gitlab.com/NalaGinrut/colt.git && cd colt
#                    docker build -t <your-registry>/colt:latest . && docker push <your-registry>/colt:latest
#                  (this project's own "harbor" addon is one option for <your-registry>)
#   colt_ns      : [OPTIONAL] namespace (default: colt)
#   colt_shorthn : [OPTIONAL] hostname prefix for ingress (default: colt)
#   colt_port    : [OPTIONAL] container port Colt listens on (default: 3000, Artanis' own default)
#
# Uses the shared Artanis-app manifest shape (libs/artanis_common.py, also used by
# install_wikimusic.py) — a single pod (git-backed content, no external database needed) with an
# Ingress. See artanis_common.py's own module docstring for why colt_image is mandatory rather
# than defaulting to a published image.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "colt",
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
    v.vreq("colt", "colt_image")
    v.vns("colt")
    v.vport("colt", "colt_port")


def setup_colt(hostname, clu_name, mydomain, cfg):
    """Deploy Colt via the shared Artanis-app manifest shape."""
    image = cfg.get("colt_image")
    if not image:
        print("ERROR: colt_image is mandatory — see this script's own header comment for how to "
              "build one from Colt's own Dockerfile.", file=sys.stderr)
        sys.exit(1)

    ns = cfg.get("colt_ns") or "colt"
    shorthn = cfg.get("colt_shorthn") or "colt"
    port = int(cfg.get("colt_port") or 3000)
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)

    manifest = artanis_common.render_artanis_app("colt", image, ns, host, port=port, data_path="/app/data")
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("Colt deployed. Namespace: {}".format(ns))
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
    cfg = definition.get("colt", {}) or {}

    setup_colt(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
