#!/usr/bin/env python3
# Regression tests for _relax_health_kill_policy() in libs/mgradm_common.py. mgradm sets --health-on-failure=stop with a zero
# start period, which can kill the container during warm-up. The fix overrides mgradm's custom.conf PODMAN_EXTRA_ARGS.
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
      "allows (podman 4.9.5 rejects Docker's 'unlimited' magic string), since the default 8192 "
      "nofile limit is saturated under concurrent load and fails connections with 'Too many open files'",
      "--ulimit nofile=1048576:1048576" in out)
check("reloads systemd so the drop-in actually takes effect",
      "systemctl daemon-reload" in out)
check("conf path itself is shell-quoted (defensive, even though it's a fixed literal)",
      shlex.quote("/etc/systemd/system/uyuni-server.service.d/custom.conf") in out)

# ── _relax_health_kill_policy: uyuni-db as well as uyuni-server ──────────────
# uyuni-db has the same health-kill flag. Its .service.d/ directory holds only generated.conf, so the fix creates the directory.
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
      "function/override point)",
      "--ulimit nofile=1048576:1048576" in out_db)

# ── _raise_in_container_service_fd_limits: services inside the container ──────
# The container's own ulimit does not reach Tomcat, because Tomcat's packaged unit sets LimitNOFILE=8192, and that limit takes precedence.
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

# ── _clean_stale_netavark_dnat_rules: stale DNAT rules ──────────────────────
# Stale DNAT rules from earlier containers can sit ahead of the current rule and refuse external connections. The function removes them.
# podman network reload does not fix this, because it adds a rule without removing the stale one.
_REAL_NAT_RULESET = "\n".join([
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 80 -j DNAT --to-destination 10.89.0.6:80",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 443 -j DNAT --to-destination 10.89.0.6:443",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 4505:4506 -j DNAT "
    "--to-destination 10.89.0.6:4505-4506/4505",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 5556:5557 -j DNAT "
    "--to-destination 10.89.0.6:5556-5557/5556",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9100 -j DNAT --to-destination 10.89.0.6:9100",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9187 -j DNAT --to-destination 10.89.0.6:9187",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9800 -j DNAT --to-destination 10.89.0.6:9800",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 80 -j DNAT --to-destination 10.89.0.7:80",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 443 -j DNAT --to-destination 10.89.0.7:443",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 4505:4506 -j DNAT "
    "--to-destination 10.89.0.7:4505-4506/4505",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 5556:5557 -j DNAT "
    "--to-destination 10.89.0.7:5556-5557/5556",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9100 -j DNAT --to-destination 10.89.0.7:9100",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9187 -j DNAT --to-destination 10.89.0.7:9187",
    "-A NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 9800 -j DNAT --to-destination 10.89.0.7:9800",
])
rec_nat = _Rec(stdout_by_cmd={
    "NetworkSettings.Networks": "10.89.0.7",
    "iptables -t nat -S": _REAL_NAT_RULESET,
})
mgradm_common.ssh_run = rec_nat
mgradm_common._clean_stale_netavark_dnat_rules("vm1")
delete_calls = [c for c in rec_nat.cmds if c.startswith("iptables -t nat -D ")]
check("removes exactly the 7 stale rules pointing at the dead container IP (10.89.0.6), "
      "one per published port",
      len(delete_calls) == 7 and all("10.89.0.6" in c for c in delete_calls))
check("never touches the correct, current rules (10.89.0.7)",
      not any("10.89.0.7" in c for c in delete_calls))
check("the delete command is a real, directly-runnable iptables -D matching the exact "
      "stale rule spec (not just the -A rule re-quoted some other way)",
      "iptables -t nat -D NETAVARK-DN-3AE499F176F7F -p tcp -m tcp --dport 80 -j DNAT "
      "--to-destination 10.89.0.6:80" in delete_calls)

# A container with no orphaned rules (the normal case) is a real no-op.
rec_nat_clean = _Rec(stdout_by_cmd={
    "NetworkSettings.Networks": "10.89.0.7",
    "iptables -t nat -S": "\n".join(l for l in _REAL_NAT_RULESET.splitlines() if "10.89.0.7" in l),
})
mgradm_common.ssh_run = rec_nat_clean
mgradm_common._clean_stale_netavark_dnat_rules("vm1")
check("no-op when every DNAT rule already points at the current container's real IP "
      "(the normal case — this runs unconditionally on every confirmed-healthy check)",
      not any(c.startswith("iptables -t nat -D ") for c in rec_nat_clean.cmds))

# A container that isn't running (no IP to compare against) is also a safe no-op.
rec_nat_norun = _Rec(stdout_by_cmd={"iptables -t nat -S": _REAL_NAT_RULESET})
mgradm_common.ssh_run = rec_nat_norun
mgradm_common._clean_stale_netavark_dnat_rules("vm1")
check("no-op (never even lists iptables rules) when the container has no real IP to "
      "compare against, rather than guessing",
      not any(c.startswith("iptables -t nat -D ") for c in rec_nat_norun.cmds)
      and not any(c.startswith("iptables -t nat -S") for c in rec_nat_norun.cmds))


# ── ensure_server_container_active calls the fix BEFORE polling, for BOTH --
# ── uyuni-server AND uyuni-db ------------------------------------------------
rec2 = _Rec(stdout_by_cmd={
    "systemctl is-active uyuni-server.service": "active",
    "State.Health.Status": "healthy",
    "NetworkSettings.Networks": "10.89.0.7",
})
mgradm_common.ssh_run = rec2
mgradm_common.time.sleep = lambda *a, **kw: None
mgradm_common.ensure_server_container_active("vm1", timeout=60, poll_interval=1)
out2 = rec2.joined()

check("ensure_server_container_active applies the health-kill-policy fix itself "
      "(both install_uyuni.py and install_smlm.py get it for free)",
      "custom.conf" in out2 and "daemon-reload" in out2)
check("ensure_server_container_active ALSO relaxes uyuni-db's own copy of the same "
      "policy, not just uyuni-server's",
      "/etc/systemd/system/uyuni-db.service.d/custom.conf" in out2)
check("the fix is applied before the is-active poll starts",
      out2.index("daemon-reload") < out2.index("systemctl is-active uyuni-server.service"))
check("ensure_server_container_active ALSO raises the in-container service fd limits "
      "once it actually confirms healthy — the outer ulimit fix alone doesn't reach "
      "Tomcat's own package-shipped systemd unit",
      "podman exec -i uyuni-server sh -c" in out2 and "LimitNOFILE=1048576" in out2)
check("ensure_server_container_active ALSO checks for stale netavark DNAT rules once "
      "confirmed healthy — the real bug that silently broke external access to the "
      "web UI for hours while every internal automation call kept working fine",
      "podman inspect uyuni-server --format" in out2 and "NetworkSettings.Networks" in out2
      and "iptables -t nat -S" in out2)

# ── run_install_with_pg_hba_guard: the health-kill policy during the install ─
# The health-kill policy is relaxed while mgradm install is still running. ensure_server_container_active() runs only after the install
# returns, so it is too late for a first boot that would otherwise never become healthy.
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
      "as soon as pg_isready succeeds",
      "/etc/systemd/system/uyuni-db.service.d/custom.conf" in out3)
check("does NOT restart uyuni-db here — it's mid-bootstrap (schema/org/admin creation "
      "happens via exec calls against it right after) and a restart now would risk the "
      "exact corruption this same function's own pg_hba-guard logic exists to avoid",
      "systemctl restart uyuni-db.service" not in out3)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mgradm_health_kill_fix checks passed")
