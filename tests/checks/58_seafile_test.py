#!/usr/bin/env python3
# Unit tests for scripts/install_seafile.py — no real podman/kubectl available
# in this container. Verifies the podman-mode container invocation (bridge
# networking + host.containers.internal, since — unlike install_nextcloud.py's
# --network host approach — no documented port-override env var exists for
# the seafile-mc image; delegation to libs/db_common.py for the companion
# MariaDB instead of an ad-hoc container; the real env vars the image needs)
# and the kubernetes-mode Helm invocation. Run from 58_seafile.sh, in its own
# container — see tests/run_tests.sh.
import shlex
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_seafile as isf  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def env_val(run_cmd, key):
    tokens = shlex.split(run_cmd)
    for i, tok in enumerate(tokens):
        if tok == "-e" and i + 1 < len(tokens) and tokens[i + 1].startswith(key + "="):
            return tokens[i + 1][len(key) + 1:]
    return None


ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if "curl -sk" in cmd:
        return mock.Mock(returncode=0, stdout="200", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


isf.ssh_run = _fake_ssh_run
isf.time.sleep = lambda s: None
_fake_now = [0]
isf.time.time = lambda: _fake_now.__setitem__(0, _fake_now[0] + 100) or _fake_now[0]


def _run_podman(cfg, mariadb_result=None):
    ssh_calls.clear()
    with mock.patch.object(isf.primary, "find_service_credential_for_kind", return_value=(None, [])), \
         mock.patch.object(isf, "setup_mariadb_os",
                           return_value=mariadb_result or {"root_password": "rootpw", "password": "dbpw",
                                                            "port": "3306", "bind_address": "0.0.0.0"}) as m_db:
        isf.setup_seafile_podman("host1", cfg)
    cmds = [c[1] for c in ssh_calls]
    run_cmd = next(c for c in cmds if "--name seafile " in c and "seafile-data" in c)
    return cmds, run_cmd, m_db


# ── setup_seafile_podman: base container invocation ─────────────────────────
cmds, run_cmd, m_db = _run_podman({})
check("setup_seafile_podman: uses the real upstream seafileltd/seafile-mc image, real default "
      "version tag confirmed from the actual seafile-server.yml compose file",
      "seafileltd/seafile-mc:13.0-latest" in run_cmd)
check("setup_seafile_podman: publishes port 80 directly via a plain docker/podman port-publish "
      "(the confirmed real mechanism — no documented port-override env var exists for this image)",
      "-p 8081:80" in run_cmd)
check("setup_seafile_podman: adds the real host.containers.internal add-host entry so the "
      "bridge-networked container can reach the native MariaDB/cache on the same host",
      "--add-host host.containers.internal:host-gateway" in run_cmd)
check("setup_seafile_podman: does NOT use --network host (unlike install_nextcloud.py — no "
      "confirmed port-override env var exists for this image, so a fixed 'always port 80' "
      "posture was rejected in favor of an ordinary port-publish)",
      "--network host" not in run_cmd)
check("setup_seafile_podman: delegates the companion database to setup_mariadb_os() — the SAME "
      "code install_mariadb.py itself runs — instead of an ad-hoc container",
      m_db.called and not any(c.startswith("podman run") and "--name seafile-db" in c for c in cmds))
db_cfg_arg = m_db.call_args[0][1]
check("setup_seafile_podman: opens the companion MariaDB's bind-address to 0.0.0.0, unlike "
      "install_nextcloud.py's loopback-only default — required because this container reaches "
      "it over the bridge network, not a shared host network namespace",
      db_cfg_arg.get("mariadb_bind_address") == "0.0.0.0")
check("setup_seafile_podman: does NOT pre-create a mariadb_db/mariadb_user — Seafile's own image "
      "creates its ccnet_db/seafile_db/seahub_db schema itself using root credentials",
      "mariadb_db" not in db_cfg_arg and "mariadb_user" not in db_cfg_arg)
check("setup_seafile_podman: SEAFILE_MYSQL_DB_HOST points at host.containers.internal, reachable "
      "from a bridge-networked container",
      env_val(run_cmd, "SEAFILE_MYSQL_DB_HOST") == "host.containers.internal")
check("setup_seafile_podman: INIT_SEAFILE_MYSQL_ROOT_PASSWORD comes from setup_mariadb_os()'s own "
      "return value",
      env_val(run_cmd, "INIT_SEAFILE_MYSQL_ROOT_PASSWORD") == "rootpw")
check("setup_seafile_podman: JWT_PRIVATE_KEY is set and at least 32 characters (Seafile's own "
      "documented requirement)",
      len(env_val(run_cmd, "JWT_PRIVATE_KEY") or "") >= 32)
check("setup_seafile_podman: deploys a companion redis cache container by default (CACHE_PROVIDER)",
      env_val(run_cmd, "CACHE_PROVIDER") == "redis"
      and any(c.startswith("podman run") and "--name seafile-cache" in c and "docker.io/redis" not in c
              and "redis:latest" in c for c in cmds))
check("setup_seafile_podman: creates systemd units for both containers so they survive a reboot",
      any("seafile.service" in c and "podman start -a seafile" in c for c in cmds)
      and any("seafile-cache.service" in c and "podman start -a seafile-cache" in c for c in cmds))

# ── seafile_cache_provider = memcached ───────────────────────────────────────
cmds, run_cmd, m_db = _run_podman({"seafile_cache_provider": "memcached"})
check("setup_seafile_podman: seafile_cache_provider=memcached deploys a real memcached container "
      "on its own default port 11211, and sets CACHE_PROVIDER/MEMCACHED_HOST accordingly",
      env_val(run_cmd, "CACHE_PROVIDER") == "memcached"
      and env_val(run_cmd, "MEMCACHED_HOST") == "host.containers.internal"
      and any(c.startswith("podman run") and "--name seafile-cache" in c and "-p 11211:11211" in c
              and "memcached:latest" in c for c in cmds))

died = False
try:
    _run_podman({"seafile_cache_provider": "bogus"})
except SystemExit:
    died = True
check("setup_seafile_podman: dies on an invalid seafile_cache_provider", died)

died = False
try:
    _run_podman({"seafile_jwt_private_key": "tooshort"})
except SystemExit:
    died = True
check("setup_seafile_podman: dies when seafile_jwt_private_key is under 32 characters, rather "
      "than silently sending an invalid signing key", died)

# ── Custom hostname/ports/db credentials reach the real invocation ─────────
cmds, run_cmd, m_db = _run_podman({
    "seafile_hostname": "seafile.mydemo.lab", "seafile_http_port": "9000",
    "seafile_db_user": "sfuser", "seafile_db_password": "sfpw123",
})
check("setup_seafile_podman: a custom hostname/port/db-user/db-password reach the real "
      "container invocation",
      env_val(run_cmd, "SEAFILE_SERVER_HOSTNAME") == "seafile.mydemo.lab"
      and "-p 9000:80" in run_cmd
      and env_val(run_cmd, "SEAFILE_MYSQL_DB_USER") == "sfuser"
      and env_val(run_cmd, "SEAFILE_MYSQL_DB_PASSWORD") == "sfpw123")

# ── Credential-store path: same auto-discovery convention as every other
#    addon in this project ───────────────────────────────────────────────────
ssh_calls.clear()
with mock.patch.object(isf.primary, "find_service_credential_for_kind",
                        return_value=("my-seafile-creds", ["my-seafile-creds"])), \
     mock.patch.object(isf.primary, "load_service_credential",
                        return_value={"seafile_admin_password": "pw-from-store"}), \
     mock.patch.object(isf, "setup_mariadb_os",
                       return_value={"root_password": "rootpw", "password": "dbpw", "port": "3306"}):
    isf.setup_seafile_podman("host1", {})
run_cmd = next(c[1] for c in ssh_calls if "--name seafile " in c[1] and "seafile-data" in c[1])
check("setup_seafile_podman: auto-discovers a single matching credential-store file",
      env_val(run_cmd, "INIT_SEAFILE_ADMIN_PASSWORD") == "pw-from-store")


# ── setup_seafile_kubernetes: real Helm chart invocation ────────────────────
ssh_calls = []
isf.ssh_run = lambda hostname, cmd, **kw: ssh_calls.append((hostname, cmd, kw)) or mock.Mock(returncode=0)
isf.helm_repo_add = lambda hostname, rel, url: ssh_calls.append((hostname, "helm-repo-add:{}:{}".format(rel, url), {}))

ssh_calls.clear()
isf.setup_seafile_kubernetes("host1", {})
cmds = [c[1] for c in ssh_calls]
check("setup_seafile_kubernetes: adds the real official Helm repo "
      "(haiwen.github.io/seafile-helm-chart)",
      any("helm-repo-add:seafile:https://haiwen.github.io/seafile-helm-chart/repo" in c for c in cmds))
install_cmd = next(c for c in cmds if c.startswith("helm upgrade --install"))
check("setup_seafile_kubernetes: installs the real 'seafile/ce' chart (Community Edition "
      "default) into the default 'seafile' namespace",
      "seafile/ce -n seafile" in install_cmd)
check("setup_seafile_kubernetes: a real JWT_PRIVATE_KEY/db passwords reach the chart as --set "
      "env.* overrides",
      "env.JWT_PRIVATE_KEY=" in install_cmd and "env.SEAFILE_MYSQL_DB_PASSWORD=" in install_cmd
      and "env.INIT_SEAFILE_MYSQL_ROOT_PASSWORD=" in install_cmd)

died = False
try:
    isf.setup_seafile_kubernetes("host1", {"seafile_edition": "bogus"})
except SystemExit:
    died = True
check("setup_seafile_kubernetes: dies on an invalid seafile_edition", died)

ssh_calls.clear()
isf.setup_seafile_kubernetes("host1", {
    "seafile_edition": "pro", "seafile_namespace": "custom-seafile",
    "seafile_hostname": "seafile.example.com", "seafile_extra_values": {"ingress.enabled": "true"},
})
install_cmd = next(c[1] for c in ssh_calls if c[1].startswith("helm upgrade --install"))
check("setup_seafile_kubernetes: seafile_edition=pro installs the real 'seafile/pro' chart",
      "seafile/pro -n custom-seafile" in install_cmd)
check("setup_seafile_kubernetes: custom hostname reaches the real invocation",
      "env.SEAFILE_SERVER_HOSTNAME=seafile.example.com" in install_cmd)
check("setup_seafile_kubernetes: seafile_extra_values reach the real helm invocation as --set",
      "--set ingress.enabled=true" in install_cmd)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all seafile checks passed")
