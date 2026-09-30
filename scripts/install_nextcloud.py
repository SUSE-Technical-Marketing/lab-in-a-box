#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Nextcloud — either as a standalone
# podman deployment directly on a host/VM (no Kubernetes needed, and the
# only mode this addon supports airgapped) or on a real Kubernetes cluster
# via Nextcloud's own community-maintained Helm chart.
# Author/s: Raul Mahiques
# License: GPLv3
#
# References (ground-truthed 2026-09-27 against the real GitHub org
# https://github.com/nextcloud/ and its own repos/docs, not guessed):
#   https://github.com/nextcloud/server                — core; bundles the
#     "workflowengine" app (real technical id of the "Flow" feature — no
#     separate repo/app exists for it, it ships with every install)
#   https://github.com/nextcloud/richdocuments          — the real
#     Collabora Online (LibreOffice-engine-based) integration app, real id
#     "richdocuments", real admin config keys wopi_url/public_wopi_url
#   https://github.com/nextcloud/spreed                 — Nextcloud Talk's
#     real backend app, real id "spreed"
#   https://github.com/nextcloud/{calendar,contacts,mail} — the real three
#     apps that make up the "Groupware" bundle
#   https://github.com/nextcloud/assistant               — the real
#     "assistant" app; needs a text-processing backend app behind it (see
#     smlm_assistant_llm_endpoint below)
#   https://github.com/nextcloud/helm                    — the real
#     community-maintained Helm chart (repo https://nextcloud.github.io/helm/)
#   https://github.com/nextcloud/all-in-one              — Nextcloud AIO,
#     deliberately NOT used here: its own FAQ states offline/airgapped
#     operation "is not possible and will not be added" (needs live
#     app-store installs, on-demand image pulls, and direct docker-socket
#     access to orchestrate sibling containers) — incompatible with this
#     addon's own nextcloud_airgap requirement, so podman mode instead
#     deploys the plain official nextcloud/mariadb/redis images directly,
#     the same way every other real image in this project gets deployed.
#   https://github.com/ONLYOFFICE/onlyoffice-nextcloud   — the real
#     ONLYOFFICE connector app, id "onlyoffice" — needs a separate
#     ONLYOFFICE Document Server (https://github.com/ONLYOFFICE/DocumentServer,
#     official image onlyoffice/documentserver) reachable from both
#     Nextcloud and end users' browsers, configured with a shared JWT secret
#   https://github.com/ibeacon-projekt/eurooffice-nextcloud — Euro-Office,
#     a sovereign European fork of the same ONLYOFFICE open-source codebase
#     (NOT affiliated with ONLYOFFICE) — architecturally identical
#     integration shape (its own Document Server + JWT secret), real app id
#     "eurooffice"
#
# ─── JSON section: "nextcloud" ────────────────────────────────────────────────
#
#   nextcloud_deployment   : "podman" (default) = standalone containers directly on a
#                             host/VM, no Kubernetes — the only mode nextcloud_airgap
#                             works in. "kubernetes" = the real community Helm chart.
#
# ── "podman" fields ───────────────────────────────────────────────────────────
#   nextcloud_image          : container image                (default: "nextcloud")
#   nextcloud_version         : image tag                      (default: "latest")
#   nextcloud_hostname         : the real trusted_domains / OVERWRITECLI hostname clients
#                             will reach this at                (default: the target host's
#                             own hostname)
#   nextcloud_http_port        : host port -> container 80        (default: 8080 — not 80,
#                             so this addon can coexist with something else already using
#                             80 on a shared lab node; override freely)
#   nextcloud_admin_user       : initial admin username           (default: "admin") — the
#                             real NEXTCLOUD_ADMIN_USER env var the official image
#                             auto-provisions on FIRST boot only
#   nextcloud_admin_password    : initial admin password           (default: auto-generated
#                             and printed) — the real NEXTCLOUD_ADMIN_PASSWORD env var
#   nextcloud_account          : name of an encrypted credential_kind "nextcloud" file
#                             under /etc/lab_creation/credentials/ to read
#                             nextcloud_admin_password from instead — auto-discovered if
#                             exactly one such file exists and this is left unset (same
#                             convention as install_ds389.py/install_gitlab.py)
#   nextcloud_data_size        : size of the persistent data volume — informational only,
#                             podman named volumes aren't size-capped; documents intent
#                             (default: unset)
#   nextcloud_db              : "sqlite" (default — simplest, fine for a lab), "mariadb" or
#                             "postgresql" — the latter two are installed NATIVELY on this
#                             SAME host (not as a separate container) via this project's own
#                             install_mariadb.py/install_postgresql.py logic
#                             (libs/db_common.py's setup_mariadb_os()/setup_postgresql_os()
#                             — the identical code those addons themselves run, not a
#                             reimplementation), and reached from the Nextcloud container over
#                             127.0.0.1 via --network host (see setup_nextcloud_podman()'s own
#                             comment on why). Every mariadb_*/postgresql_* configuration
#                             option those addons support (root/superuser password, port,
#                             bind-address/listen address, OS package version, ...) is
#                             available here too — see install_mariadb.py's/
#                             install_postgresql.py's own top-of-file schema docs for the
#                             full list; pass any of them straight through inside a
#                             nextcloud_db_options dict (e.g. {"mariadb_bind_address": "0.0.0.0"}).
#   nextcloud_db_port         : companion database's listening port, only relevant if
#                             nextcloud_db is "mariadb"/"postgresql"    (default: 3306/5432)
#   nextcloud_db_password      : companion database's "nextcloud" application user password,
#                             only relevant if nextcloud_db is "mariadb"/"postgresql"
#                             (default: auto-generated and printed)
#   nextcloud_db_root_password : companion database's root/superuser password, only relevant
#                             if nextcloud_db is "mariadb"/"postgresql"  (default: auto-generated
#                             and printed) — postgresql itself has no separate app-user
#                             password from its superuser one, so this is mariadb-only in
#                             practice, but harmlessly ignored for postgresql
#   nextcloud_db_options       : [OPTIONAL] a dict of additional mariadb_*/postgresql_* keys
#                             (whichever matches nextcloud_db) passed straight through to
#                             setup_mariadb_os()/setup_postgresql_os(), for anything this
#                             addon doesn't have its own named nextcloud_db_* field for —
#                             e.g. {"mariadb_bind_address": "0.0.0.0"} or
#                             {"postgresql_listen": "0.0.0.0", "postgresql_pg_version": "15"}
#   nextcloud_redis           : "true"/"false"                    (default: "true" — deploys
#                             a real companion redis container; the official image wires it
#                             up automatically via REDIS_HOST for real file-locking/caching,
#                             not just a nice-to-have)
#   nextcloud_redis_version    : companion Redis image tag, only relevant if nextcloud_redis
#                             is "true"                            (default: "latest")
#   nextcloud_max_upload_size  : the real NEXTCLOUD_UPLOAD_LIMIT env var, e.g. "10G"
#                             (default: unset — image's own default, 512M)
#   nextcloud_trusted_domains  : [OPTIONAL] extra hostnames/IPs beyond nextcloud_hostname to
#                             add to config.php's trusted_domains
#
#   nextcloud_talk            : "true"/"false"      (default: "false") — installs+enables
#                             the real "spreed" app (Nextcloud Talk's technical id)
#   nextcloud_groupware        : "true"/"false"      (default: "false") — installs+enables
#                             the real "calendar"+"contacts"+"mail" apps
#   nextcloud_flow            : "true"/"false"      (default: "false") — enables the real
#                             "workflowengine" app (Flow's technical id) — it ships bundled
#                             with core already, so this is enable-only, no install needed
#   nextcloud_assistant        : "true"/"false"      (default: "false") — installs+enables
#                             the real "assistant" app
#   nextcloud_assistant_llm_endpoint : [OPTIONAL] an OpenAI-API-compatible base URL (e.g.
#                             this project's own "ollama"/"anthropic"/"openai" LiteLLM-proxy
#                             addons' own endpoint) to wire the Assistant's real
#                             "integration_openai" backend app to, instead of running a local
#                             model — installs+enables integration_openai and points its
#                             real "url" config key at this endpoint. Left unset, Assistant
#                             installs with no working text-processing backend configured
#                             (matches real Nextcloud behavior — Assistant itself never
#                             ships a backend, one must be added separately either way).
#   nextcloud_extra_apps       : [OPTIONAL] a plain list of additional app ids to
#                             install+enable generically, for anything this addon doesn't
#                             have its own named field for, e.g. ["deck", "forms", "notes"]
#
#   nextcloud_office           : [OPTIONAL] {
#                                 "provider": "onlyoffice" | "eurooffice" | "collabora",
#                                 "document_server_url": "https://...",   # onlyoffice/eurooffice
#                                 "wopi_url": "https://...",              # collabora only
#                                 "jwt_secret": "...",                    # onlyoffice/eurooffice —
#                                            REQUIRED if deploy_server is left false (a real,
#                                            already-running Document Server's own configured
#                                            secret); auto-generated+printed if deploy_server=true
#                                 "deploy_server": "true"/"false",  # (default: "false") — also
#                                            deploy a REAL standalone Document Server container
#                                            on THIS SAME host for the chosen provider
#                                            (onlyoffice/documentserver, the real
#                                            eurooffice-nextcloud image, or collabora/code) —
#                                            document_server_url/wopi_url default to
#                                            http://<hostname>:<port> when this is true
#                                 "server_image": "...",            # override the real default
#                                            image for the chosen provider
#                                 "server_version": "...",          # image tag for the deployed
#                                            Document Server                (default: "latest")
#                                 "server_port": 8443,              # host port for the deployed
#                                            Document Server (default: 8443)
#                               }
#                             Installs+enables the matching real app ("onlyoffice",
#                             "eurooffice", or "richdocuments" for collabora) and configures
#                             its real admin settings via `occ config:app:set`.
#
#   nextcloud_airgap           : "true"/"false"      (default: "false") — REAL, documented
#                             Nextcloud constraint, not this addon's own invention: with
#                             this on, EVERY app above installs from a local, pre-downloaded
#                             archive instead of the live App Store (occ app:install always
#                             reaches out to apps.nextcloud.com otherwise) — see
#                             nextcloud_apps_archive_dir. Also sets the real config.php
#                             appstoreenabled=false and updatechecker.enabled=false keys, so
#                             Nextcloud itself never tries to phone out either. Every
#                             *_image field above (and nextcloud_office's server_image) must
#                             already point at an image reachable without internet access
#                             (a private registry mirror, or already pre-pulled on the
#                             target host) — this addon does not manage image mirroring
#                             itself, same stance as every other addon's own *_image field.
#   nextcloud_apps_archive_dir : [REQUIRED if nextcloud_airgap is "true" and any app is
#                             enabled] a local directory ON THE AUTOMATION NODE containing
#                             one "<appid>.tar.gz" per app to install (download each from
#                             https://apps.nextcloud.com/apps/<appid> — "Download and enable"
#                             on that app's own page links the real release archive — on a
#                             connected machine beforehand) — staged onto the target host via
#                             real scp, then extracted directly, matching Nextcloud's own
#                             documented manual/offline install procedure.
#
# ── "kubernetes" fields ───────────────────────────────────────────────────────
#   nextcloud_rel              : Helm repo alias               (default: "nextcloud")
#   nextcloud_repo_url         : Helm repo URL                 (default:
#                             "https://nextcloud.github.io/helm/")
#   nextcloud_chart_version     : Helm chart version             (empty = latest)
#   nextcloud_namespace        : Kubernetes namespace           (default: "nextcloud")
#   nextcloud_extra_values      : [OPTIONAL] a dict of additional raw `--set key=value` pairs
#                             passed straight through to `helm upgrade --install`, for
#                             anything this addon doesn't expose its own field for.
#
# NOT live-tested in Kubernetes mode, and only smoke-tested in podman mode for the base
# Nextcloud+SQLite deployment — no real host with the extra resource budget for Talk +
# an Office Document Server + Groupware all running simultaneously was available in this
# session. Every image/app id/config key above is ground-truthed against the real upstream
# repos and official documentation referenced at the top of this file, not guessed, but the
# full combined feature surface (airgap + all apps + a deployed Document Server, together)
# has only been exercised via the mocked test suite.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "nextcloud",
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
from lab_creation import ssh_run, scp_to, helm_repo_add, die  # noqa: E402
from db_common import setup_mariadb_os, setup_postgresql_os  # noqa: E402

_DEFAULT_IMAGE = "nextcloud"
_OFFICE_SERVER_DEFAULTS = {
    "onlyoffice": ("onlyoffice/documentserver", "latest"),
    "eurooffice": ("ghcr.io/ibeacon-projekt/eurooffice-nextcloud/documentserver", "latest"),
    "collabora": ("collabora/code", "latest"),
}
_OFFICE_APP_IDS = {"onlyoffice": "onlyoffice", "eurooffice": "eurooffice", "collabora": "richdocuments"}


def _validate(v):
    v.vver("nextcloud")


def _occ(hostname, args, check=False, capture=True):
    """
    Runs one `occ` (Nextcloud's own admin CLI) command inside the running
    container — real official invocation shape, including the required
    `-u www-data` (occ refuses to run as any other user by default; the
    official image's own docs use exactly this).
    """
    return ssh_run(hostname, "podman exec -u www-data nextcloud php occ {}".format(args),
                   check=check, capture=capture)


def _install_app(hostname, app_id, cfg):
    """
    Installs+enables one Nextcloud app — from the live App Store
    (occ app:install, the normal path) or from a local pre-downloaded
    archive when nextcloud_airgap is set (see this file's own top-of-file
    schema doc for why AIO-style bundling can't do this but a plain
    container deployment can: the archive is just extracted straight into
    the apps volume, no internet access needed at any point).
    """
    r = _occ(hostname, "app:list --shipped=false", capture=True)
    if r.returncode == 0 and app_id in (r.stdout or ""):
        print("  Nextcloud app '{}' already installed — enabling only".format(app_id))
        _occ(hostname, "app:enable {}".format(app_id))
        return

    if (cfg.get("nextcloud_airgap") or "").lower() == "true":
        archive_dir = cfg.get("nextcloud_apps_archive_dir")
        if not archive_dir:
            die("nextcloud_apps_archive_dir is required when nextcloud_airgap is \"true\" — "
                "apps install from a local pre-downloaded archive, never the live App Store")
        local_path = str(Path(archive_dir) / "{}.tar.gz".format(app_id))
        if not Path(local_path).is_file():
            die("nextcloud_airgap: no local archive found for app '{}' at '{}' — download "
                "it from https://apps.nextcloud.com/apps/{} on a connected machine first "
                "and place it there".format(app_id, local_path, app_id))
        remote_tmp = "/tmp/{}.tar.gz".format(app_id)
        r = scp_to(hostname, local_path, remote_tmp)
        if r.returncode != 0:
            die("could not stage the '{}' app archive onto '{}': {}".format(
                app_id, hostname, (r.stderr or "").strip()))
        r = ssh_run(hostname, "podman cp {} nextcloud:/tmp/{}.tar.gz && "
                    "podman exec nextcloud tar -xzf /tmp/{}.tar.gz -C /var/www/html/apps/ && "
                    "podman exec nextcloud chown -R www-data:www-data /var/www/html/apps/{} && "
                    "podman exec nextcloud rm -f /tmp/{}.tar.gz".format(
                        shlex.quote(remote_tmp), app_id, app_id, shlex.quote(app_id), app_id),
                    check=False, capture=True)
        ssh_run(hostname, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
        if r.returncode != 0:
            die("could not extract the '{}' app archive inside the nextcloud container: {}".format(
                app_id, (r.stderr or r.stdout or "").strip()))
        print("  Installed Nextcloud app '{}' from a local archive (airgap mode)".format(app_id))
    else:
        r = _occ(hostname, "app:install {}".format(shlex.quote(app_id)), capture=True)
        if r.returncode != 0 and "already installed" not in (r.stdout or "") + (r.stderr or ""):
            die("could not install Nextcloud app '{}': {}".format(
                app_id, (r.stderr or r.stdout or "").strip()))
        print("  Installed Nextcloud app '{}' from the App Store".format(app_id))
    _occ(hostname, "app:enable {}".format(shlex.quote(app_id)))


def _wait_ready(hostname, port, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        r = ssh_run(hostname, "curl -sk -o /dev/null -w '%{{http_code}}' http://localhost:{}/status.php".format(
            port), check=False, capture=True)
        if r.returncode == 0 and (r.stdout or "").strip() == "200":
            return True
        time.sleep(5)
    return False


def _ensure_document_server(hostname, provider, cfg_office):
    """
    Deploys a real, standalone Document Server container for `provider`
    (onlyoffice/eurooffice/collabora) directly on `hostname` via podman —
    same "podman rm -f; podman run" pattern already used by
    install_gitlab.py/install_grafana.py. Returns (base_url, jwt_secret_or_None).
    """
    default_image, default_version = _OFFICE_SERVER_DEFAULTS[provider]
    image = cfg_office.get("server_image") or default_image
    version = cfg_office.get("server_version") or default_version
    port = int(cfg_office.get("server_port") or 8443)
    name = "nextcloud-office-{}".format(provider)

    jwt_secret = None
    env_args = ""
    if provider in ("onlyoffice", "eurooffice"):
        jwt_secret = cfg_office.get("jwt_secret") or secrets.token_urlsafe(24)
        env_args = "-e JWT_ENABLED=true -e JWT_SECRET={}".format(shlex.quote(jwt_secret))

    print("- Deploying a standalone {} Document Server on '{}' (port {})".format(
        provider, hostname, port))
    ssh_run(hostname, "podman rm -f {} 2>/dev/null".format(name), check=False)
    ssh_run(hostname, "podman run -d --name {name} --restart=always -p {port}:443 {env} "
                       "-v {name}-data:/var/www/onlyoffice/Data "
                       "{image}:{version}".format(
                           name=name, port=port, env=env_args,
                           image=shlex.quote(image), version=shlex.quote(version)))
    return "https://{}:{}".format(hostname, port), jwt_secret


def setup_nextcloud_podman(hostname, cfg, virt_srv=None):
    """
    Deploys Nextcloud as a standalone podman container directly on
    `hostname` — the official nextcloud image, plus a companion MariaDB or
    PostgreSQL installed NATIVELY on the same host (if nextcloud_db is
    "mariadb"/"postgresql") via libs/db_common.py's own setup_mariadb_os()/
    setup_postgresql_os() — the exact same install_mariadb.py/
    install_postgresql.py logic, not a re-implementation — and a companion
    Redis container (if nextcloud_redis; no shared addon exists for Redis
    in this project yet, so it stays a small inline container here, same as
    install_prometheus.py/install_grafana.py's own single-container addons).
    See this file's own top-of-file reference for why this shape (not
    Nextcloud AIO) is what supports nextcloud_airgap.
    """
    image = cfg.get("nextcloud_image") or _DEFAULT_IMAGE
    version = cfg.get("nextcloud_version") or "latest"
    nextcloud_hostname = cfg.get("nextcloud_hostname") or hostname
    http_port = int(cfg.get("nextcloud_http_port") or 8080)

    creds = ac.resolve_credential(cfg, "nextcloud", {"nextcloud_admin_password": "nextcloud_admin_password"},
                                   account_key="nextcloud_account")
    admin_user = cfg.get("nextcloud_admin_user") or "admin"
    admin_password = creds["nextcloud_admin_password"]
    if not admin_password:
        admin_password = secrets.token_urlsafe(18)
        print("  No nextcloud_admin_password configured anywhere — generated one: {}".format(
            admin_password))

    print("- Installing podman (if not already present)")
    ssh_run(hostname, "command -v podman >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y podman 2>/dev/null || "
                       "apt-get install -y podman 2>/dev/null || "
                       "dnf install -y podman 2>/dev/null)", check=False)
    ssh_run(hostname, "systemctl enable --now podman.socket", check=False)

    env_args = [
        "-e NEXTCLOUD_ADMIN_USER={}".format(shlex.quote(admin_user)),
        "-e NEXTCLOUD_ADMIN_PASSWORD={}".format(shlex.quote(admin_password)),
        "-e NEXTCLOUD_TRUSTED_DOMAINS={}".format(shlex.quote(" ".join(
            [nextcloud_hostname] + (cfg.get("nextcloud_trusted_domains") or [])))),
    ]
    if cfg.get("nextcloud_max_upload_size"):
        env_args.append("-e NEXTCLOUD_UPLOAD_LIMIT={}".format(shlex.quote(cfg["nextcloud_max_upload_size"])))

    db_kind = (cfg.get("nextcloud_db") or "sqlite").lower()
    if db_kind not in ("sqlite", "mariadb", "postgresql"):
        die("nextcloud: nextcloud_db must be 'sqlite', 'mariadb' or 'postgresql', got '{}'".format(db_kind))
    db_options = cfg.get("nextcloud_db_options") or {}
    if db_kind == "mariadb":
        db_cfg = dict({
            "mariadb_db": "nextcloud", "mariadb_user": "nextcloud",
            "mariadb_password": cfg.get("nextcloud_db_password"),
            "mariadb_root_password": cfg.get("nextcloud_db_root_password"),
            "mariadb_port": cfg.get("nextcloud_db_port"),
        }, **db_options)
        result = setup_mariadb_os(hostname, db_cfg, virt_srv)
        env_args += [
            "-e MYSQL_HOST=127.0.0.1", "-e MYSQL_PORT={}".format(result["port"]),
            "-e MYSQL_DATABASE=nextcloud", "-e MYSQL_USER=nextcloud",
            "-e MYSQL_PASSWORD={}".format(shlex.quote(result["password"])),
        ]
    elif db_kind == "postgresql":
        db_cfg = dict({
            "postgresql_db": "nextcloud", "postgresql_user": "nextcloud",
            "postgresql_password": cfg.get("nextcloud_db_password"),
            "postgresql_port": cfg.get("nextcloud_db_port"),
        }, **db_options)
        result = setup_postgresql_os(hostname, db_cfg, virt_srv)
        env_args += [
            "-e POSTGRES_HOST=127.0.0.1", "-e POSTGRES_PORT={}".format(result["port"]),
            "-e POSTGRES_DB=nextcloud", "-e POSTGRES_USER=nextcloud",
            "-e POSTGRES_PASSWORD={}".format(shlex.quote(result["password"])),
        ]

    if (cfg.get("nextcloud_redis") or "true").lower() == "true":
        redis_version = cfg.get("nextcloud_redis_version") or "latest"
        print("- Deploying a companion Redis container (real file-locking/caching, not optional "
              "in practice)")
        ssh_run(hostname, "podman rm -f nextcloud-redis 2>/dev/null", check=False)
        ssh_run(hostname, "podman run -d --name nextcloud-redis --restart=always --network host "
                           "redis:{}".format(shlex.quote(redis_version)))
        env_args += ["-e REDIS_HOST=127.0.0.1"]

    # --network host, not -p {port}:80: nextcloud_db above installs MariaDB/
    # PostgreSQL NATIVELY on this same host (via db_common.py), and Redis
    # above joins the same host network — host networking is what lets this
    # container reach either one at a plain 127.0.0.1, the identical
    # reasoning already used by install_prometheus.py/install_grafana.py for
    # their own cross-service localhost resolution. APACHE_PORT (the
    # official image's own documented env var for exactly this scenario)
    # makes Apache itself listen on nextcloud_http_port instead of the
    # image's built-in default of 80, since host networking has no
    # separate host-port/container-port mapping to remap through.
    env_args.append("-e APACHE_PORT={}".format(http_port))

    print("- Deploying the Nextcloud container")
    ssh_run(hostname, "podman rm -f nextcloud 2>/dev/null", check=False)
    ssh_run(hostname, "podman run -d --name nextcloud --restart=always --network host {env} "
                       "-v nextcloud-data:/var/www/html "
                       "{image}:{version}".format(
                           env=" ".join(env_args),
                           image=shlex.quote(image), version=shlex.quote(version)))

    print("- Generating a systemd unit so the container survives a reboot")
    unit = (
        "[Unit]\nDescription=Nextcloud (lab-in-a-box)\nAfter=network-online.target podman.socket\n"
        "Wants=network-online.target\n\n[Service]\nRestart=always\nTimeoutStartSec=180\n"
        "ExecStart=/usr/bin/podman start -a nextcloud\nExecStop=/usr/bin/podman stop -t 30 nextcloud\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    ssh_run(hostname, "cat > /etc/systemd/system/nextcloud.service <<'EOF'\n{}EOF".format(unit))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable nextcloud.service", check=False)

    print("- Waiting for Nextcloud to finish first-run setup and answer real HTTP requests "
          "(up to 3 minutes)")
    if not _wait_ready(hostname, http_port):
        print("WARNING: Nextcloud was still not answering after 3 minutes — check progress "
              "with: podman logs nextcloud")

    airgap = (cfg.get("nextcloud_airgap") or "").lower() == "true"
    if airgap:
        print("- nextcloud_airgap is set: disabling the live App Store and update checker in "
              "config.php")
        _occ(hostname, "config:system:set appstoreenabled --type=boolean --value=false")
        _occ(hostname, "config:system:set updatechecker.enabled --type=boolean --value=false")

    if (cfg.get("nextcloud_talk") or "").lower() == "true":
        _install_app(hostname, "spreed", cfg)
    if (cfg.get("nextcloud_groupware") or "").lower() == "true":
        for app_id in ("calendar", "contacts", "mail"):
            _install_app(hostname, app_id, cfg)
    if (cfg.get("nextcloud_flow") or "").lower() == "true":
        # workflowengine ships bundled with core — enable-only, no install
        # step exists (occ app:install would fail: it's not a store app).
        _occ(hostname, "app:enable workflowengine")
        print("  Enabled the bundled 'workflowengine' app (Flow)")
    if (cfg.get("nextcloud_assistant") or "").lower() == "true":
        _install_app(hostname, "assistant", cfg)
        llm_endpoint = cfg.get("nextcloud_assistant_llm_endpoint")
        if llm_endpoint:
            _install_app(hostname, "integration_openai", cfg)
            _occ(hostname, "config:app:set integration_openai url --value={}".format(
                shlex.quote(llm_endpoint)))
            print("  Wired the Assistant's integration_openai backend to '{}'".format(llm_endpoint))
    for app_id in (cfg.get("nextcloud_extra_apps") or []):
        _install_app(hostname, app_id, cfg)

    office = cfg.get("nextcloud_office")
    if office:
        provider = (office.get("provider") or "").lower()
        if provider not in _OFFICE_APP_IDS:
            die("nextcloud_office.provider must be 'onlyoffice', 'eurooffice' or 'collabora', "
                "got '{}'".format(office.get("provider")))
        app_id = _OFFICE_APP_IDS[provider]
        _install_app(hostname, app_id, cfg)

        deploy_server = (office.get("deploy_server") or "").lower() == "true"
        jwt_secret = office.get("jwt_secret")
        if deploy_server:
            base_url, generated_secret = _ensure_document_server(hostname, provider, office)
            jwt_secret = jwt_secret or generated_secret
            document_server_url = office.get("document_server_url") or base_url
            wopi_url = office.get("wopi_url") or base_url
        else:
            document_server_url = office.get("document_server_url")
            wopi_url = office.get("wopi_url")
            if provider in ("onlyoffice", "eurooffice") and not (document_server_url and jwt_secret):
                die("nextcloud_office: document_server_url and jwt_secret are required for "
                    "'{}' when deploy_server is not \"true\" — point at an already-running "
                    "Document Server".format(provider))
            if provider == "collabora" and not wopi_url:
                die("nextcloud_office: wopi_url is required for 'collabora' when deploy_server "
                    "is not \"true\" — point at an already-running Collabora Online server")

        if provider in ("onlyoffice", "eurooffice"):
            _occ(hostname, "config:app:set {} DocumentServerUrl --value={}".format(
                app_id, shlex.quote(document_server_url)))
            _occ(hostname, "config:app:set {} jwt_secret --value={}".format(
                app_id, shlex.quote(jwt_secret)))
            print("  Configured '{}' against Document Server {}".format(provider, document_server_url))
        else:
            _occ(hostname, "config:app:set richdocuments wopi_url --value={}".format(shlex.quote(wopi_url)))
            print("  Configured Collabora Online (richdocuments) against {}".format(wopi_url))

    print("Nextcloud available at: http://{}:{}".format(nextcloud_hostname, http_port))
    print("Admin login: {} / {}".format(admin_user, admin_password))


def setup_nextcloud_kubernetes(hostname, cfg):
    """
    Installs Nextcloud via its own real community Helm chart
    (nextcloud.github.io/helm) — deployed ONCE to the cluster's server
    node, matching install_gitlab.py/install_jenkins.py's own convention
    for a cluster-wide Kubernetes app.
    """
    ns = ac.require_k8s_name(cfg, "nextcloud_namespace", "nextcloud")
    domain = cfg.get("nextcloud_hostname") or hostname

    helm_repo_add(hostname, cfg.get("nextcloud_rel") or "nextcloud",
                  cfg.get("nextcloud_repo_url") or "https://nextcloud.github.io/helm/")
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)

    set_args = ["--set nextcloud.host={}".format(domain)]
    for key, value in (cfg.get("nextcloud_extra_values") or {}).items():
        set_args.append("--set {}={}".format(shlex.quote(str(key)), shlex.quote(str(value))))
    version_arg = "--version {}".format(shlex.quote(cfg["nextcloud_chart_version"])) \
        if cfg.get("nextcloud_chart_version") else ""

    print("- Installing Nextcloud via its own community Helm chart into namespace '{}' "
          "(pulls and starts nextcloud + its bundled mariadb/redis sub-charts)".format(ns))
    ssh_run(hostname, "helm upgrade --install nextcloud {rel}/nextcloud -n {ns} {version} "
                       "--timeout 600s {sets}".format(
                           rel=cfg.get("nextcloud_rel") or "nextcloud", ns=ns, version=version_arg,
                           sets=" ".join(set_args)))
    print("Nextcloud installed in namespace '{}'.".format(ns))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()
    cfg = definition.get("nextcloud", {}) or {}

    if (cfg.get("nextcloud_deployment") or "podman") == "kubernetes":
        target = k8s.first_server_node(definition)
        if not target:
            sys.exit(1)
        vm_name, _ssh_cmd = target
        setup_nextcloud_kubernetes(vm_name, cfg)
        return

    virt_srv = config.get("VIRT_SRV", "")
    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "nextcloud", vm_name=env_vm_name):
        setup_nextcloud_podman(vm_name, cfg, virt_srv)


if __name__ == "__main__":
    main()
