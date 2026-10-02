#!/usr/bin/env python3
# client_registration_profile_name — see 63_client_registration_profile_name.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import spacecmd_common as sc  # noqa: E402
import install_client_registration as icr  # noqa: E402

failures = []
_real_resolve = sc._ensure_client_can_resolve_server


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def run(profile_name):
    ssh = []
    accepted = []
    pending = {"at-ct-pro", "zzsles15a.lab"}
    sc.ssh_run = lambda host, cmd, check=True, capture=False, input_text=None: ssh.append((host, cmd)) or R()
    sc.saltkey_accepted = lambda h, e, minion: minion in accepted
    sc.saltkey_pending = lambda h, e: pending
    sc.saltkey_accept = lambda h, e, minion: accepted.append(minion)
    sc._ensure_client_can_resolve_server = lambda *args: None
    sc.resolve_activation_key_name = lambda h, e, k: k
    sc.time.sleep = lambda s: None
    sc.ensure_client_registered("smlm.lab", "mgrctl exec --", "zzsles15a.lab", "smlm.lab", "1-sles15sp6",
                                profile_name=profile_name)
    return ssh, accepted


ssh, accepted = run("at-ct-pro")
boot = [cmd for host, cmd in ssh if host == "zzsles15a.lab"]
check("bootstrap runs on the node itself", len(boot) >= 1)
check("PROFILENAME passed to the bootstrap script", any("PROFILENAME=at-ct-pro" in c for c in boot))
check("salt key accepted under the profile name", accepted == ["at-ct-pro"])

ssh, accepted = run(None)
check("no PROFILENAME without a profile name", not any("PROFILENAME" in c for _, c in ssh))
check("salt key accepted under the node name by default", accepted == ["zzsles15a.lab"])

# server_ip is pinned as given, not resolved on the automation node.
pinned = []
sc.socket.gethostbyname = lambda name: "10.9.9.9"
sc.ssh_run = lambda host, cmd, check=True, capture=False, input_text=None: pinned.append(cmd) or R()
_real_resolve("c.lab", "smlm.rodeo.lab", "192.168.123.20")
check("explicit server IP pinned", any("192.168.123.20 smlm.rodeo.lab" in c for c in pinned))
pinned.clear()
_real_resolve("c.lab", "smlm.rodeo.lab")
check("without it the name is resolved locally", any("10.9.9.9 smlm.rodeo.lab" in c for c in pinned))

# The addon passes the per-node field through.
seen = {}
icr.sc.ensure_client_registered = lambda *a, **kw: seen.update(kw)
icr.sc.activation_key_exists = lambda *a: False
icr._register_now("zzsles15a.lab", {"client_registration_profile_name": "at-ct-pro",
                                   "client_registration_server_ip": "192.168.123.20"},
                  "smlm.lab", "mgrctl exec --", "smlm.lab", "1-sles15sp6")
check("addon passes client_registration_profile_name", seen.get("profile_name") == "at-ct-pro")
check("addon passes client_registration_server_ip", seen.get("server_ip") == "192.168.123.20")

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all client registration profile name checks passed")
