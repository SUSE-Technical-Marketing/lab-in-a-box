#!/usr/bin/env python3.11
# Part of lab-in-a-box. Shared OS-native MariaDB/PostgreSQL installation logic —
# lives here (not in scripts/install_mariadb.py / scripts/install_postgresql.py
# themselves) because install_automation_node_scripts.sh strips the .py suffix
# off every scripts/install_<addon> file and installs it as a bare, standalone
# executable under /usr/local/bin/ — it is never importable as a Python module.
# Any addon that needs a companion database on the same host (install_nextcloud.py,
# install_seafile.py, ...) imports setup_mariadb_os()/setup_postgresql_os() from
# here directly, instead of re-implementing its own ad-hoc database container —
# the same "logic used by >1 script belongs in libs/" convention already
# followed by spacecmd_common.py.
# Author/s: Raul Mahiques
# License: GPLv3

import re
import secrets
import shlex
import time

from lab_creation import ssh_run, ssh_output, reboot_vm, check_ssh_conn, detect_os, die

_SUSE_IDS = ("sle-micro", "slemicro")
_SUSE_ZYPPER_IDS = ("sles", "suse", "opensuse-leap", "opensuse-tumbleweed")
_RHEL_IDS = ("rhel", "centos", "rocky", "almalinux", "ol", "scientific", "fedora")
_DEBIAN_IDS = ("ubuntu", "debian", "linuxmint", "pop", "raspbian")


def digits_only(cfg, key, default, prefix):
    """
    Validate a bare-integer config value before it's interpolated unquoted
    into a remote shell command (port numbers, major-version numbers, ...).
    `prefix` is the JSON-section-qualified name to use in the error message
    (e.g. "mariadb.mariadb_port").
    """
    v = str(cfg.get(key) or default)
    if not re.match(r'^[0-9]+$', v):
        die("{} = '{}' is invalid — must be a bare number".format(prefix, v))
    return v


def _sql_lit(s):
    return "'{}'".format(str(s).replace("'", "''"))


# ─── MariaDB ─────────────────────────────────────────────────────────────────

def mariadb_install_suse(hostname):
    print("- Installing MariaDB on {} (SUSE/openSUSE)".format(hostname))
    r = ssh_run(hostname, "zypper install -y --no-confirm mariadb mariadb-client 2>&1", check=False)
    if r.returncode != 0:
        die("Could not install MariaDB via zypper on {}.".format(hostname))
    ssh_run(hostname, "systemctl enable --now mariadb.service", check=False)


def mariadb_install_slemicro(hostname, virt_srv):
    print("- Installing MariaDB on {} (SLE Micro — transactional)".format(hostname))
    r = ssh_run(hostname, "transactional-update --quiet pkg install -y mariadb mariadb-client 2>&1", check=False)
    if r.returncode != 0:
        die("Could not install MariaDB via transactional-update on {}.".format(hostname))
    print("- Rebooting {} to activate transactional changes".format(hostname))
    reboot_vm(virt_srv, hostname)
    time.sleep(5)
    check_ssh_conn(hostname)
    ssh_run(hostname, "systemctl enable --now mariadb.service", check=False)


def mariadb_install_rhel(hostname, port):
    print("- Installing MariaDB on {} (RHEL/CentOS family)".format(hostname))
    pkg_mgr = "dnf" if ssh_run(hostname, "command -v dnf", check=False, capture=True).returncode == 0 else "yum"
    r = ssh_run(hostname, "{} install -y mariadb-server 2>&1".format(pkg_mgr), check=False)
    if r.returncode != 0:
        die("Could not install mariadb-server via {} on {}.".format(pkg_mgr, hostname))
    ssh_run(hostname, "systemctl enable --now mariadb.service", check=False)
    ssh_run(hostname, (
        "if systemctl is-active firewalld &>/dev/null; then\n"
        "    firewall-cmd --permanent --add-port={}/tcp\n"
        "    firewall-cmd --reload\n"
        "fi"
    ).format(port), check=False)


def mariadb_install_debian(hostname, port):
    print("- Installing MariaDB on {} (Ubuntu/Debian family)".format(hostname))
    r = ssh_run(hostname, "DEBIAN_FRONTEND=noninteractive apt-get install -y mariadb-server 2>&1", check=False)
    if r.returncode != 0:
        die("Could not install mariadb-server via apt-get on {}.".format(hostname))
    ssh_run(hostname, "systemctl enable --now mariadb.service", check=False)
    ssh_run(hostname, (
        "if command -v ufw &>/dev/null && ufw status | grep -q 'Status: active'; then\n"
        "    ufw allow {}/tcp\n"
        "fi"
    ).format(port), check=False)


def _mariadb_configure_os(hostname, db, user, user_password, root_password, port, bind_address):
    """
    Common post-install: set the root password (fresh MariaDB installs use
    the unix_socket auth plugin for root, so no real password exists at all
    until this runs), create the configured database/user, and apply
    port/bind-address overrides.
    """
    print("- Configuring MariaDB (root password, database/user, port, bind-address)")

    def _ident(s):
        return "`{}`".format(str(s).replace("`", "``"))

    # MYSQL_PWD (an env var), never "-p<password>" as a literal argument —
    # keeps the password out of a remote `ps aux` listing.
    def _mysql_root(sql_text, use_password):
        cmd = "mysql -u root" if not use_password else \
            "MYSQL_PWD={} mysql -u root".format(shlex.quote(root_password))
        return ssh_run(hostname, cmd, input_text=sql_text + "\n", check=False)

    sql = "ALTER USER 'root'@'localhost' IDENTIFIED BY {};\n".format(_sql_lit(root_password))
    r = _mysql_root(sql, use_password=False)
    if r.returncode != 0:
        # already has a password from an earlier run of this addon
        _mysql_root(sql, use_password=True)

    def _mysql(sql_text):
        return _mysql_root(sql_text, use_password=True)

    if db:
        _mysql("CREATE DATABASE IF NOT EXISTS {};".format(_ident(db)))
    if user:
        _mysql("CREATE USER IF NOT EXISTS {}@'%' IDENTIFIED BY {};".format(_ident(user), _sql_lit(user_password)))
        if db:
            _mysql("GRANT ALL PRIVILEGES ON {}.* TO {}@'%';".format(_ident(db), _ident(user)))
        _mysql("FLUSH PRIVILEGES;")

    ssh_run(hostname, (
        "for f in /etc/my.cnf.d/*.cnf /etc/mysql/mariadb.conf.d/*.cnf; do\n"
        "    [[ -f \"$f\" ]] || continue\n"
        "    grep -q '^\\[mysqld\\]' \"$f\" || continue\n"
        "    sed -i \"/^\\[mysqld\\]/a bind-address = {bind}\\nport = {port}\" \"$f\"\n"
        "    break\n"
        "done"
    ).format(bind=bind_address, port=port), check=False)

    ssh_run(hostname,
            "systemctl restart mariadb.service 2>/dev/null || systemctl restart mysql.service 2>/dev/null || true",
            check=False)

    print("MariaDB installed on {}:{} (bind-address {})".format(hostname, port, bind_address))
    if user:
        print("Connect: mysql -h {} -P {} -u {} -p -D {}".format(hostname, port, user, db or ""))


def setup_mariadb_os(hostname, cfg, virt_srv=None):
    """
    Installs MariaDB natively on `hostname` (no container) and configures it
    per `cfg` — the "mariadb" JSON section fields documented in
    scripts/install_mariadb.py's own top-of-file schema doc. Returns
    {"root_password", "password", "port", "bind_address"} so a CALLING
    addon (install_nextcloud.py, install_seafile.py, ...) can wire its own
    app container's DB connection settings to whatever was actually
    generated/configured, without duplicating any of this installation
    logic itself.
    """
    os_info = detect_os(hostname)
    os_id = os_info["id"]
    os_like = os_info["like"].lower()
    port = digits_only(cfg, "mariadb_port", "3306", "mariadb.mariadb_port")

    if os_id in _SUSE_IDS:
        mariadb_install_slemicro(hostname, virt_srv)
    elif os_id in _SUSE_ZYPPER_IDS or os_id.startswith("opensuse"):
        mariadb_install_suse(hostname)
    elif os_id in _RHEL_IDS:
        mariadb_install_rhel(hostname, port)
    elif os_id in _DEBIAN_IDS:
        mariadb_install_debian(hostname, port)
    elif "suse" in os_like:
        mariadb_install_suse(hostname)
    elif "rhel" in os_like or "fedora" in os_like or "centos" in os_like:
        mariadb_install_rhel(hostname, port)
    elif "debian" in os_like:
        mariadb_install_debian(hostname, port)
    else:
        die("mariadb: unsupported OS '{}' (ID_LIKE='{}') on {}. Supported families: "
            "SUSE/openSUSE, RHEL/CentOS, Ubuntu/Debian.".format(os_id, os_info["like"], hostname))

    root_password = cfg.get("mariadb_root_password") or secrets.token_urlsafe(18)
    user_password = cfg.get("mariadb_password") or secrets.token_urlsafe(18)
    bind_address = cfg.get("mariadb_bind_address") or "127.0.0.1"
    _mariadb_configure_os(hostname, cfg.get("mariadb_db"), cfg.get("mariadb_user"), user_password,
                          root_password, port, bind_address)
    return {"root_password": root_password, "password": user_password, "port": port, "bind_address": bind_address}


# ─── PostgreSQL ──────────────────────────────────────────────────────────────

def _pg_lit(s):
    return _sql_lit(s)


def _pg_ident(s):
    return '"{}"'.format(str(s).replace('"', '""'))


def pg_install_suse(hostname, pg_ver):
    print("- Installing PostgreSQL {} on {} (SUSE/openSUSE)".format(pg_ver, hostname))
    r = ssh_run(hostname, "zypper install -y --no-confirm postgresql{}-server postgresql{} 2>&1".format(
        pg_ver, pg_ver), check=False)
    if r.returncode != 0:
        r = ssh_run(hostname, "zypper install -y --no-confirm postgresql-server postgresql 2>&1", check=False)
        if r.returncode != 0:
            die("Could not install PostgreSQL via zypper on {}. On SLES, ensure the 'Server "
                "Applications Module' is activated: SUSEConnect -p "
                "sle-module-server-applications/<ver>/x86_64".format(hostname))
    ssh_run(hostname, (
        "if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then\n"
        "    su - postgres -s /bin/bash -c 'initdb -D /var/lib/pgsql/data' 2>/dev/null || \\\n"
        "    postgresql{pg_ver}-setup initdb 2>/dev/null || \\\n"
        "    postgresql-setup --initdb 2>/dev/null || true\n"
        "fi"
    ).format(pg_ver=pg_ver))
    ssh_run(hostname,
            "systemctl enable --now postgresql.service 2>/dev/null || "
            "systemctl enable --now postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)


def pg_install_slemicro(hostname, pg_ver, virt_srv):
    print("- Installing PostgreSQL {} on {} (SLE Micro — transactional)".format(pg_ver, hostname))
    r = ssh_run(hostname, "transactional-update --quiet pkg install -y postgresql{}-server postgresql{} 2>&1".format(
        pg_ver, pg_ver), check=False)
    if r.returncode != 0:
        r = ssh_run(hostname, "transactional-update --quiet pkg install -y postgresql-server postgresql 2>&1",
                    check=False)
        if r.returncode != 0:
            die("Could not install PostgreSQL via transactional-update on {}.".format(hostname))
    print("- Rebooting {} to activate transactional changes".format(hostname))
    reboot_vm(virt_srv, hostname)
    time.sleep(5)
    check_ssh_conn(hostname)
    ssh_run(hostname, (
        "if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then\n"
        "    su - postgres -s /bin/bash -c 'initdb -D /var/lib/pgsql/data' 2>/dev/null || \\\n"
        "    postgresql{pg_ver}-setup initdb 2>/dev/null || true\n"
        "fi"
    ).format(pg_ver=pg_ver))
    ssh_run(hostname,
            "systemctl enable --now postgresql.service 2>/dev/null || "
            "systemctl enable --now postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)


def pg_install_rhel(hostname, pg_ver, os_info, port):
    print("- Installing PostgreSQL {} on {} (RHEL/CentOS family)".format(pg_ver, hostname))
    el_ver = os_info["ver_major"]
    pkg_mgr = "dnf" if ssh_run(hostname, "command -v dnf", check=False, capture=True).returncode == 0 else "yum"

    pgdg_rpm = ("https://download.postgresql.org/pub/repos/yum/reporpms/"
                "EL-{}-x86_64/pgdg-redhat-repo-latest.noarch.rpm".format(el_ver))
    ssh_run(hostname, "{} install -y {} 2>&1 || true".format(pkg_mgr, pgdg_rpm), check=False)
    if el_ver and int(el_ver) >= 8:
        ssh_run(hostname, "{} -qy module disable postgresql 2>/dev/null || true".format(pkg_mgr), check=False)

    r = ssh_run(hostname, "{} install -y postgresql{}-server 2>&1".format(pkg_mgr, pg_ver), check=False)
    if r.returncode != 0:
        die("Could not install postgresql{}-server on {}. Check PGDG repo availability.".format(pg_ver, hostname))

    ssh_run(hostname,
            "/usr/pgsql-{pg_ver}/bin/postgresql-{pg_ver}-setup initdb 2>/dev/null || "
            "postgresql{pg_ver}-setup initdb 2>/dev/null || true".format(pg_ver=pg_ver), check=False)
    ssh_run(hostname, "systemctl enable --now postgresql-{}.service".format(pg_ver))
    ssh_run(hostname, (
        "if systemctl is-active firewalld &>/dev/null; then\n"
        "    firewall-cmd --permanent --add-port={}/tcp\n"
        "    firewall-cmd --reload\n"
        "fi"
    ).format(port), check=False)


def pg_install_debian(hostname, pg_ver, port):
    print("- Installing PostgreSQL {} on {} (Ubuntu/Debian family)".format(pg_ver, hostname))
    ssh_run(hostname, (
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get install -y curl ca-certificates lsb-release gnupg 2>&1\n"
        "install -d /usr/share/postgresql-common/pgdg\n"
        "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc "
        "-o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n"
        "echo \"deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] "
        "https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main\" "
        "> /etc/apt/sources.list.d/pgdg.list\n"
        "apt-get update -qq"
    ))
    r = ssh_run(hostname, "DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-{} 2>&1".format(pg_ver),
                check=False)
    if r.returncode != 0:
        die("Could not install postgresql-{} on {}. Check PGDG repo availability.".format(pg_ver, hostname))
    ssh_run(hostname, (
        "if command -v ufw &>/dev/null && ufw status | grep -q 'Status: active'; then\n"
        "    ufw allow {}/tcp\n"
        "fi"
    ).format(port), check=False)


def pg_configure_os(hostname, db, user, user_password, root_password, pg_ver, port, listen):
    """
    Common post-install: set the postgres superuser password, create the
    configured database/user, and apply port/listen_addresses overrides.
    """
    print("- Configuring PostgreSQL (password, listen_addresses, pg_hba)")

    pgdata = ssh_output(hostname,
                        "su - postgres -s /bin/bash -c 'psql -t -c \"SHOW data_directory;\"' 2>/dev/null "
                        "| tr -d ' \\n'") or "/var/lib/pgsql/data"

    def _psql(sql):
        return ssh_run(hostname, "sudo -u postgres psql -v ON_ERROR_STOP=1",
                       input_text=sql + "\n", check=False)

    result = _psql("ALTER USER postgres PASSWORD {};".format(_pg_lit(root_password)))
    if result.returncode != 0:
        ssh_run(hostname, "su - postgres -s /bin/bash -c 'psql -v ON_ERROR_STOP=1'",
                input_text="ALTER USER postgres PASSWORD {};\n".format(_pg_lit(root_password)), check=False)

    if db and db != "postgres":
        _psql("CREATE DATABASE {};".format(_pg_ident(db)))
    if user and user != "postgres":
        _psql("CREATE USER {} WITH PASSWORD {};".format(_pg_ident(user), _pg_lit(user_password)))
        if db:
            _psql("GRANT ALL PRIVILEGES ON DATABASE {} TO {};".format(_pg_ident(db), _pg_ident(user)))

    ssh_run(hostname, (
        "if [[ -f '{pgdata}/postgresql.conf' ]]; then\n"
        "    sed -i \"s|^#*listen_addresses.*|listen_addresses = '{listen}'|\" '{pgdata}/postgresql.conf'\n"
        "    sed -i \"s|^#*port.*|port = {port}|\" '{pgdata}/postgresql.conf'\n"
        "fi\n"
        "if [[ -f '{pgdata}/pg_hba.conf' ]]; then\n"
        "    grep -q 'host all all 0.0.0.0/0 md5' '{pgdata}/pg_hba.conf' || \\\n"
        "        echo 'host all all 0.0.0.0/0 md5' >> '{pgdata}/pg_hba.conf'\n"
        "fi"
    ).format(pgdata=pgdata, listen=listen, port=port))

    ssh_run(hostname,
            "systemctl restart postgresql.service 2>/dev/null || "
            "systemctl restart postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)

    print("PostgreSQL installed on {}:{}".format(hostname, port))
    if user:
        print("Connect: psql -h {} -U {} -d {}".format(hostname, user, db or "postgres"))


_PG_SUSE_IDS = _SUSE_IDS
_PG_SUSE_ZYPPER_IDS = _SUSE_ZYPPER_IDS
_PG_RHEL_IDS = _RHEL_IDS
_PG_DEBIAN_IDS = _DEBIAN_IDS


def setup_postgresql_os(hostname, cfg, virt_srv=None):
    """
    Installs PostgreSQL natively on `hostname` (no container) and
    configures it per `cfg` — the "postgresql" JSON section's OS-mode
    fields documented in scripts/install_postgresql.py's own top-of-file
    schema doc. Returns {"root_password", "password", "port"} for a
    calling addon to wire its own app container's DB connection settings
    to, same contract as setup_mariadb_os() above.
    """
    os_info = detect_os(hostname)
    os_id = os_info["id"]
    pg_ver = digits_only(cfg, "postgresql_pg_version", "16", "postgresql.postgresql_pg_version")
    port = digits_only(cfg, "postgresql_port", "5432", "postgresql.postgresql_port")

    if os_id in _PG_SUSE_IDS:
        pg_install_slemicro(hostname, pg_ver, virt_srv)
    elif os_id in _PG_SUSE_ZYPPER_IDS or os_id.startswith("opensuse"):
        pg_install_suse(hostname, pg_ver)
    elif os_id in _PG_RHEL_IDS:
        pg_install_rhel(hostname, pg_ver, os_info, port)
    elif os_id in _PG_DEBIAN_IDS:
        pg_install_debian(hostname, pg_ver, port)
    else:
        os_like = os_info["like"].lower()
        if "suse" in os_like:
            pg_install_suse(hostname, pg_ver)
        elif "rhel" in os_like or "fedora" in os_like or "centos" in os_like:
            pg_install_rhel(hostname, pg_ver, os_info, port)
        elif "debian" in os_like:
            pg_install_debian(hostname, pg_ver, port)
        else:
            die("postgresql: unsupported OS '{}' (ID_LIKE='{}') on {}. Supported families: "
                "SUSE/openSUSE, RHEL/CentOS, Ubuntu/Debian.".format(os_id, os_info["like"], hostname))

    root_password = cfg.get("postgresql_password") or "postgres123"
    user_password = cfg.get("postgresql_password") or root_password
    listen = cfg.get("postgresql_listen") or "*"
    pg_configure_os(hostname, cfg.get("postgresql_db"), cfg.get("postgresql_user"), user_password,
                    root_password, pg_ver, port, listen)
    return {"root_password": root_password, "password": user_password, "port": port}
