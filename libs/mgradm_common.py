#!/usr/bin/env python3
# Part of lab-in-a-box. Shared mgradm and podman install mechanics, used by install_uyuni.py (Uyuni) and by the podman deployment
# mode of install_smlm.py (SUSE Multi-Linux Manager). These functions live in libs/ because install_<addon> scripts are deployed
# without their .py suffix, so one addon script cannot import another. Logic that more than one script uses belongs in libs/.
# Author/s: Raul Mahiques
# License: GPLv3
import re
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent)):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from lab_creation import ssh_run, die  # noqa: E402


def run_install_with_pg_hba_guard(hostname, install_cmd, timeout=1800, poll_interval=5):
    """
    Run `mgradm install podman` in the background, and make the uyuni-db container's pg_hba.conf accept the server's first
    connection.

    mgradm's network enables IPv6, and the auto-generated pg_hba.conf of the uyuni-db container does not trust that IPv6 subnet.
    The server's first database connection then fails, and mgradm gives up waiting, which aborts the whole install. Patching
    pg_hba.conf after the failure is not enough, because mgradm performs the schema, organization and admin bootstrap itself. The
    function patches the file as soon as uyuni-db accepts connections, before the server's first connection, so the install
    completes in one command. The rule is a permissive lab-only entry, because the database is not reachable from outside the lab
    network.

    The same timing applies to --health-on-failure=stop. The health-kill policy is relaxed as soon as the systemd drop-in
    directory appears, while mgradm still waits for the container. The container is not restarted then: mgradm aborts the install
    if its first start fails. The relaxed policy applies at the unit's next start.

    timeout is 1800 seconds. The install continues after the core bootstrap with optional services, such as attestation and tftpd,
    and that can take longer than 900 seconds.

    A timeout does not always mean the install failed. The outer mgradm install process can exit after the core bootstrap has
    finished, while it checks an optional image that the subscription is not entitled to. Before treating a timeout as a failure,
    check `mgradm status` and `systemctl is-active spacewalk.target` on the host.

    The names uyuni-db and uyuni-server are mgradm's fixed container names. They are the same for Uyuni and SMLM.
    """
    log_path = "/tmp/mgradm_install.log"
    rc_path = "/tmp/mgradm_install.rc"
    ssh_run(hostname, "rm -f {} {}".format(log_path, rc_path), check=False)
    launch = "nohup sh -c '{} ; echo $? > {}' > {} 2>&1 < /dev/null &".format(
        install_cmd, rc_path, log_path)
    ssh_run(hostname, launch, check=True)

    hba_fix = (
        "podman exec uyuni-db sh -c \""
        "printf 'host all all 0.0.0.0/0 scram-sha-256\\n"
        "host all all ::0/0 scram-sha-256\\n' > /var/lib/pgsql/data/pg_hba_custom.conf\" "
        "&& podman exec uyuni-db psql -U postgres -c 'SELECT pg_reload_conf();'"
    )
    patched = False
    health_patched = False
    finished = False
    elapsed = 0
    while elapsed < timeout:
        if not patched:
            r = ssh_run(hostname, "podman exec uyuni-db pg_isready", check=False)
            if r.returncode == 0:
                ssh_run(hostname, hba_fix, check=False)
                # Not restarted here — uyuni-db is mid-bootstrap (schema/org/
                # admin creation happens via uyuni-server's own exec calls
                # against it right after this) and a restart now would risk
                # the exact corruption this function's own docstring already
                # warns about. The drop-in still takes effect on whatever
                # restart naturally happens next (the post-install reboot
                # setup_smlm_podman()/setup_uyuni() already does shortly
                # after this function returns).
                _relax_health_kill_policy(hostname, "uyuni-db")
                patched = True
                print("  Pre-empted the known pg_hba/IPv6 race as soon as uyuni-db came up")
        if not health_patched:
            r = ssh_run(hostname, "test -d /etc/systemd/system/uyuni-server.service.d", check=False)
            if r.returncode == 0:
                # No restart: mgradm is waiting on this container's first start, and a
                # restart there aborts the install. The relaxed policy applies at the
                # unit's next start (Restart=on-success brings it back after a health-kill).
                _relax_health_kill_policy(hostname)
                health_patched = True
                print("  Pre-empted the health-kill crash-loop as soon as the unit existed")
        r = ssh_run(hostname, "test -f {}".format(rc_path), check=False)
        if r.returncode == 0:
            finished = True
            break
        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        die("mgradm install on '{}' did not finish within {}s — check {} there directly"
            .format(hostname, timeout, log_path))

    r = ssh_run(hostname, "cat {}".format(rc_path), check=False, capture=True)
    rc = (r.stdout or "").strip()
    if rc != "0":
        die("mgradm install failed on '{}' (exit {}) even with the pg_hba/IPv6 guard applied — "
            "check {} there directly".format(hostname, rc, log_path))


def _relax_health_kill_policy(hostname, service_name="uyuni-server"):
    """
    Remove the health-kill policy that mgradm sets on uyuni-server and uyuni-db, and raise the container's open-file limit.

    mgradm sets --health-on-failure=stop on both containers. With the image's own health thresholds, a container is killed after
    three failed checks. uyuni-server can fail its checks while it is still starting. uyuni-db can be killed under heavy write load,
    when one query runs longer than the health timeout. systemd then restarts the container into the same failure window, so the
    restart does not recover it.

    The fix is a static custom.conf drop-in with a PODMAN_EXTRA_ARGS override. The option is spliced into the podman run command
    before the image name, so its later --health-on-failure, --health-retries and --health-start-period values override mgradm's
    own. A static drop-in survives mgradm upgrade and a reboot.

    The same override sets --ulimit nofile. Tomcat's default of 8192 open files saturates under concurrent client registration, so
    the limit is raised to 1048576, the kernel ceiling on this host. podman does not accept the value "unlimited", so the numeric
    form is used.
    """
    conf_dir = "/etc/systemd/system/{}.service.d".format(service_name)
    override = ('[Service]\nEnvironment="PODMAN_EXTRA_ARGS=--health-on-failure=none '
                '--health-retries=10 --health-start-period=180s '
                '--ulimit nofile=1048576:1048576"\n')
    ssh_run(hostname, "mkdir -p {} && cat > {}/custom.conf <<'EOF'\n{}EOF".format(
        shlex.quote(conf_dir), shlex.quote(conf_dir), override), check=False)
    ssh_run(hostname, "systemctl daemon-reload", check=False)


def _raise_in_container_service_fd_limits(hostname):
    """
    Raise the file-descriptor limit of the services inside the uyuni-server container.

    The --ulimit override of _relax_health_kill_policy() raises only the container's own limit. Tomcat's packaged systemd unit sets
    LimitNOFILE=8192 itself, and systemd applies a unit's own limit regardless of its parent. salt-api has the same cap. salt-master's
    own limit is already high enough.

    The drop-in is written under /etc/systemd/system inside the container. That directory is not a persistent volume, so the drop-in
    is lost when the container is recreated. This function is therefore called from ensure_server_container_active(), after the
    container is confirmed healthy, and not only once at install.
    """
    override = "[Service]\nLimitNOFILE=1048576\n"
    for unit in ("tomcat.service", "salt-api.service"):
        conf_dir = "/etc/systemd/system/{}.d".format(unit)
        ssh_run(hostname,
                "podman exec -i uyuni-server sh -c {} <<'EOF'\n{}EOF".format(
                    shlex.quote("mkdir -p {0} && cat > {0}/override.conf".format(conf_dir)), override),
                check=False)
    ssh_run(hostname, "podman exec uyuni-server systemctl daemon-reload", check=False)
    for unit in ("tomcat.service", "salt-api.service"):
        ssh_run(hostname, "podman exec uyuni-server systemctl restart {}".format(unit), check=False)


# mgradm's fixed --publish list for uyuni-server, identical for Uyuni and SMLM: 80, 443, 4505, 4506, 5556 to 5557, 9800, 9187
# and 9100. netavark merges adjacent ports into ranges, so these are the --dport values that appear in the host's iptables
# nat table.
_UYUNI_SERVER_DPORTS = frozenset(("80", "443", "4505:4506", "5556:5557", "9100", "9187", "9800"))

_IPTABLES_DPORT_RE = re.compile(r"--dport (\S+)")


def _clean_stale_netavark_dnat_rules(hostname, container="uyuni-server"):
    """
    Remove stale DNAT rules for the server's published ports.

    netavark does not always remove a container's port-forwarding rules from the host's iptables nat table when the container is
    removed, including the `podman rm` that mgradm's unit runs before each restart. After several restarts the table can hold rules
    for the current container's address and for an older address that no longer exists. iptables uses the first match, so the stale
    rule wins, and connections from outside are refused.

    podman network reload does not fix this. It adds the current rule without removing the stale one.

    The function reads the container's current IP, and removes any DNAT rule for one of the server's published ports
    (_UYUNI_SERVER_DPORTS) whose target is a different address. Those ports are used only by this server, so removing their rules
    affects nothing else. A rule that already points at the current address is left alone, so the function is safe to call on every
    confirmed-healthy check.
    """
    r = ssh_run(hostname,
                "podman inspect {} --format '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}}{{{{end}}}}'"
                .format(shlex.quote(container)),
                check=False, capture=True)
    real_ip = (r.stdout or "").strip()
    if not real_ip:
        return

    r2 = ssh_run(hostname, "iptables -t nat -S", check=False, capture=True)
    for line in (r2.stdout or "").splitlines():
        if not line.startswith("-A ") or " DNAT " not in line or "--to-destination" not in line:
            continue
        if real_ip in line:
            continue
        m = _IPTABLES_DPORT_RE.search(line)
        if not m or m.group(1) not in _UYUNI_SERVER_DPORTS:
            continue
        delete_cmd = "iptables -t nat -D " + line[len("-A "):]
        ssh_run(hostname, delete_cmd, check=False)
        print("  Removed a stale netavark DNAT rule left over from a previous container "
              "instance: {}".format(delete_cmd))


def ensure_server_container_active(hostname, timeout=600, poll_interval=15, max_restarts=3):
    """
    Wait until the server container reports healthy after the post-install reboot. A plain `systemctl restart` is used when the
    container fails along the way. The function dies if the container never becomes healthy.

    Two failure modes occur after a reboot:
      1. An immediate crash. systemd's is-active check never leaves a non-active state.
      2. A delayed crash. is-active reports "active" almost at once, because podman's --sdnotify=conmon ties that state to the
         container process being alive. Two to three minutes later the healthcheck fails while the rhn web application is still
         deploying, and the health-kill policy stops the container.

    The function therefore watches podman's health state, not only the service state. The names uyuni-server (service and container)
    are mgradm's fixed names for Uyuni and SMLM alike.
    """
    _relax_health_kill_policy(hostname)
    _relax_health_kill_policy(hostname, "uyuni-db")
    restarts = 0
    elapsed = 0
    while elapsed < timeout:
        r = ssh_run(hostname, "systemctl is-active uyuni-server.service", check=False, capture=True)
        state = (r.stdout or "").strip()
        if state != "active":
            if restarts >= max_restarts:
                die("uyuni-server.service is '{}' on '{}' after {} restart attempts — "
                    "check `journalctl -u uyuni-server` there directly".format(state, hostname, restarts))
            print("  uyuni-server.service is '{}' (attempt {}/{}) — retrying with a restart"
                  .format(state, restarts + 1, max_restarts))
            ssh_run(hostname, "systemctl reset-failed uyuni-server.service", check=False)
            ssh_run(hostname, "systemctl restart uyuni-server.service", check=False)
            restarts += 1
        else:
            h = ssh_run(hostname, "podman inspect uyuni-server --format '{{.State.Health.Status}}'",
                        check=False, capture=True)
            if (h.stdout or "").strip() == "healthy":
                _clean_stale_netavark_dnat_rules(hostname)
                _raise_in_container_service_fd_limits(hostname)
                return
        time.sleep(poll_interval)
        elapsed += poll_interval
    die("uyuni-server never reported healthy on '{}' within {}s — "
        "check `journalctl -u uyuni-server` there directly".format(hostname, timeout))
