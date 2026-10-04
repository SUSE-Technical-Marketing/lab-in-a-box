#!/usr/bin/env python3
# Pure-logic unit tests for libs/kvm_host_profiles.py.
# No real /etc/os-release read, no real subprocess/systemctl calls — all
# mocked. Run from 19_kvm_host_profiles.sh, in its own container — see
# tests/run_tests.sh.
import subprocess
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import kvm_host_profiles as khp  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _profile_for(os_info):
    with mock.patch.object(khp, "_read_os_release", return_value=os_info):
        return khp.detect_profile()


# ── (ID, major version) branch selection ──────────────────────────────────────
p = _profile_for({"ID": "opensuse-leap", "VERSION_ID": "15.6"})
check("opensuse-leap 15.6 resolves to the Leap 15 profile", isinstance(p, khp.OpenSUSELeap15Profile))
check("Leap 15 profile keeps the full package list (kubevirt-virtctl etc. included)",
      "kubevirt-virtctl" in p.packages and not p.unmapped_packages)
check("Leap 15 profile installs fuse3 (guestmount/guestunmount's own fusermount3 "
      "dependency; it is missing on a Minimal-VM Cloud host, see the packages list's own comment)",
      "fuse3" in p.packages)

p = _profile_for({"ID": "opensuse-leap", "VERSION_ID": "16.0"})
check("opensuse-leap 16.0 resolves to the Leap 16 profile", isinstance(p, khp.OpenSUSELeap16Profile))
check("Leap 16 profile's package list is genuinely different from Leap 15's",
      p.packages != khp.OpenSUSELeap15Profile.packages)
check("Leap 16 profile moves the OBS-devel-origin packages to unmapped_packages, not packages",
      "kubevirt-virtctl" not in p.packages and "kubevirt-virtctl" in p.unmapped_packages)

p = _profile_for({"ID": "sles", "VERSION_ID": "15.6"})
check("sles 15.6 resolves to the SLES 15 profile", isinstance(p, khp.SLES15Profile))
p = _profile_for({"ID": "sles", "VERSION_ID": "16.0"})
check("sles 16.0 resolves to the SLES 16 profile", isinstance(p, khp.SLES16Profile))
check("SLES 16 profile also moves the unverified packages out",
      "kubevirt-virtctl" not in p.packages and "kubevirt-virtctl" in p.unmapped_packages)

p = _profile_for({"ID": "opensuse-leap", "VERSION_ID": "17.1"})
check("an unrecognised future Leap major version falls back to the newest known branch (16)",
      isinstance(p, khp.OpenSUSELeap16Profile))

p = _profile_for({"ID": "", "ID_LIKE": "suse opensuse", "VERSION_ID": "15.6"})
check("ID_LIKE=suse fallback (no exact ID match) resolves via the opensuse-leap family",
      isinstance(p, khp.OpenSUSELeap15Profile))

p = _profile_for({"ID": "unknownos", "VERSION_ID": "1.0"})
check("a genuinely unrecognised OS returns None", p is None)

# ── Non-SUSE families unaffected by the version-branch change ─────────────────
p = _profile_for({"ID": "ubuntu", "VERSION_ID": "24.04"})
check("ubuntu still resolves to DebianProfile (no version-branch regression)",
      isinstance(p, khp.DebianProfile))
p = _profile_for({"ID": "rocky", "VERSION_ID": "9"})
check("rocky still resolves to RHELProfile", isinstance(p, khp.RHELProfile))


# ── configure_bridge(): nmcli vs. wicked live-service detection ───────────────
def _run(rc_by_service, device_conn="", conn_ipv4=None):
    """device_conn/conn_ipv4 simulate `nmcli device show <nic>` and
    `nmcli con show <existing_conn>` for configure_bridge()'s
    existing-connection-migration logic — empty/None means "no existing
    connection found on this device", matching a fresh device with nothing
    configured on it yet."""
    conn_ipv4 = conn_ipv4 or {}

    def fake_run(cmd, check=False, **kwargs):
        if cmd[:2] == ["systemctl", "is-active"]:
            svc = cmd[-1]
            return subprocess.CompletedProcess(cmd, rc_by_service.get(svc, 1))
        if cmd[:4] == ["nmcli", "-t", "-f", "GENERAL.CONNECTION"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="GENERAL.CONNECTION:{}".format(device_conn))
        if cmd[:2] == ["nmcli", "-g"]:
            field = cmd[2]
            return subprocess.CompletedProcess(cmd, 0, stdout=conn_ipv4.get(field, ""))
        return subprocess.CompletedProcess(cmd, 0, stdout="")
    return fake_run


# The test container runs Python 3.6, where unittest.mock's call.args and call.kwargs do not exist. The call_args_list entries are
# therefore indexed as tuples: call[0] is the positional tuple and call[0][0] is the first positional argument.
p = khp.OpenSUSELeap15Profile({"ID": "opensuse-leap", "VERSION_ID": "15.6"})
with mock.patch.object(subprocess, "run", side_effect=_run({"NetworkManager": 0})) as m:
    p.configure_bridge("eth0", "br0")
    calls = [c[0][0] for c in m.call_args_list]
    check("configure_bridge picks nmcli when NetworkManager is active",
          any(c[:2] == ["nmcli", "con"] for c in calls))
    check("configure_bridge with no existing connection on the NIC never tries to deactivate one",
          not any(c[:3] == ["nmcli", "con", "down"] for c in calls))

# ── configure_bridge(): migrating an existing static-IP connection ─────────
# The NIC's existing connection is migrated to the bridge. Otherwise the bridge gets no IP, and the slave connection never attaches.
p = khp.OpenSUSELeap15Profile({"ID": "opensuse-leap", "VERSION_ID": "15.6"})
with mock.patch.object(subprocess, "run", side_effect=_run(
        {"NetworkManager": 0}, device_conn="lab-static",
        conn_ipv4={"ipv4.method": "manual", "ipv4.addresses": "192.168.88.150/24",
                   "ipv4.gateway": "192.168.88.1", "ipv4.dns": "192.168.88.73"})) as m:
    p.configure_bridge("eth0", "br0")
    calls = [c[0][0] for c in m.call_args_list]
    bridge_add = next(c for c in calls if c[:5] == ["nmcli", "con", "add", "type", "bridge"])
    check("configure_bridge gives the new bridge the NIC's existing static IPv4 config",
          "ipv4.method" in bridge_add and "manual" in bridge_add
          and "192.168.88.150/24" in bridge_add and "192.168.88.1" in bridge_add)
    check("configure_bridge deactivates the NIC's old connection so the slave connection can attach",
          ["nmcli", "con", "down", "lab-static"] in calls)
    down_idx = calls.index(["nmcli", "con", "down", "lab-static"])
    slave_idx = next(i for i, c in enumerate(calls) if c[:4] == ["nmcli", "con", "add", "type"]
                      and "bridge-slave" in c)
    check("configure_bridge deactivates the old connection before adding the slave connection",
          down_idx < slave_idx)

# A device with no existing connection (nothing configured on it yet, or
# nmcli couldn't determine one) must not try to migrate a nonexistent config
# or deactivate anything by name.
p = khp.OpenSUSELeap15Profile({"ID": "opensuse-leap", "VERSION_ID": "15.6"})
with mock.patch.object(subprocess, "run", side_effect=_run({"NetworkManager": 0}, device_conn="")) as m:
    p.configure_bridge("eth0", "br0")
    calls = [c[0][0] for c in m.call_args_list]
    bridge_add = next(c for c in calls if c[:5] == ["nmcli", "con", "add", "type", "bridge"])
    check("configure_bridge with no existing connection creates a plain (DHCP-default) bridge",
          "ipv4.method" not in bridge_add)
    check("configure_bridge with no existing connection never calls 'nmcli con down'",
          not any(c[:3] == ["nmcli", "con", "down"] for c in calls))

with mock.patch.object(subprocess, "run", side_effect=_run({"NetworkManager": 1, "wickedd": 0})) as m, \
     mock.patch.object(khp.Path, "write_text") as wt:
    p.configure_bridge("eth0", "br0")
    calls = [c[0][0] for c in m.call_args_list]
    check("configure_bridge falls back to wicked when NetworkManager is inactive but wicked is live",
          any(c[:2] == ["wicked", "ifreload"] for c in calls))
    check("configure_bridge writes ifcfg files for both the bridge and the NIC under wicked",
          wt.call_count == 2)

with mock.patch.object(subprocess, "run", side_effect=_run({"NetworkManager": 1, "wickedd": 1})):
    raised = False
    try:
        p.configure_bridge("eth0", "br0")
    except NotImplementedError:
        raised = True
    check("configure_bridge raises when neither NetworkManager nor wicked is live", raised)


# ── _SuseRegisteredProfile.register_repos(): the registration code is required ──
# The base product is registered with the registration code before any module is added. Modules alone fail on an unregistered host.
p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
raised = False
with mock.patch.object(subprocess, "run") as m:
    try:
        p.register_repos()
    except RuntimeError:
        raised = True
check("register_repos without a regcode raises instead of silently skipping base registration",
      raised)
check("register_repos without a regcode never calls SUSEConnect at all", m.call_count == 0)

p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
p.regcode = "SOME-REAL-REGCODE"
with mock.patch.object(subprocess, "run") as m:
    p.register_repos()
    calls = [c[0][0] for c in m.call_args_list]
check("register_repos with a regcode set registers the base product first",
      calls and calls[0] == ["SUSEConnect", "--regcode", "SOME-REAL-REGCODE"])
check("register_repos then adds every configured module on top of the base registration",
      all(c[:2] == ["SUSEConnect", "--product"] for c in calls[1:])
      and len(calls) == 1 + len(p._products))

p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
p.regcode = "SOME-REAL-REGCODE"
p.suse_email = "me@example.com"
p.suse_url = "https://scc.suse.com"
with mock.patch.object(subprocess, "run") as m:
    p.register_repos()
    base_call = m.call_args_list[0][0][0]
check("register_repos passes --email/--url through when set",
      "--email" in base_call and "me@example.com" in base_call
      and "--url" in base_call and "https://scc.suse.com" in base_call)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all kvm_host_profiles checks passed")
