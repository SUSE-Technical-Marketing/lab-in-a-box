#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install openldap
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "openldap" — OpenLDAP directory service on Kubernetes
#
#   openldap_ns      : [OPTIONAL] Kubernetes namespace                 (default: db)
#   openldap_name    : [OPTIONAL] Deployment and service name          (default: openldap)
#   openldap_version : [OPTIONAL] Helm chart version                   (empty = latest)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "openldap",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402


def setup_openldap(hostname, templ_addons_loc, openldap_cfg):
    """
    Install OpenLDAP via Helm using a rendered values file. Mirrors
    setup_openldap (bash). See:
    https://www.talkingquickly.co.uk/installing-openldap-kubernetes-helm

    NOTE: bash's final helm command references a LOCAL relative chart path
    ("./charts/openldap") resolved on the REMOTE host relative to whatever
    directory the SSH session lands in — this presupposes a pre-staged chart
    directory that nothing in this script (or its callers, as far as this
    repo shows) actually creates. Preserved verbatim rather than guessing at
    the missing provisioning step.
    """
    ns = openldap_cfg.get("openldap_ns") or "db"
    name = openldap_cfg.get("openldap_name") or "openldap"
    ssh_run(hostname, "kubectl delete -n {} deployment.apps/{} service/{}".format(ns, name, name), check=False)

    tmpl = "{}/openldap/install.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
    rendered = process_template(tmpl, openldap_cfg)
    ssh_run(hostname, "cat >/tmp/openldap_install_values.yml", input_text=rendered)

    ssh_run(hostname, "helm upgrade --install openldap ./charts/openldap --values /tmp/openldap_install_values.yml")


def main():
    # bash's --validate block here defines the usual helpers but never calls
    # any of them — always exits 0.
    ac.handle_common_args(__file__, __version__, validate_fn=None, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    # The first node of the lab, as in install_mariadb; no server-role filter.
    nodes = list(definition.get("nodes", {}))
    if not nodes:
        sys.exit(1)
    vm_name = nodes[0]

    openldap_cfg = definition.get("openldap", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")

    print("# Using node: {}".format(vm_name))
    setup_openldap(vm_name, templ_addons_loc, openldap_cfg)
    time.sleep(60)


if __name__ == "__main__":
    main()
