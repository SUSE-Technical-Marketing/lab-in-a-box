#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install MariaDB in Kubernetes (Helm-less manifest) or on the OS
# Author/s: Raul Mahiques
# License: GPLv3
#
# ─── MODE DETECTION ────────────────────────────────────────────────────────────
#   Kubernetes mode : script is called as a kclusters addon → clu_name env var is set
#   OS mode         : script is called as a nodes addon     → clu_name env var is empty
#   Override either mode by setting mariadb_mode = "kubernetes" or "os" in the JSON.
#
# ─── JSON section: "mariadb" ────────────────────────────────────────────────────
#
# SHARED (both modes)
#   mariadb_mode          : [OPTIONAL] Where to run MariaDB (default: auto) (options: auto, kubernetes, os)
#   mariadb_db            : [OPTIONAL] default database name    (default: none created)
#   mariadb_user          : [OPTIONAL] default username         (default: none created)
#   mariadb_password      : [OPTIONAL] password for mariadb_user / the Kubernetes root user
#                           (default: auto-generated and printed)
#
# KUBERNETES MODE
#   mariadb_ns            : [OPTIONAL] Kubernetes namespace                  (default: db)
#   mariadb_name          : [OPTIONAL] Deployment and service name           (default: mariadb)
#   mariadb_version       : [OPTIONAL] Helm chart version                    (empty = latest)
#
# OS MODE — the target node must list "mariadb" in its nodes[x].addons[] array. The actual
# installation logic lives in libs/db_common.py's setup_mariadb_os() — shared so any other
# addon that needs a companion MariaDB on the same host (install_nextcloud.py,
# install_seafile.py, ...) can call it directly instead of bootstrapping its own ad-hoc
# database container.
#   mariadb_root_password : [OPTIONAL] root user password                    (default:
#                           auto-generated and printed)
#   mariadb_port          : [OPTIONAL] listening port                        (default: 3306)
#   mariadb_bind_address  : [OPTIONAL] bind-address value                   (default: 127.0.0.1
#                           — loopback-only; a caller relying on this from another container on
#                           the SAME host is expected to reach it via --network host, same
#                           convention as install_prometheus.py/install_grafana.py, not by
#                           opening this up to the outside)
#
# SLES / SLE Micro note:
#   SLE Micro uses transactional-update and requires a reboot after package install.
#
# RHEL / CentOS note:
#   Uses the distro's own mariadb-server package (no external repo needed, unlike Postgres).

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "mariadb",
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "os-native"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402
from db_common import setup_mariadb_os  # noqa: E402


def _validate(v):
    definition = v.definition
    mode = (definition.get("mariadb", {}) or {}).get("mariadb_mode", "")
    if mode and mode not in ("auto", "kubernetes", "os"):
        v.errors.append("[ERROR] mariadb.mariadb_mode='{}': must be auto, kubernetes, or os".format(mode))
    v.vport("mariadb", "mariadb_port")
    v.vns("mariadb")
    v.vver("mariadb")
    v.vurl("mariadb")


def setup_mariadb(hostname, templ_addons_loc, mariadb_cfg):
    """
    Delete any existing deployment/service, then render+apply the
    mariadb manifest template. Mirrors setup_mariadb (bash).

    mariadb_cfg : the raw "mariadb" JSON section dict — passed straight
    through to process_template, whose bash-side ${VAR:-default} defaults in
    the template itself supply the fallback for anything not set here (same
    mechanism bash used, since this shells out to the identical eval/heredoc
    primitive).
    """
    ns = mariadb_cfg.get("mariadb_ns") or "db"
    name = mariadb_cfg.get("mariadb_name") or "mariadb"
    # No error check in bash either — deleting a deployment/service that
    # doesn't exist yet (first run) is expected to "fail" harmlessly.
    ssh_run(hostname, "kubectl delete -n {} deployment.apps/{} service/{}".format(ns, name, name), check=False)

    tmpl = "{}/mariadb/install.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
    rendered = process_template(tmpl, mariadb_cfg)
    ssh_run(hostname, "kubectl apply -f -", input_text=rendered)


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()
    config = primary.load_config()

    mariadb_cfg = definition.get("mariadb", {}) or {}

    clu_name_env = os.environ.get("clu_name", "")
    mode = mariadb_cfg.get("mariadb_mode") or "auto"
    if mode == "auto":
        mode = "kubernetes" if clu_name_env else "os"

    if mode == "kubernetes":
        nodes = list(definition.get("nodes", {}))
        if not nodes:
            sys.exit(1)
        vm_name = nodes[0]
        templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")

        print("# Using node: {}".format(vm_name))
        setup_mariadb(vm_name, templ_addons_loc, mariadb_cfg)
        time.sleep(60)
    else:
        virt_srv = config.get("VIRT_SRV", "")
        env_vm_name = os.environ.get("_vm_name") or None
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "mariadb", vm_name=env_vm_name):
            setup_mariadb_os(vm_name, mariadb_cfg, virt_srv)


if __name__ == "__main__":
    main()
