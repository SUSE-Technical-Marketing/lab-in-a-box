#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install PostgreSQL in Kubernetes (Helm) or on the OS
# Author/s: Raul Mahiques
# License: GPLv3
#
# ─── MODE DETECTION ────────────────────────────────────────────────────────────
#   Kubernetes mode : script is called as a kclusters addon → clu_name env var is set
#   OS mode         : script is called as a nodes addon     → clu_name env var is empty
#   Override either mode by setting postgresql_mode = "kubernetes" or "os" in the JSON.
#
# ─── JSON section: "postgresql" ────────────────────────────────────────────────
#
# SHARED (both modes)
#   postgresql_mode       : [OPTIONAL] Where to run Postgres (default: auto) (options: auto, kubernetes, os)
#   postgresql_password   : [OPTIONAL] superuser password       (default: postgres123)
#   postgresql_db         : [OPTIONAL] default database name    (default: postgres)
#   postgresql_user       : [OPTIONAL] default username         (default: postgres)
#
# KUBERNETES MODE
#   postgresql_version    : [OPTIONAL] Helm chart version       (empty = latest, e.g. "15.5.17")
#   postgresql_ns         : [OPTIONAL] namespace                (default: postgresql)
#   postgresql_rel        : [OPTIONAL] Helm repo alias          (default: bitnami)
#   postgresql_repo_url   : [OPTIONAL] Helm repo URL            (default: https://charts.bitnami.com/bitnami)
#
# OS MODE  — the target node must list "postgresql" in its nodes[x].addons[] array
#   postgresql_pg_version : [OPTIONAL] PostgreSQL major version (default: 16, e.g. "14", "15", "16")
#                           Setting a lower version than the distro default achieves a downgrade.
#   postgresql_port       : [OPTIONAL] listening port           (default: 5432)
#   postgresql_listen     : [OPTIONAL] listen_addresses value   (default: *)
#
# SLES / SLE Micro note:
#   On SLES 15, the postgresql packages live in the "Server Applications Module".
#   Activate it first:  SUSEConnect -p sle-module-server-applications/15.6/x86_64
#   SLE Micro uses transactional-update and requires a reboot after package install.
#
# RHEL / CentOS note:
#   The official PGDG repository (yum.postgresql.org) is added automatically so that
#   any supported major version (including older ones) can be installed.
#
# Ubuntu / Debian note:
#   The official PGDG APT repository (apt.postgresql.org) is added automatically.

__version__ = "a45abd4"

PLUGIN = {
    "name": "postgresql",
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "os-native"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402
from db_common import setup_postgresql_os  # noqa: E402


def _validate(v):
    definition = v.definition
    mode = (definition.get("postgresql", {}) or {}).get("postgresql_mode", "")
    if mode and mode not in ("auto", "kubernetes", "os"):
        v.errors.append("[ERROR] postgresql.postgresql_mode='{}': must be auto, kubernetes, or os".format(mode))
    pgver = (definition.get("postgresql", {}) or {}).get("postgresql_pg_version", "")
    if pgver and not str(pgver).isdigit():
        v.errors.append(
            "[ERROR] postgresql.postgresql_pg_version='{}': must be a major version number (e.g. 16)".format(pgver))
    v.vport("postgresql", "postgresql_port")
    v.vns("postgresql")
    v.vver("postgresql")


# ─── Kubernetes (Helm) mode ─────────────────────────────────────────────────────

def setup_postgresql_repo(hostname, postgresql_rel=None, postgresql_repo_url=None):
    """Add the PostgreSQL (bitnami) Helm repo. Mirrors setup_postgresql_repo (bash)."""
    helm_repo_add(hostname, postgresql_rel or "bitnami", postgresql_repo_url or "https://charts.bitnami.com/bitnami")


def setup_postgresql_k8s(hostname, cfg):
    """Install PostgreSQL via Helm. Mirrors setup_postgresql_k8s (bash)."""
    rel = cfg.get("postgresql_rel") or "bitnami"
    ns = cfg.get("postgresql_ns") or "postgresql"
    ver_arg = "--version {}".format(cfg["postgresql_version"]) if cfg.get("postgresql_version") else ""
    pwd = cfg.get("postgresql_password") or "postgres123"
    db = cfg.get("postgresql_db") or "postgres"
    user = cfg.get("postgresql_user") or "postgres"

    ssh_run(hostname,
            "helm upgrade -i postgresql {}/postgresql --namespace {} --create-namespace "
            "--set auth.postgresPassword={} --set auth.database={} --set auth.username={} "
            "{}".format(rel, ns, pwd, db, user, ver_arg))
    print("PostgreSQL installed in Kubernetes. Namespace: {}".format(ns))
    print("Connect: kubectl -n {} exec -it postgresql-0 -- psql -U {}".format(ns, user))


# ─── OS mode ────────────────────────────────────────────────────────────────────
# The actual installation logic lives in libs/db_common.py's setup_postgresql_os()
# (imported above) — shared so any other addon that needs a companion PostgreSQL
# on the same host (install_nextcloud.py, ...) can call it directly instead of
# bootstrapping its own ad-hoc database container.


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()

    cfg = definition.get("postgresql", {}) or {}

    # Mode detection: explicit postgresql_mode override, else auto-detect from
    # whether clu_name was inherited from the environment (set by setup_lab.py
    # only for cluster-level addon invocations).
    clu_name_env = os.environ.get("clu_name", "")
    mode = cfg.get("postgresql_mode") or "auto"
    if mode == "auto":
        mode = "kubernetes" if clu_name_env else "os"

    if mode == "kubernetes":
        target = k8s.first_server_node(definition)
        if not target:
            sys.exit(1)
        vm_name, _ssh_cmd = target
        clu_name = k8s.get_vm_kcluster(definition, vm_name)
        online = definition.get("common", {}).get("online") == "1"

        setup_helm(vm_name, clu_name, online=online)
        setup_postgresql_repo(vm_name, postgresql_rel=cfg.get("postgresql_rel"),
                               postgresql_repo_url=cfg.get("postgresql_repo_url"))
        setup_postgresql_k8s(vm_name, cfg)
    else:
        virt_srv = config.get("VIRT_SRV", "")
        env_vm_name = os.environ.get("_vm_name") or None
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "postgresql", vm_name=env_vm_name):
            setup_postgresql_os(vm_name, cfg, virt_srv)


if __name__ == "__main__":
    main()
