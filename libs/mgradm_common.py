#!/usr/bin/env python3
# Part of lab-in-a-box — shared mgradm/podman install mechanics used by BOTH
# scripts/install_uyuni.py (Uyuni) and scripts/install_smlm.py's podman
# deployment mode (SUSE Multi-Linux Manager). Moved here 2026-09-12 from
# install_uyuni.py, where these two functions originally lived: install_smlm.py
# used to do `from install_uyuni import ...` to reuse them, which worked in a
# git checkout but broke the moment either script was actually deployed —
# install_automation_node_scripts.sh deliberately strips the .py suffix from
# every install_<addon> script (so setup_lab.py's addon dispatch and the
# webui's discovery can look scripts up by their addon name), which makes the
# deployed file un-importable as a Python module (`from install_uyuni import
# ...` -> ModuleNotFoundError, confirmed live 2026-09-12 running setup_lab.py
# for real). Per this project's own established convention (logic used by
# more than one script belongs in libs/, not duplicated or cross-imported
# between scripts), the fix is to live here instead — nothing else about
# install_uyuni.py's own scope/behaviour changes; it still refers only to the
# Uyuni project, it just now imports these two from a shared, product-neutral
# library rather than defining them itself.
# Author/s: Raul Mahiques
# License: GPLv3
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent)):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from lab_creation import ssh_run, die  # noqa: E402


def run_install_with_pg_hba_guard(hostname, install_cmd, timeout=900, poll_interval=5):
    """
    Run `mgradm install podman ...` while proactively neutralizing a
    confirmed upstream mgradm/Uyuni-postgres-image race hit live
    (2026-08-28, disposable VM on nuc6.mydemo.lab, mgradm 5.3.1, podman
    5.0.3/netavark): `mgradm install podman` creates its "uyuni" podman
    network with IPv6 enabled unconditionally (even actively deletes and
    recreates an IPv4-only network to add IPv6 back — not something a
    caller can opt out of), but the uyuni-db container's auto-generated
    pg_hba.conf doesn't trust that IPv6 subnet, so uyuni-server's first DB
    connection attempt fails and mgradm's own ~15s startup wait gives up —
    aborting the ENTIRE install, not just the container start. Matches
    (with different specifics) long-standing upstream reports of the same
    underlying class of problem — e.g. uyuni-project/uyuni#10434, #10464 —
    confirmed NOT fixed by any available mgradm/postgres-image version at
    the time of testing (only one of each was available via the configured
    repo/registry).

    An earlier version of this workaround waited for `mgradm install` to
    fail, then patched pg_hba and did a plain `systemctl restart` on the
    already-created (but empty) uyuni-server container. Confirmed live
    (2026-08-28) that this is INSUFFICIENT: schema/org/admin bootstrap is
    performed by `mgradm install` itself, as part of the one command that
    just died — restarting the container only brings the Tomcat process
    back up against a completely empty database (confirmed directly on
    cutoveruyuni2.mydemo.lab: spacecmd login failed with "Invalid
    credentials", and a direct `spacewalk-sql` query showed zero rows in
    web_contact and zero tables at all — `\\dt` empty). Worse, `mgradm
    install` cannot simply be re-run afterwards either: it refuses with
    "Server is already initialized! Uninstall before attempting new
    installation or use upgrade command" as soon as its containers/volumes
    exist, even though nothing inside them was ever actually populated —
    there is no supported way to resume a `mgradm install` that died
    mid-bootstrap short of a full `mgradm uninstall` + reinstall.

    Fix: run the real `mgradm install` in the background, poll until
    uyuni-db is actually accepting connections (`pg_isready`), and patch
    pg_hba (same permissive entry as before — this is a lab-only server
    behind the automation VM's own network, not internet-facing;
    broadening trust here is not a materially different exposure than the
    0.0.0.0:5432->5432/tcp port mapping mgradm itself already publishes)
    the moment it does — before uyuni-server's first connection attempt,
    not after. On a lucky/fast run where the race doesn't trigger, this is
    a harmless no-op patch applied slightly early. Confirmed live to let
    the ONE install command complete end-to-end (network + DB + schema +
    org + admin), with no separate recovery/resume step needed.

    The database/container names here ("uyuni-db", "uyuni-server") are
    mgradm's own fixed container names — identical regardless of which
    product (Uyuni or SMLM) is being installed, since both use the same
    underlying mgradm/podman tooling; not Uyuni-specific despite the name.
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
    finished = False
    elapsed = 0
    while elapsed < timeout:
        if not patched:
            r = ssh_run(hostname, "podman exec uyuni-db pg_isready", check=False)
            if r.returncode == 0:
                ssh_run(hostname, hba_fix, check=False)
                patched = True
                print("  Pre-empted the known pg_hba/IPv6 race as soon as uyuni-db came up")
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


def _relax_health_kill_policy(hostname):
    """
    mgradm bakes `--health-on-failure=stop` into uyuni-server's systemd unit
    (confirmed live 2026-09-13, real AWS install, SMLM 5.2.0/podman 4.9.5) —
    podman's OWN default for --health-on-failure is "none"; mgradm opts into
    "stop" deliberately. Combined with the image's zero --health-start-period,
    a container that's still legitimately warming up (Tomcat deploys fine
    every cycle — confirmed via `systemctl status tomcat` inside the
    container — it just isn't answering HTTP yet) can rack up 3 consecutive
    failed health checks and get killed before it ever reaches "healthy".
    systemd's Restart=on-success (RestartUSec=100ms) then brings it straight
    back into the same warm-up window — an infinite loop, not the occasional
    one-off flake ensure_server_container_active was originally written to
    recover from via a plain restart (systemctl is-active barely ever
    reports non-"active" because the restart is near-instant, so that retry
    path never actually fires).

    Fix: mgradm's own custom.conf ships an empty PODMAN_EXTRA_ARGS
    Environment= line specifically as an upgrade-safe override point — it's
    spliced into the `podman run` line right before the image name, so a
    later --health-on-failure/--health-retries/--health-start-period here
    overrides mgradm's own earlier ones on the same command line. Verified
    live: a container left alone with no health-triggered kill reaches
    genuine "healthy" reliably ~2-3 minutes after start. custom.conf is a
    static file — survives both `mgradm upgrade` (which only rewrites
    generated.conf) and a plain reboot.
    """
    conf = "/etc/systemd/system/uyuni-server.service.d/custom.conf"
    override = ('[Service]\nEnvironment="PODMAN_EXTRA_ARGS=--health-on-failure=none '
                '--health-retries=10 --health-start-period=180s"\n')
    ssh_run(hostname, "cat > {} <<'EOF'\n{}EOF".format(shlex.quote(conf), override), check=False)
    ssh_run(hostname, "systemctl daemon-reload", check=False)


def ensure_server_container_active(hostname, timeout=600, poll_interval=15, max_restarts=3):
    """
    Confirm the server container actually reaches podman's own "healthy"
    state after the post-install reboot, retrying a plain `systemctl
    restart` if it crashes along the way — die() if it never gets there.

    Confirmed live (2026-08-28, disposable VM on nuc6.mydemo.lab), TWO
    distinct failure modes on this reboot, independent of the pg_hba/IPv6
    race above (that one is specific to the VERY FIRST start, right after
    `mgradm install`, before postgres has ever accepted a connection at
    all — these are both on a server that was already known-working moments
    earlier, restarting after a reboot):
      1. An immediate crash (systemd's `is-active` never leaves a non-
         "active" state) — the original code printed "Uyuni available at:
         ..." unconditionally here with no check at all, a confirmed false
         success report.
      2. A DELAYED crash: `is-active` reports "active" almost immediately
         (podman's own --sdnotify=conmon integration ties that to "the
         container process is alive", not to the app inside being ready),
         but 2-3 minutes later the container's own healthcheck hits
         "Error contacting Tomcat: HTTP 500" while the "rhn" webapp is
         still deploying, and `--health-on-failure=stop` (set by mgradm
         itself, not by us) kills the whole container — reproduced 3 times
         in a row. A single is-active check right after restart, as this
         function originally did, misses this entirely — it must keep
         watching for podman's health to actually settle on "healthy", not
         just for the service to have (re)started.
    Both were recoverable with a plain `systemctl restart` — this just
    needed to keep watching long enough to know one was needed.

    "uyuni-server.service"/"uyuni-server" are mgradm's own fixed
    service/container names, identical for a Uyuni or an SMLM install —
    not Uyuni-specific despite the name.
    """
    _relax_health_kill_policy(hostname)
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
                return
        time.sleep(poll_interval)
        elapsed += poll_interval
    die("uyuni-server never reported healthy on '{}' within {}s — "
        "check `journalctl -u uyuni-server` there directly".format(hostname, timeout))
