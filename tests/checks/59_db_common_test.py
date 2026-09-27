#!/usr/bin/env python3
# Unit tests for libs/db_common.py's setup_mariadb_os() — the OS-native
# MariaDB installer extracted from install_mariadb.py 2026-09-27 so
# install_nextcloud.py/install_seafile.py can reuse the identical logic for
# their own companion database instead of an ad-hoc container. Verifies the
# real password handling (MYSQL_PWD env var, never a "-p<password>" CLI
# argument that would leak into `ps aux`), SQL-literal escaping for a
# password containing a single quote, and the per-family package-install
# dispatch. setup_postgresql_os()'s own pg_configure_os() is already covered
# by 45_addon_shell_quoting_test.py. Run from 59_db_common.sh, in its own
# container — see tests/run_tests.sh.
import shlex
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import db_common  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kw):
    ssh_calls.append((hostname, cmd, kw))
    if cmd == "mysql -u root" and kw.get("input_text", "").startswith("ALTER USER"):
        return mock.Mock(returncode=0, stdout="", stderr="")  # fresh install, unix_socket auth works
    if cmd == "command -v dnf":
        return mock.Mock(returncode=1, stdout="", stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


db_common.ssh_run = _fake_ssh_run
db_common.detect_os = lambda hostname: {"id": "opensuse-leap", "ver_id": "15.6", "like": "", "ver_major": "15"}

ssh_calls.clear()
result = db_common.setup_mariadb_os("host1", {
    "mariadb_db": "labdb", "mariadb_user": "labuser", "mariadb_password": "us3r'pw",
    "mariadb_root_password": "ro0t'pw",
})
cmds = [c[1] for c in ssh_calls]
# NOT a dict keyed by cmd string: every later authenticated mysql call shares
# the IDENTICAL "MYSQL_PWD=... mysql -u root" command text, differing only in
# input_text (the actual SQL statement) — collapsing into a {cmd: kw} dict
# would silently keep only the last one.
call_pairs = [(c[1], c[2]) for c in ssh_calls]

check("setup_mariadb_os: returns the real generated/configured root/user password, port and "
      "bind-address for a caller to wire its own app container to",
      result == {"root_password": "ro0t'pw", "password": "us3r'pw", "port": "3306",
                 "bind_address": "127.0.0.1"})
first_alter = next(kw.get("input_text", "") for cmd, kw in call_pairs if cmd == "mysql -u root")
check("setup_mariadb_os: sets the root password via a plain 'mysql -u root' unix_socket "
      "connection on first run (no password exists yet to authenticate with)",
      "mysql -u root" in cmds)
check("setup_mariadb_os: the ALTER USER statement reaches mysql via stdin, not a `mysql -e "
      "\"...\"` shell argument", "ALTER USER 'root'@'localhost'" in first_alter)
check("setup_mariadb_os: a password containing a single quote is SQL-literal-escaped ('' not "
      "raw '), not shell-escaped, in the ALTER USER statement",
      "IDENTIFIED BY 'ro0t''pw';" in first_alter)

# Every later mysql call (CREATE DATABASE/USER/GRANT) must use MYSQL_PWD, not a literal
# "-p<password>" CLI argument — keeps the password out of a `ps aux` listing.
later_calls = [(cmd, kw) for cmd, kw in call_pairs if cmd.startswith("MYSQL_PWD=")]
check("setup_mariadb_os: every authenticated mysql call passes the root password via MYSQL_PWD "
      "(an env-var prefix), never as a '-p<password>' literal command-line argument",
      len(later_calls) >= 3 and all("-pro0t" not in c and "-p'ro0t" not in c for c in cmds))
expected_pwd_prefix = "MYSQL_PWD={} mysql -u root".format(shlex.quote("ro0t'pw"))
check("setup_mariadb_os: MYSQL_PWD is shell-quoted (the root password contains a single quote)",
      any(cmd == expected_pwd_prefix for cmd, _kw in later_calls))
later_texts = [kw.get("input_text", "") for _cmd, kw in later_calls]
check("setup_mariadb_os: creates the configured database",
      any("CREATE DATABASE IF NOT EXISTS `labdb`;" in t for t in later_texts))
check("setup_mariadb_os: creates the configured user with its own SQL-literal-escaped password",
      any("IDENTIFIED BY 'us3r''pw';" in t for t in later_texts))
check("setup_mariadb_os: grants the created user privileges on the created database",
      any("GRANT ALL PRIVILEGES ON `labdb`.* TO `labuser`@'%';" in t for t in later_texts))

# ── Per-family install dispatch ──────────────────────────────────────────────
# rhel's own pkg_mgr detection genuinely runs `command -v dnf` first, real
# behavior mirrored here rather than re-mocked away — _fake_ssh_run reports
# dnf absent, so the real fallback path (yum) is what should be dispatched.
for os_id, expect_substr in (("rhel", "yum install -y mariadb-server"),
                              ("ubuntu", "apt-get install -y mariadb-server"),
                              ("sles", "zypper install -y --no-confirm mariadb")):
    ssh_calls.clear()
    db_common.detect_os = lambda hostname, _id=os_id: {"id": _id, "ver_id": "1", "like": "", "ver_major": "1"}
    db_common.setup_mariadb_os("host1", {})
    cmds = [c[1] for c in ssh_calls]
    check("setup_mariadb_os: {} dispatches to the real distro-appropriate package install".format(os_id),
          any(expect_substr in c for c in cmds))

# ── digits_only(): a bare port/version guard shared by every OS-native
#    installer (see also 18_live_bugfixes_test.py) ──────────────────────────
db_common.detect_os = lambda hostname: {"id": "opensuse-leap", "ver_id": "15.6", "like": "", "ver_major": "15"}
ssh_calls.clear()
died = False
try:
    db_common.setup_mariadb_os("host1", {"mariadb_port": "3306; rm -rf /"})
except SystemExit:
    died = True
check("setup_mariadb_os: dies rather than interpolating a shell metacharacter in mariadb_port "
      "into a remote command", died)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all db_common checks passed")
