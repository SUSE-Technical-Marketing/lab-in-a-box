#!/usr/bin/env python3
# Targeted regression test for the 2026-09-13 uyuni-server crash-loop fix
# (libs/mgradm_common.py's _relax_health_kill_policy): mgradm bakes
# --health-on-failure=stop with a zero start-period into the systemd unit it
# generates, which killed the container mid-warm-up before it ever reached
# "healthy" — confirmed live (real AWS SMLM 5.2.0 install) via a manually-held
# container that DID reach "healthy" once nothing was killing it. The fix
# overrides mgradm's own custom.conf PODMAN_EXTRA_ARGS extension point.
# Run from 50_mgradm_health_kill_fix.sh, in its own container.
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


class _Rec:
    """Drop-in for ssh_run: records commands, returns a canned rc/stdout."""
    def __init__(self, stdout_by_cmd=None):
        self.cmds = []
        self._stdout_by_cmd = stdout_by_cmd or {}

    def __call__(self, host, cmd, **kw):
        self.cmds.append(cmd)

        class _R:
            returncode = 0
            stdout = ""
        for needle, out in self._stdout_by_cmd.items():
            if needle in cmd:
                _R.stdout = out
                break
        return _R()

    def joined(self):
        return "\n".join(self.cmds)


import mgradm_common  # noqa: E402

# ── _relax_health_kill_policy: writes the override, reloads systemd ────────
rec = _Rec()
mgradm_common.ssh_run = rec
mgradm_common._relax_health_kill_policy("vm1")
out = rec.joined()

check("writes to mgradm's own custom.conf override point (survives mgradm upgrade)",
      "/etc/systemd/system/uyuni-server.service.d/custom.conf" in out)
check("overrides --health-on-failure to none (podman's own default; mgradm opts into 'stop')",
      "--health-on-failure=none" in out)
check("gives real retries/start-period headroom",
      "--health-retries=10" in out and "--health-start-period=180s" in out)
check("PODMAN_EXTRA_ARGS is the exact env var mgradm's ExecStart line splices in",
      "PODMAN_EXTRA_ARGS=" in out)
check("raises the container's open-file ulimit as high as the host's own kernel ceiling "
      "allows (podman 4.9.5 rejects Docker's 'unlimited' magic string outright — confirmed "
      "live) — real outage found live 2026-09-22: Tomcat's default 8192 nofile limit was "
      "fully saturated under real concurrent load (14 nodes' worth of client_registration "
      "at once), failing every further connection with 'Too many open files'",
      "--ulimit nofile=1048576:1048576" in out)
check("reloads systemd so the drop-in actually takes effect",
      "systemctl daemon-reload" in out)
check("conf path itself is shell-quoted (defensive, even though it's a fixed literal)",
      shlex.quote("/etc/systemd/system/uyuni-server.service.d/custom.conf") in out)

# ── _relax_health_kill_policy also works for uyuni-db, not just the server —
# real bug found live 2026-09-14: mgradm bakes the identical
# --health-on-failure=stop into uyuni-db's own systemd unit too, and its
# baked-in healthcheck (Interval=10s, Timeout=5s, Retries=3) killed a
# perfectly healthy, multi-hour-uptime database mid-operation under
# sustained heavy write load (a long-running reposync) — taking the whole
# application down for 3+ hours with no automatic recovery, since this
# project's original fix only ever patched uyuni-server's copy of the
# identical flag. Unlike uyuni-server, uyuni-db's own .service.d/ directory
# ships with only generated.conf (no empty custom.conf placeholder already
# there) — the fix must create the directory, not just the file.
rec_db = _Rec()
mgradm_common.ssh_run = rec_db
mgradm_common._relax_health_kill_policy("vm1", "uyuni-db")
out_db = rec_db.joined()
check("writes to uyuni-db's own custom.conf override point, creating the "
      "drop-in directory first since it doesn't pre-exist there",
      "mkdir -p /etc/systemd/system/uyuni-db.service.d" in out_db
      and "/etc/systemd/system/uyuni-db.service.d/custom.conf" in out_db)
check("uyuni-db's override uses the exact same relaxed policy as uyuni-server's",
      "--health-on-failure=none" in out_db and "--health-retries=10" in out_db
      and "--health-start-period=180s" in out_db)
check("uyuni-db also gets the raised open-file ulimit (applied via the same shared "
      "function/override point, even though only uyuni-server has been observed hitting "
      "this live so far)",
      "--ulimit nofile=1048576:1048576" in out_db)

# ── _raise_in_container_service_fd_limits: the OUTER container ulimit fix
# above is NOT enough on its own — confirmed live 2026-09-22 that Tomcat's
# real java process still reported the old 8192 limit even with the outer
# container ulimit confirmed at 1048576, because Tomcat's own
# package-shipped systemd unit INSIDE the container bakes in its own
# explicit LimitNOFILE=8192, which always wins over whatever the parent
# process (the container's own PID 1) inherited.
rec_incontainer = _Rec()
mgradm_common.ssh_run = rec_incontainer
mgradm_common._raise_in_container_service_fd_limits("vm1")
out_incontainer = rec_incontainer.joined()
check("writes a LimitNOFILE override drop-in for tomcat.service INSIDE the container "
      "via podman exec -i (not the outer host-level systemd)",
      "podman exec -i uyuni-server sh -c" in out_incontainer
      and "/etc/systemd/system/tomcat.service.d" in out_incontainer
      and "LimitNOFILE=1048576" in out_incontainer)
check("also patches salt-api.service — real relevance here: salt-api handles every "
      "registered client's own check-ins, and this lab's real workload is 14 "
      "concurrently-registering nodes",
      "/etc/systemd/system/salt-api.service.d" in out_incontainer)
check("does NOT touch salt-master.service — its own cap (100000) is already generous "
      "enough to leave alone",
      "salt-master.service.d" not in out_incontainer)
check("reloads systemd INSIDE the container so the drop-ins actually take effect",
      "podman exec uyuni-server systemctl daemon-reload" in out_incontainer)
check("restarts both patched services so the new limit actually applies to a running "
      "process, not just future ones",
      "podman exec uyuni-server systemctl restart tomcat.service" in out_incontainer
      and "podman exec uyuni-server systemctl restart salt-api.service" in out_incontainer)

# ── ensure_server_container_active calls the fix BEFORE polling, for BOTH --
# ── uyuni-server AND uyuni-db ------------------------------------------------
rec2 = _Rec(stdout_by_cmd={
    "systemctl is-active uyuni-server.service": "active",
    "State.Health.Status": "healthy",
})
mgradm_common.ssh_run = rec2
mgradm_common.time.sleep = lambda *a, **kw: None
mgradm_common.ensure_server_container_active("vm1", timeout=60, poll_interval=1)
out2 = rec2.joined()

check("ensure_server_container_active applies the health-kill-policy fix itself "
      "(both install_uyuni.py and install_smlm.py get it for free)",
      "custom.conf" in out2 and "daemon-reload" in out2)
check("ensure_server_container_active ALSO relaxes uyuni-db's own copy of the same "
      "policy, not just uyuni-server's — this is what actually bit live",
      "/etc/systemd/system/uyuni-db.service.d/custom.conf" in out2)
check("the fix is applied before the is-active poll starts",
      out2.index("daemon-reload") < out2.index("systemctl is-active uyuni-server.service"))
check("ensure_server_container_active ALSO raises the in-container service fd limits "
      "once it actually confirms healthy — the outer ulimit fix alone doesn't reach "
      "Tomcat's own package-shipped systemd unit",
      "podman exec -i uyuni-server sh -c" in out2 and "LimitNOFILE=1048576" in out2)

# ── run_install_with_pg_hba_guard also pre-empts the SAME crash-loop on the
# very first boot, WHILE mgradm install is still running — confirmed live
# 2026-09-13 (fresh AWS instance, from-scratch SMLM install) that the fix
# inside ensure_server_container_active() is too late for this case: it
# only runs after mgradm install itself returns, but mgradm install can
# itself get stuck forever waiting on a container that never reaches
# "healthy" because of this exact bug.
class _RecRc(_Rec):
    def __call__(self, host, cmd, **kw):
        r = super().__call__(host, cmd, **kw)
        if "test -d /etc/systemd/system/uyuni-server.service.d" in cmd:
            r.returncode = 0
        elif "pg_isready" in cmd:
            r.returncode = 0
        elif cmd.startswith("test -f") and "mgradm_install.rc" in cmd:
            r.returncode = 0
        elif cmd.startswith("cat ") and "mgradm_install.rc" in cmd:
            r.stdout = "0\n"
        return r


rec3 = _RecRc()
mgradm_common.ssh_run = rec3
mgradm_common.time.sleep = lambda *a, **kw: None
mgradm_common.run_install_with_pg_hba_guard("vm1", "mgradm install podman --admin-login admin")
out3 = rec3.joined()

check("run_install_with_pg_hba_guard also applies the health-kill-policy fix "
      "as soon as the systemd drop-in directory exists, not just after install finishes",
      "custom.conf" in out3 and "daemon-reload" in out3)
check("restarts the service once so the freshly-patched PODMAN_EXTRA_ARGS actually take effect",
      "systemctl restart uyuni-server.service" in out3)
check("the health-kill patch happens before mgradm install is confirmed finished",
      out3.index("daemon-reload") < out3.rindex("test -f"))
check("run_install_with_pg_hba_guard ALSO relaxes uyuni-db's own health-kill policy, "
      "as soon as pg_isready succeeds — this is the container that actually got killed "
      "live, not uyuni-server",
      "/etc/systemd/system/uyuni-db.service.d/custom.conf" in out3)
check("does NOT restart uyuni-db here — it's mid-bootstrap (schema/org/admin creation "
      "happens via exec calls against it right after) and a restart now would risk the "
      "exact corruption this same function's own pg_hba-guard logic exists to avoid",
      "systemctl restart uyuni-db.service" not in out3)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mgradm_health_kill_fix checks passed")
