#!/usr/bin/env python3
# Unit tests for scripts/install_nextcloud.py. Verifies the podman-mode container invocation
# (host networking + APACHE_PORT, credential resolution, delegation to
# libs/db_common.py for a companion mariadb/postgresql instead of an ad-hoc
# container, the office/document-server integration, the airgap app-install
# path) and the kubernetes-mode Helm invocation. Run from 57_nextcloud.sh,
# in its own container — see tests/run_tests.sh.
import io
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_nextcloud as inc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def env_val(run_cmd, key):
    """Extracts a real "-e KEY=value" argument's decoded value from a full
    `podman run ...` command string via shlex.split(), not a substring
    search (same reasoning as 56_gitlab_test.py's own omnibus_config())."""
    tokens = shlex.split(run_cmd)
    for i, tok in enumerate(tokens):
        if tok == "-e" and i + 1 < len(tokens) and tokens[i + 1].startswith(key + "="):
            return tokens[i + 1][len(key) + 1:]
    return None


ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if "curl -sk" in cmd and "status.php" in cmd:
        return mock.Mock(returncode=0, stdout="200", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


inc.ssh_run = _fake_ssh_run
inc.time.sleep = lambda s: None
_fake_now = [0]
inc.time.time = lambda: _fake_now.__setitem__(0, _fake_now[0] + 100) or _fake_now[0]


def _run_podman(cfg, mariadb_result=None, postgres_result=None):
    ssh_calls.clear()
    with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
         mock.patch.object(inc, "setup_mariadb_os",
                           return_value=mariadb_result or {"root_password": "rootpw", "password": "dbpw",
                                                            "port": "3306", "bind_address": "127.0.0.1"}) as m_db, \
         mock.patch.object(inc, "setup_postgresql_os",
                           return_value=postgres_result or {"root_password": "rootpw", "password": "dbpw",
                                                            "port": "5432"}) as p_db:
        inc.setup_nextcloud_podman("host1", cfg)
    cmds = [c[1] for c in ssh_calls]
    run_cmd = next(c for c in cmds if "--name nextcloud " in c and "nextcloud-data" in c)
    return cmds, run_cmd, m_db, p_db


# ── setup_nextcloud_podman: base container invocation, sqlite default ───────
cmds, run_cmd, m_db, p_db = _run_podman({})
check("setup_nextcloud_podman: uses the real upstream nextcloud image by default, latest tag",
      "nextcloud:latest" in run_cmd)
check("setup_nextcloud_podman: uses --network host, not a port-publish, matching install_"
      "prometheus.py's/install_grafana.py's own cross-service localhost-resolution convention",
      "--network host" in run_cmd and "-p 8080:80" not in run_cmd)
check("setup_nextcloud_podman: APACHE_PORT is set to the real nextcloud_http_port default (8080) "
      "since host networking has no separate host/container port mapping to remap through",
      env_val(run_cmd, "APACHE_PORT") == "8080")
check("setup_nextcloud_podman: sqlite (the default) never calls out to either database installer",
      not m_db.called and not p_db.called)
check("setup_nextcloud_podman: deploys a companion Redis container by default, also host-networked",
      any("--name nextcloud-redis" in c and "--network host" in c for c in cmds))
check("setup_nextcloud_podman: REDIS_HOST points at the host's own loopback (host networking)",
      env_val(run_cmd, "REDIS_HOST") == "127.0.0.1")
check("setup_nextcloud_podman: creates a systemd unit so the container survives a reboot",
      any("nextcloud.service" in c and "podman start -a nextcloud" in c for c in cmds))

# ── nextcloud_db = mariadb: delegates to db_common.setup_mariadb_os(), not an
#    ad-hoc "podman run mariadb" container ───────────────────────────────────
cmds, run_cmd, m_db, p_db = _run_podman({"nextcloud_db": "mariadb", "nextcloud_db_password": "dbpw123"})
check("setup_nextcloud_podman: nextcloud_db=mariadb calls setup_mariadb_os() — the SAME code "
      "install_mariadb.py itself runs — instead of a separate ad-hoc container",
      m_db.called and not any("podman run" in c and "mariadb" in c and "--name nextcloud-db" in c
                              for c in cmds))
check("setup_nextcloud_podman: no separate MariaDB container is deployed any more (installed "
      "natively on the host instead)",
      not any(c.startswith("podman run") and "--name nextcloud-db" in c for c in cmds))
check("setup_nextcloud_podman: MYSQL_HOST points at the native install via loopback",
      env_val(run_cmd, "MYSQL_HOST") == "127.0.0.1")
check("setup_nextcloud_podman: MYSQL_PASSWORD comes from setup_mariadb_os()'s own return value, "
      "not straight from cfg unchanged",
      env_val(run_cmd, "MYSQL_PASSWORD") == "dbpw")
db_cfg_arg = m_db.call_args[0][1]
check("setup_nextcloud_podman: passes a pre-created nextcloud database/user to setup_mariadb_os() "
      "(Nextcloud's own image expects them to already exist, unlike Seafile's)",
      db_cfg_arg.get("mariadb_db") == "nextcloud" and db_cfg_arg.get("mariadb_user") == "nextcloud")

# ── nextcloud_db = postgresql: Nextcloud natively supports Postgres too ─────
cmds, run_cmd, m_db, p_db = _run_podman({"nextcloud_db": "postgresql"})
check("setup_nextcloud_podman: nextcloud_db=postgresql calls setup_postgresql_os()",
      p_db.called and not m_db.called)
check("setup_nextcloud_podman: POSTGRES_HOST/PORT come from setup_postgresql_os()'s own return",
      env_val(run_cmd, "POSTGRES_HOST") == "127.0.0.1" and env_val(run_cmd, "POSTGRES_PORT") == "5432")

# ── nextcloud_db_options passes arbitrary mariadb_*/postgresql_* keys through
cmds, run_cmd, m_db, p_db = _run_podman({
    "nextcloud_db": "mariadb", "nextcloud_db_options": {"mariadb_bind_address": "0.0.0.0"},
})
db_cfg_arg = m_db.call_args[0][1]
check("setup_nextcloud_podman: nextcloud_db_options reaches setup_mariadb_os() unchanged",
      db_cfg_arg.get("mariadb_bind_address") == "0.0.0.0")

died = False
try:
    _run_podman({"nextcloud_db": "bogus"})
except SystemExit:
    died = True
check("setup_nextcloud_podman: dies on an invalid nextcloud_db", died)

# ── Credential resolution: auto-discovered store file wins over a generated
#    password ───────────────────────────────────────────────────────────────
ssh_calls.clear()
with mock.patch.object(inc.primary, "find_service_credential_for_kind",
                        return_value=("my-nc-creds", ["my-nc-creds"])), \
     mock.patch.object(inc.primary, "load_service_credential",
                        return_value={"nextcloud_admin_password": "pw-from-store"}), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"):
    inc.setup_nextcloud_podman("host1", {})
run_cmd = next(c[1] for c in ssh_calls if "--name nextcloud " in c[1] and "nextcloud-data" in c[1])
check("setup_nextcloud_podman: auto-discovers a single matching credential-store file",
      env_val(run_cmd, "NEXTCLOUD_ADMIN_PASSWORD") == "pw-from-store")

# ── Airgap app install: stages a local archive via scp, never touches the
#    live App Store ─────────────────────────────────────────────────────────
scp_calls = []
inc.scp_to = lambda hostname, local, remote: scp_calls.append((hostname, local, remote)) or \
    mock.Mock(returncode=0, stderr="")
ssh_calls.clear()
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"), \
     mock.patch("pathlib.Path.is_file", return_value=True):
    inc.setup_nextcloud_podman("host1", {
        "nextcloud_airgap": "true", "nextcloud_apps_archive_dir": "/local/apps", "nextcloud_flow": "true",
        "nextcloud_talk": "true",
    })
cmds = [c[1] for c in ssh_calls]
check("setup_nextcloud_podman: nextcloud_airgap disables the live App Store in config.php",
      any("appstoreenabled" in c and "false" in c for c in cmds))
check("setup_nextcloud_podman: nextcloud_talk stages 'spreed' from a local archive via scp, never "
      "occ app:install (which would reach the live App Store)",
      any(local.endswith("spreed.tar.gz") for _h, local, _r in scp_calls)
      and not any("app:install spreed" in c for c in cmds))
check("setup_nextcloud_podman: nextcloud_flow only enables the bundled workflowengine app — no "
      "install step at all (it ships with core already)",
      any("app:enable workflowengine" in c for c in cmds)
      and not any(local.endswith("workflowengine.tar.gz") for _h, local, _r in scp_calls))

died = False
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"), \
     mock.patch("pathlib.Path.is_file", return_value=False):
    try:
        inc.setup_nextcloud_podman("host1", {
            "nextcloud_airgap": "true", "nextcloud_apps_archive_dir": "/local/apps",
            "nextcloud_talk": "true",
        })
    except SystemExit:
        died = True
check("setup_nextcloud_podman: dies with a clear message when an airgap archive is missing, "
      "rather than silently falling back to a live App Store install", died)

# ── Office integration: onlyoffice with an already-running Document Server
inc.scp_to = lambda *a, **kw: mock.Mock(returncode=0)
ssh_calls.clear()
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"):
    inc.setup_nextcloud_podman("host1", {
        "nextcloud_office": {
            "provider": "onlyoffice", "document_server_url": "https://docs.example.com",
            "jwt_secret": "s3cr3t-jwt",
        },
    })
cmds = [c[1] for c in ssh_calls]
check("setup_nextcloud_podman: onlyoffice with an existing server configures the real "
      "DocumentServerUrl/jwt_secret app config keys, without deploying its own server",
      any("config:app:set onlyoffice DocumentServerUrl" in c and "docs.example.com" in c for c in cmds)
      and any("config:app:set onlyoffice jwt_secret" in c and "s3cr3t-jwt" in c for c in cmds)
      and not any("nextcloud-office-onlyoffice" in c for c in cmds))

died = False
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"):
    try:
        inc.setup_nextcloud_podman("host1", {"nextcloud_office": {"provider": "onlyoffice"}})
    except SystemExit:
        died = True
check("setup_nextcloud_podman: dies when onlyoffice/eurooffice has neither deploy_server nor a "
      "document_server_url+jwt_secret pair for an existing server", died)

# Office integration: deploy_server=true stands up a real standalone Document
# Server container on the same host.
ssh_calls.clear()
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"):
    inc.setup_nextcloud_podman("host1", {
        "nextcloud_office": {"provider": "collabora", "deploy_server": "true"},
    })
cmds = [c[1] for c in ssh_calls]
check("setup_nextcloud_podman: collabora deploy_server=true stands up a real collabora/code "
      "Document Server container",
      any(c.startswith("podman run") and "nextcloud-office-collabora" in c and "collabora/code" in c
          for c in cmds))
check("setup_nextcloud_podman: collabora configures richdocuments' wopi_url against the "
      "just-deployed server",
      any("config:app:set richdocuments wopi_url" in c and "host1" in c for c in cmds))

# ── Readiness: a WARNING prints instead of a false success when Nextcloud
#    never answers ───────────────────────────────────────────────────────────
def _fake_never_ready(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if "curl -sk" in cmd and "status.php" in cmd:
        return mock.Mock(returncode=0, stdout="503", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


inc.ssh_run = _fake_never_ready
out = io.StringIO()
ssh_calls.clear()
with mock.patch.object(inc.primary, "find_service_credential_for_kind", return_value=(None, [])), \
     mock.patch.object(inc, "setup_mariadb_os"), mock.patch.object(inc, "setup_postgresql_os"), \
     redirect_stdout(out):
    inc.setup_nextcloud_podman("host1", {})
check("setup_nextcloud_podman: prints a clear warning when Nextcloud never answers, instead of "
      "silently proceeding as if it were ready",
      "WARNING" in out.getvalue())
inc.ssh_run = _fake_ssh_run


# ── setup_nextcloud_kubernetes: real Helm chart invocation ──────────────────
ssh_calls = []
inc.ssh_run = lambda hostname, cmd, **kw: ssh_calls.append((hostname, cmd, kw)) or mock.Mock(returncode=0)
inc.helm_repo_add = lambda hostname, rel, url: ssh_calls.append((hostname, "helm-repo-add:{}:{}".format(rel, url), {}))

ssh_calls.clear()
inc.setup_nextcloud_kubernetes("host1", {})
cmds = [c[1] for c in ssh_calls]
check("setup_nextcloud_kubernetes: adds the real community Helm repo (nextcloud.github.io/helm)",
      any("helm-repo-add:nextcloud:https://nextcloud.github.io/helm/" in c for c in cmds))
install_cmd = next(c for c in cmds if c.startswith("helm upgrade --install"))
check("setup_nextcloud_kubernetes: installs the real 'nextcloud/nextcloud' chart into the "
      "default 'nextcloud' namespace",
      "nextcloud/nextcloud -n nextcloud" in install_cmd)
check("setup_nextcloud_kubernetes: domain defaults to the target host's own hostname",
      "nextcloud.host=host1" in install_cmd)

ssh_calls.clear()
inc.setup_nextcloud_kubernetes("host1", {"nextcloud_extra_values": {"persistence.size": "50Gi"}})
install_cmd = next(c[1] for c in ssh_calls if c[1].startswith("helm upgrade --install"))
check("setup_nextcloud_kubernetes: nextcloud_extra_values reach the real helm invocation as --set",
      "--set persistence.size=50Gi" in install_cmd)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all nextcloud checks passed")
