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
check("reloads systemd so the drop-in actually takes effect",
      "systemctl daemon-reload" in out)
check("conf path itself is shell-quoted (defensive, even though it's a fixed literal)",
      shlex.quote("/etc/systemd/system/uyuni-server.service.d/custom.conf") in out)

# ── ensure_server_container_active calls the fix BEFORE polling ────────────
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
check("the fix is applied before the is-active poll starts",
      out2.index("daemon-reload") < out2.index("systemctl is-active uyuni-server.service"))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mgradm_health_kill_fix checks passed")
