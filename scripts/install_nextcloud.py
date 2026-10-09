#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs Nextcloud, either as standalone podman containers on a host or VM, which is the only mode that supports
# airgapped installs, or on Kubernetes with Nextcloud's community Helm chart.
# Author/s: Raul Mahiques
# License: GPLv3
#
# References, from the Nextcloud GitHub organisation:
#   nextcloud/server: the core. It includes the workflowengine app, which is the Flow feature.
#   nextcloud/richdocuments: the Collabora Online integration (app id richdocuments, with wopi_url and public_wopi_url).
#   nextcloud/spreed: Nextcloud Talk (app id spreed).
#   nextcloud/calendar, contacts and mail: the Groupware bundle.
#   nextcloud/assistant: the Assistant app. It needs a text-processing backend, see nextcloud_assistant_llm_endpoint.
#   nextcloud/helm: the community Helm chart, from https://nextcloud.github.io/helm/.
#   ONLYOFFICE/onlyoffice-nextcloud: the ONLYOFFICE connector (app id onlyoffice). It needs an ONLYOFFICE Document Server with a shared JWT
#   secret. Euro-Office (ibeacon-projekt/eurooffice-nextcloud, app id eurooffice) is a separate fork with the same integration shape.
#   Nextcloud AIO is not used. The podman mode deploys the official nextcloud, mariadb and redis images directly.
#
# Schema version: 1.1
#
# JSON section: "nextcloud"
#   nextcloud_deployment : "podman" runs standalone containers on a host, and it is the only mode that supports nextcloud_airgap.
#                          "kubernetes" installs the Helm chart. (default: podman)
#
# podman fields
#   nextcloud_image           : container image (default: nextcloud)
#   nextcloud_version         : image tag (default: latest)
#   nextcloud_hostname        : the hostname that clients use. It is set as trusted_domains and OVERWRITECLI; when unset, the host's own hostname.
#   nextcloud_http_port       : host port for container port 80, chosen not to clash with another service on the node (default: 8080)
#   nextcloud_admin_user      : initial admin user, set as NEXTCLOUD_ADMIN_USER. The image creates it on first boot only (default: admin)
#   nextcloud_admin_password  : initial admin password, set as NEXTCLOUD_ADMIN_PASSWORD; when unset, generated and printed
#   nextcloud_account         : name of an encrypted credential file of kind "nextcloud" under /etc/lab_creation/credentials/ to read
#                               nextcloud_admin_password from. It is auto-discovered when exactly one such file exists and this is unset.
#   nextcloud_data_size       : size of the data volume. It is informational only, because podman volumes are not size-capped.
#   nextcloud_db              : "sqlite", "mariadb" or "postgresql" (default: sqlite). The last two are installed natively on the same host, by the code
#                               of install_mariadb.py and install_postgresql.py (libs/db_common.py). The container reaches the database at
#                               127.0.0.1, through --network host. Any mariadb_* or postgresql_* option of those addons can be passed in
#                               nextcloud_db_options, for example {"mariadb_bind_address": "0.0.0.0"}.
#   nextcloud_db_port         : database port for mariadb or postgresql; when unset, 3306 or 5432
#   nextcloud_db_password     : password of the Nextcloud database user, for mariadb or postgresql; when unset, generated and printed
#   nextcloud_db_root_password: root or superuser password, for mariadb or postgresql; when unset, generated and printed. PostgreSQL has no
#                               separate application password, so this applies in practice to mariadb.
#   nextcloud_db_options      : a dict of further mariadb_* or postgresql_* keys for the chosen database, for anything without a named field.
#   nextcloud_redis           : "true" or "false" (default: true). Deploys a Redis container, which the image uses through REDIS_HOST for file
#                               locking and caching.
#   nextcloud_redis_version   : Redis image tag (default: latest)
#   nextcloud_max_upload_size : upload limit, set as NEXTCLOUD_UPLOAD_LIMIT, for example 10G; when unset, the image's 512M
#   nextcloud_trusted_domains : extra hostnames or addresses for trusted_domains
#
# Apps, in both deployment modes
#   nextcloud_talk            : "true" or "false" (default: false). Installs and enables spreed (Talk).
#   nextcloud_groupware       : "true" or "false" (default: false). Installs and enables calendar, contacts and mail.
#   nextcloud_flow            : "true" or "false" (default: false). Enables workflowengine (Flow). It ships with the core, so nothing is installed.
#   nextcloud_assistant       : "true" or "false" (default: false). Installs and enables assistant.
#   nextcloud_assistant_llm_endpoint : an OpenAI-compatible base URL, for example the endpoint of a LiteLLM proxy addon. It is used by
#                               integration_openai, which is installed and pointed at it. Without it, the Assistant has no backend.
#   nextcloud_extra_apps      : a list of further app ids to install and enable, for example ["deck", "forms", "notes"]
#   nextcloud_office          : {"provider": "onlyoffice", "eurooffice" or "collabora"; "document_server_url" (onlyoffice and eurooffice);
#                               "wopi_url" (collabora only); "jwt_secret" (onlyoffice and eurooffice; required when the server is not deployed
#                               here); "deploy_server": "true" or "false" (false when unset), which also deploys that provider's Document Server on
#                               this host; "server_image"; "server_version" (latest when unset); "server_port" (8443 when unset)}. With deploy_server
#                               true, the URLs default to http://<hostname>:<port>. The matching app is installed and enabled.
#
# Airgapped installs (podman only)
#   nextcloud_airgap          : "true" or "false" (default: false). Every app installs from a local archive, and the live App Store is not
#                               contacted. config.php gets appstoreenabled=false and updatechecker.enabled=false. Every image must already be
#                               reachable without internet access, through a private registry mirror or a pre-pulled image on the host.
#   nextcloud_apps_archive_dir: [REQUIRED when nextcloud_airgap is true and any app is enabled] a local directory on the automation node that holds
#                               one <appid>.tar.gz per app. Download each archive from apps.nextcloud.com on a connected machine. The archives are
#                               copied to the host with scp and extracted, as Nextcloud's manual offline procedure describes.
#
# kubernetes fields
#   nextcloud_rel             : Helm repo alias (default: nextcloud)
#   nextcloud_repo_url        : Helm repo URL (default: https://nextcloud.github.io/helm/)
#   nextcloud_chart_version   : Helm chart version (empty = latest)
#   nextcloud_namespace       : Kubernetes namespace (default: nextcloud)
#   nextcloud_extra_values    : a dict of extra key=value pairs for helm upgrade --install, for anything this addon has no field for.
#

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
    Installs and enables one Nextcloud app. By default it runs occ
    app:install against the live App Store. With nextcloud_airgap set, it
    extracts a local pre-downloaded archive straight into the apps volume,
    so no internet access is needed.
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
    The podman container deployment is the only shape that supports
    nextcloud_airgap.
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
