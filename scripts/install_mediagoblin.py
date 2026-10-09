#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install GNU MediaGoblin (federated media-publishing platform)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "mediagoblin" — configurable keys:
#   mediagoblin_ns             : [OPTIONAL] namespace (default: mediagoblin)
#   mediagoblin_shorthn        : [OPTIONAL] hostname prefix for ingress (default: mediagoblin)
#   mediagoblin_version        : [OPTIONAL] mediagoblin/mediagoblin image tag (default: latest)
#   mediagoblin_admin_user     : [OPTIONAL] admin username (default: admin)
#   mediagoblin_admin_password : [OPTIONAL] admin password (default: changeme123)
#   mediagoblin_admin_email    : [OPTIONAL] admin email (default: admin@lab.local)
#
# Deploys the official mediagoblin/mediagoblin Docker Hub image (stable release 0.15.0, native Docker
# support since 0.14.0). The project's production compose stack (web, celery worker, RabbitMQ and nginx)
# is reduced to one pod. MediaGoblin's CELERY_ALWAYS_EAGER=true mode processes uploads synchronously
# in-process, with no separate worker or broker. A large upload's processing (video transcoding, etc.)
# blocks the web request until it finishes, and an aborted connection halts it. This suits a lab demo,
# not a multi-user site. No official Helm chart exists, so this uses raw manifests, as install_mailman.py does.
#
# Uses emptyDir (not a PersistentVolumeClaim) for /srv, which is ephemeral and suited to a quick demo.
# The same tradeoff applies as in install_mailman.py, and RKE2 clusters have no default StorageClass.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "mediagoblin",
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
from lab_creation import ssh_run, process_template  # noqa: E402


def _validate(v):
    v.vns("mediagoblin")
    v.vver("mediagoblin")


def setup_mediagoblin(hostname, templ_addons_loc, cfg):
    """Deploy MediaGoblin (single pod, CELERY_ALWAYS_EAGER mode)."""
    tmpl = "{}/mediagoblin/manifests.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
    ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    ns = cfg.get("mediagoblin_ns") or "mediagoblin"
    shorthn = cfg.get("mediagoblin_shorthn") or "mediagoblin"
    print("MediaGoblin deployed. Namespace: {}".format(ns))
    print("Available at: http://{}.{}.{}".format(shorthn, cfg.get("clu_name"), cfg.get("mydomain")))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = dict(definition.get("mediagoblin", {}) or {})
    cfg["clu_name"] = clu_name
    cfg["mydomain"] = clu_cfg.get("mydomain")

    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    setup_mediagoblin(vm_name, templ_addons_loc, cfg)


if __name__ == "__main__":
    main()
