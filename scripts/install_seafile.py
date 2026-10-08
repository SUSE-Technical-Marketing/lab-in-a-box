#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Seafile — either as a standalone
# podman deployment directly on a host/VM (no Kubernetes needed) or on a
# real Kubernetes cluster via Seafile's own official Helm chart. Same
# dual-mode philosophy as install_nextcloud.py: a "podman" mode for a
# single lab node, a "kubernetes" mode for a real cluster, and as much of
# the underlying image's own real configuration surface exposed as
# reasonably possible.
# Author/s: Raul Mahiques
# License: GPLv3
#
# References:
#   https://manual.seafile.com/latest/setup/setup_ce_by_docker/ (Community Edition Docker deployment)
#   https://manual.seafile.com/13.0/repo/docker/ce/seafile-server.yml (compose file: the image references
#     for the db, redis and seafile services)
#   https://manual.seafile.com/latest/setup/helm_chart_single_node/ (Helm chart at
#     https://haiwen.github.io/seafile-helm-chart/repo, charts seafile/ce and seafile/pro)
#   https://manual.seafile.com/latest/develop/server/ (the databases ccnet_db, seafile_db and seahub_db)
#
# Seafile CE requires MariaDB or MySQL, not PostgreSQL. The seafile-mc image creates and migrates its
# three databases on first boot, using root database credentials. This addon therefore does not
# pre-create them, unlike install_nextcloud.py. The port is published directly, with no TLS frontend,
# because the lab domains are not publicly resolvable and cannot get Let's Encrypt certificates.
#
# Real env vars confirmed from the docs above: SEAFILE_SERVER_HOSTNAME,
# INIT_SEAFILE_ADMIN_EMAIL, INIT_SEAFILE_ADMIN_PASSWORD,
# INIT_SEAFILE_MYSQL_ROOT_PASSWORD, SEAFILE_MYSQL_DB_HOST, SEAFILE_MYSQL_DB_PORT,
# SEAFILE_MYSQL_DB_USER, SEAFILE_MYSQL_DB_PASSWORD, JWT_PRIVATE_KEY (>=32 chars —
# the java-saml-style signing key protecting Seafile's own internal service auth),
# CACHE_PROVIDER (redis/memcached), REDIS_HOST, REDIS_PORT, REDIS_PASSWORD. No
# documented env var exists for changing the seafile-mc image's own internal
# nginx listening port — its real, confirmed mechanism is a plain docker/podman
# port-publish (-p {host_port}:80), not an env var, so (unlike
# install_nextcloud.py's APACHE_PORT-based --network host approach) this addon
# uses ordinary bridge networking + port-publish for the app container, and
# reaches the companion MariaDB/Redis on the SAME host via the
# "host.containers.internal:host-gateway" special add-host entry (a real,
# documented Docker/Podman mechanism, not this addon's own invention) — the
# companion database's bind-address is therefore opened to 0.0.0.0, not left at
# the loopback-only default install_nextcloud.py uses with --network host.
#
# ─── JSON section: "seafile" ────────────────────────────────────────────────────
#
#   seafile_deployment    : "podman" (default) = a standalone deployment directly on a
#                            host/VM, no Kubernetes. "kubernetes" = the real official Helm chart.
#
# ── "podman" fields ───────────────────────────────────────────────────────────
#   seafile_image            : container image                (default: "seafileltd/seafile-mc")
#   seafile_version           : image tag                      (default: "13.0-latest")
#   seafile_hostname           : the real SEAFILE_SERVER_HOSTNAME clients will reach this at
#                             (default: the target host's own hostname)
#   seafile_http_port          : host port -> container port 80  (default: 8081 — distinct
#                             from install_nextcloud.py's own 8080 default, so both can run on
#                             the same lab node without a collision)
#   seafile_admin_email        : initial admin email             (default:
#                             "admin@<seafile_hostname>") — the real INIT_SEAFILE_ADMIN_EMAIL
#                             env var, only used on FIRST boot
#   seafile_admin_password      : initial admin password           (default: auto-generated and
#                             printed) — the real INIT_SEAFILE_ADMIN_PASSWORD env var
#   seafile_account            : name of an encrypted credential_kind "seafile" file under
#                             /etc/lab_creation/credentials/ to read seafile_admin_password
#                             from instead — auto-discovered if exactly one such file exists
#                             and this is left unset (same convention as install_ds389.py/
#                             install_gitlab.py/install_nextcloud.py)
#   seafile_jwt_private_key     : the real JWT_PRIVATE_KEY signing key (>=32 chars)  (default:
#                             auto-generated and printed)
#   seafile_data_size          : size of the persistent data volume — informational only,
#                             podman named volumes aren't size-capped; documents intent
#                             (default: unset)
#
#   seafile_db_user            : the real SEAFILE_MYSQL_DB_USER — the dedicated MySQL user
#                             Seafile creates and uses for ongoing operation (default: "seafile")
#   seafile_db_password         : the real SEAFILE_MYSQL_DB_PASSWORD                (default:
#                             auto-generated and printed)
#   seafile_db_root_password    : the companion MariaDB's real root password, used ONCE by
#                             Seafile's own first-boot init to create its ccnet_db/seafile_db/
#                             seahub_db databases and the seafile_db_user above (default:
#                             auto-generated and printed) — passed straight through as
#                             mariadb_root_password to libs/db_common.py's setup_mariadb_os(),
#                             the SAME code install_mariadb.py itself runs, not a
#                             reimplementation (see install_nextcloud.py's own nextcloud_db for
#                             the identical convention)
#   seafile_db_port            : companion MariaDB's listening port                (default: 3306)
#   seafile_db_options         : [OPTIONAL] a dict of additional mariadb_* keys passed straight
#                             through to setup_mariadb_os() — see install_mariadb.py's own
#                             top-of-file schema doc for the full list, e.g.
#                             {"mariadb_pkg_version": "10.6"}
#
#   seafile_cache_provider      : "redis" (default) or "memcached" — the real CACHE_PROVIDER
#                             env var; deploys a matching companion container either way (no
#                             shared addon exists for either in this project yet, so both stay
#                             small inline containers here, same as install_prometheus.py's/
#                             install_grafana.py's own single-container addons)
#   seafile_cache_version       : companion Redis/Memcached image tag                (default:
#                             "latest")
#   seafile_cache_port          : companion Redis/Memcached's published port          (default:
#                             6379 for redis, 11211 for memcached)
#
# ── "kubernetes" fields ───────────────────────────────────────────────────────
#   seafile_edition            : "ce" (default, Community Edition) or "pro" (Professional —
#                             requires a real Seafile Pro license; this addon does not manage
#                             licensing itself)
#   seafile_rel                : Helm repo alias               (default: "seafile")
#   seafile_repo_url           : Helm repo URL                 (default:
#                             "https://haiwen.github.io/seafile-helm-chart/repo")
#   seafile_chart_version       : Helm chart version             (empty = latest — the chart's
#                             own docs recommend pinning an explicit version for reproducible
#                             deployments; left to the caller's own judgement here, same
#                             stance as every other Helm-based addon in this project)
#   seafile_namespace          : Kubernetes namespace           (default: "seafile")
#   seafile_extra_values        : [OPTIONAL] a dict of additional raw `--set key=value` pairs
#                             passed straight through to `helm upgrade --install`, for anything
#                             this addon doesn't expose its own field for (JWT_PRIVATE_KEY,
#                             SEAFILE_MYSQL_DB_PASSWORD, INIT_SEAFILE_ADMIN_PASSWORD and
#                             INIT_SEAFILE_MYSQL_ROOT_PASSWORD are real required Kubernetes
#                             Secret values per the chart's own docs — generated here and
#                             passed through --set the same as every other value, since this
#                             project's other Helm-chart addons don't manage separate Secret
#                             objects either)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "seafile",
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "standalone-container"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import secrets
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, helm_repo_add, die  # noqa: E402
from db_common import setup_mariadb_os  # noqa: E402

_DEFAULT_IMAGE = "seafileltd/seafile-mc"
_DEFAULT_VERSION = "13.0-latest"


def _validate(v):
    v.vver("seafile")


def _wait_ready(hostname, port, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        r = ssh_run(hostname, "curl -sk -o /dev/null -w '%{{http_code}}' http://localhost:{}/".format(port),
                   check=False, capture=True)
        if r.returncode == 0 and (r.stdout or "").strip() in ("200", "302"):
            return True
        time.sleep(5)
    return False


def setup_seafile_podman(hostname, cfg, virt_srv=None):
    """
    Deploys Seafile CE as a standalone podman container directly on
    `hostname` — the official seafileltd/seafile-mc image, plus a
    companion MariaDB installed NATIVELY on the same host (via
    libs/db_common.py's setup_mariadb_os() — the identical code
    install_mariadb.py itself runs) and a companion Redis/Memcached
    container. See this file's own top-of-file reference for why bridge
    networking + host.containers.internal is used here instead of
    install_nextcloud.py's --network host approach.
    """
    image = cfg.get("seafile_image") or _DEFAULT_IMAGE
    version = cfg.get("seafile_version") or _DEFAULT_VERSION
    seafile_hostname = cfg.get("seafile_hostname") or hostname
    http_port = int(cfg.get("seafile_http_port") or 8081)

    creds = ac.resolve_credential(cfg, "seafile", {"seafile_admin_password": "seafile_admin_password"},
                                   account_key="seafile_account")
    admin_email = cfg.get("seafile_admin_email") or "admin@{}".format(seafile_hostname)
    admin_password = creds["seafile_admin_password"]
    if not admin_password:
        admin_password = secrets.token_urlsafe(18)
        print("  No seafile_admin_password configured anywhere — generated one: {}".format(admin_password))

    jwt_key = cfg.get("seafile_jwt_private_key") or secrets.token_hex(20)
    if len(jwt_key) < 32:
        die("seafile: seafile_jwt_private_key must be at least 32 characters (Seafile's own "
            "documented JWT_PRIVATE_KEY requirement)")

    print("- Installing podman (if not already present)")
    ssh_run(hostname, "command -v podman >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y podman 2>/dev/null || "
                       "apt-get install -y podman 2>/dev/null || "
                       "dnf install -y podman 2>/dev/null)", check=False)
    ssh_run(hostname, "systemctl enable --now podman.socket", check=False)

    db_options = cfg.get("seafile_db_options") or {}
    db_root_password = cfg.get("seafile_db_root_password") or secrets.token_urlsafe(18)
    db_cfg = dict({
        "mariadb_root_password": db_root_password,
        "mariadb_port": cfg.get("seafile_db_port"),
        # Reached from a bridge-networked container via host.containers.internal
        # below, not the host's own loopback — must listen on every interface,
        # unlike install_nextcloud.py's --network host approach, which keeps the
        # loopback-only default.
        "mariadb_bind_address": "0.0.0.0",
    }, **db_options)
    db_result = setup_mariadb_os(hostname, db_cfg, virt_srv)

    db_user = cfg.get("seafile_db_user") or "seafile"
    db_password = cfg.get("seafile_db_password") or secrets.token_urlsafe(18)

    cache_provider = (cfg.get("seafile_cache_provider") or "redis").lower()
    if cache_provider not in ("redis", "memcached"):
        die("seafile: seafile_cache_provider must be 'redis' or 'memcached', got '{}'".format(cache_provider))
    cache_version = cfg.get("seafile_cache_version") or "latest"
    default_cache_port = 6379 if cache_provider == "redis" else 11211
    cache_port = int(cfg.get("seafile_cache_port") or default_cache_port)

    print("- Deploying a companion {} container".format(cache_provider))
    ssh_run(hostname, "podman rm -f seafile-cache 2>/dev/null", check=False)
    ssh_run(hostname, "podman run -d --name seafile-cache --restart=always -p {port}:{port} "
                       "{image}:{version}".format(
                           port=cache_port, image=shlex.quote(cache_provider), version=shlex.quote(cache_version)))

    env_args = [
        "-e SEAFILE_SERVER_HOSTNAME={}".format(shlex.quote(seafile_hostname)),
        "-e INIT_SEAFILE_ADMIN_EMAIL={}".format(shlex.quote(admin_email)),
        "-e INIT_SEAFILE_ADMIN_PASSWORD={}".format(shlex.quote(admin_password)),
        "-e INIT_SEAFILE_MYSQL_ROOT_PASSWORD={}".format(shlex.quote(db_result["root_password"])),
        "-e SEAFILE_MYSQL_DB_HOST=host.containers.internal",
        "-e SEAFILE_MYSQL_DB_PORT={}".format(db_result["port"]),
        "-e SEAFILE_MYSQL_DB_USER={}".format(shlex.quote(db_user)),
        "-e SEAFILE_MYSQL_DB_PASSWORD={}".format(shlex.quote(db_password)),
        "-e JWT_PRIVATE_KEY={}".format(shlex.quote(jwt_key)),
        "-e CACHE_PROVIDER={}".format(shlex.quote(cache_provider)),
        "-e REDIS_HOST=host.containers.internal" if cache_provider == "redis" else
        "-e MEMCACHED_HOST=host.containers.internal",
        "-e REDIS_PORT={}".format(cache_port) if cache_provider == "redis" else
        "-e MEMCACHED_PORT={}".format(cache_port),
    ]

    print("- Deploying the Seafile container")
    ssh_run(hostname, "podman rm -f seafile 2>/dev/null", check=False)
    ssh_run(hostname, "podman run -d --name seafile --restart=always -p {port}:80 "
                       "--add-host host.containers.internal:host-gateway {env} "
                       "-v seafile-data:/shared "
                       "{image}:{version}".format(
                           port=http_port, env=" ".join(env_args),
                           image=shlex.quote(image), version=shlex.quote(version)))

    print("- Generating a systemd unit so the containers survive a reboot")
    for name in ("seafile-cache", "seafile"):
        unit = (
            "[Unit]\nDescription={name} (lab-in-a-box)\nAfter=network-online.target podman.socket\n"
            "Wants=network-online.target\n\n[Service]\nRestart=always\nTimeoutStartSec=180\n"
            "ExecStart=/usr/bin/podman start -a {name}\nExecStop=/usr/bin/podman stop -t 30 {name}\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ).format(name=name)
        ssh_run(hostname, "cat > /etc/systemd/system/{}.service <<'EOF'\n{}EOF".format(name, unit))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable seafile-cache.service seafile.service",
           check=False)

    print("- Waiting for Seafile to finish first-run setup and answer real HTTP requests "
          "(up to 3 minutes — first boot creates the ccnet_db/seafile_db/seahub_db schema)")
    if not _wait_ready(hostname, http_port):
        print("WARNING: Seafile was still not answering after 3 minutes — check progress with: "
              "podman logs seafile")

    print("Seafile available at: http://{}:{}".format(seafile_hostname, http_port))
    print("Admin login: {} / {}".format(admin_email, admin_password))


def setup_seafile_kubernetes(hostname, cfg):
    """
    Installs Seafile via its own real official Helm chart
    (haiwen.github.io/seafile-helm-chart) — deployed ONCE to the cluster's
    server node, matching install_nextcloud.py/install_gitlab.py's own
    convention for a cluster-wide Kubernetes app.
    """
    edition = (cfg.get("seafile_edition") or "ce").lower()
    if edition not in ("ce", "pro"):
        die("seafile: seafile_edition must be 'ce' or 'pro', got '{}'".format(edition))
    ns = ac.require_k8s_name(cfg, "seafile_namespace", "seafile")
    rel = cfg.get("seafile_rel") or "seafile"

    helm_repo_add(hostname, rel, cfg.get("seafile_repo_url") or "https://haiwen.github.io/seafile-helm-chart/repo")
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)

    jwt_key = cfg.get("seafile_jwt_private_key") or secrets.token_hex(20)
    db_password = cfg.get("seafile_db_password") or secrets.token_urlsafe(18)
    db_root_password = cfg.get("seafile_db_root_password") or secrets.token_urlsafe(18)
    admin_password = cfg.get("seafile_admin_password") or secrets.token_urlsafe(18)

    set_args = [
        "--set env.JWT_PRIVATE_KEY={}".format(shlex.quote(jwt_key)),
        "--set env.SEAFILE_MYSQL_DB_PASSWORD={}".format(shlex.quote(db_password)),
        "--set env.INIT_SEAFILE_MYSQL_ROOT_PASSWORD={}".format(shlex.quote(db_root_password)),
        "--set env.INIT_SEAFILE_ADMIN_PASSWORD={}".format(shlex.quote(admin_password)),
        "--set env.SEAFILE_SERVER_HOSTNAME={}".format(shlex.quote(cfg.get("seafile_hostname") or hostname)),
    ]
    if cfg.get("seafile_admin_email"):
        set_args.append("--set env.INIT_SEAFILE_ADMIN_EMAIL={}".format(shlex.quote(cfg["seafile_admin_email"])))
    for key, value in (cfg.get("seafile_extra_values") or {}).items():
        set_args.append("--set {}={}".format(shlex.quote(str(key)), shlex.quote(str(value))))
    version_arg = "--version {}".format(shlex.quote(cfg["seafile_chart_version"])) \
        if cfg.get("seafile_chart_version") else ""

    print("- Installing Seafile ({}) via its own official Helm chart into namespace '{}'".format(
        edition.upper(), ns))
    ssh_run(hostname, "helm upgrade --install seafile {rel}/{edition} -n {ns} {version} "
                       "--timeout 600s {sets}".format(
                           rel=rel, edition=edition, ns=ns, version=version_arg, sets=" ".join(set_args)))
    print("Seafile installed in namespace '{}'.".format(ns))
    print("Admin login: {} / {}".format(cfg.get("seafile_admin_email") or "(see chart defaults)", admin_password))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()
    cfg = definition.get("seafile", {}) or {}

    if (cfg.get("seafile_deployment") or "podman") == "kubernetes":
        target = k8s.first_server_node(definition)
        if not target:
            sys.exit(1)
        vm_name, _ssh_cmd = target
        setup_seafile_kubernetes(vm_name, cfg)
        return

    virt_srv = config.get("VIRT_SRV", "")
    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "seafile", vm_name=env_vm_name):
        setup_seafile_podman(vm_name, cfg, virt_srv)


if __name__ == "__main__":
    main()
