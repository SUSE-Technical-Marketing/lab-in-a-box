#!/usr/bin/env python3
# Regression tests for shell-quoting. Addon config values, such as passwords and database and user names, are shlex-quoted before
# they reach a remote shell through ssh_run(), so a value with quotes, semicolons or $() cannot break out of the command. Covers
# install_keycloak, install_rancher and install_postgresql. The k8s and backends sites are covered in 12_k8s and 18_live_bugfixes.
# Run from 45_addon_shell_quoting.sh, in its own container.
import shlex
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


NASTY = "p'w\" ; rm -rf / $(id) `id`"


class _Rec:
    """Drop-in for ssh_run: records commands, returns rc=0."""
    def __init__(self):
        self.cmds = []

    def __call__(self, host, cmd, **kw):
        self.cmds.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    def joined(self):
        return "\n".join(self.cmds)


# ── install_keycloak.setup_keycloak ────────────────────────────────────────
import install_keycloak  # noqa: E402
rec = _Rec()
install_keycloak.ssh_run = rec
install_keycloak.setup_keycloak("vm1", "c1", "mydemo.lab",
                                keycloak_admin="ad'min", keycloak_password=NASTY)
kc = rec.joined()
check("keycloak: adminPassword is shlex-quoted in the helm command",
      "--set auth.adminPassword={}".format(shlex.quote(NASTY)) in kc)
check("keycloak: adminUser is shlex-quoted too",
      "--set auth.adminUser={}".format(shlex.quote("ad'min")) in kc)
check("keycloak: the raw nasty payload never appears unquoted",
      "; rm -rf / $(id)" not in kc.replace(shlex.quote(NASTY), "<Q>"))


# ── db_common.pg_configure_os ────────────────────────────────────────────────
# The PostgreSQL OS configuration lives in libs/db_common.py, shared by the database addons.
import db_common  # noqa: E402
rec = _Rec()
db_common.ssh_run = rec
db_common.ssh_output = lambda *a, **kw: "/var/lib/pgsql/data"
# db/user != "postgres" so the CREATE DATABASE / CREATE USER / GRANT paths run
db_common.pg_configure_os("vm1", "labdb", "labuser", NASTY, NASTY, "16", "5432", "*")
pg = rec.joined()
check("postgresql: no `psql -c \"...\"` on the remote shell any more (all via stdin)",
      "psql -c " not in pg)
check("postgresql: the SQL statements are fed to psql, not the shell",
      any(c.strip().endswith("psql -v ON_ERROR_STOP=1") or "psql -v ON_ERROR_STOP=1'" in c
          for c in rec.cmds))


# ── install_rancher.setup_rancher (helm upgrade line only) ─────────────────
import install_rancher  # noqa: E402
rec = _Rec()
install_rancher.ssh_run = rec
# stub out everything setup_rancher does around the helm call so only that line matters
install_rancher.add_service_dns = lambda *a, **kw: None
install_rancher.check_ssh_conn = lambda *a, **kw: None
for _n in ("time",):
    if hasattr(install_rancher, _n):
        setattr(getattr(install_rancher, _n), "sleep", lambda *a, **kw: None)
try:
    install_rancher.setup_rancher(
        "vm1", {"nodes": {}, "common": {}}, "c1", "mydemo.lab", "rke2",
        {"rancher_helm_rel": "rancher", "rancher_helm_chart": "rancher-prime/rancher",
         "rancher_shorthn": "rancher", "rancher_initial_pwd": NASTY, "rancher_replicas": "2"},
    )
except SystemExit:
    pass  # setup_rancher may exit after the helm call depending on later stubs — the command is already recorded
except Exception as e:  # later steps we didn't stub — fine, the helm line is what we assert on
    print("note: setup_rancher raised after the helm call (expected, not stubbed fully): {}".format(e))
rc = rec.joined()
check("rancher: bootstrapPassword is shlex-quoted in the helm command",
      "--set bootstrapPassword={}".format(shlex.quote(NASTY)) in rc)
check("rancher: the raw nasty payload never appears unquoted",
      "; rm -rf / $(id)" not in rc.replace(shlex.quote(NASTY), "<Q>"))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all addon_shell_quoting checks passed")
