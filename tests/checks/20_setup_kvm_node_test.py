#!/usr/bin/env python3
# Mocked unit tests for setup_demo_server/setup_kvm_node.py
# — image-URL construction (Leap 15 vs. 16 filenames) and the new bridge-nic/
# extra-packages wiring in do_it_all(). No real network/subprocess calls.
# Run from 20_setup_kvm_node.sh, in its own container — see tests/run_tests.sh.
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "setup_demo_server"))

import setup_kvm_node as skn  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── download_automation_image(): URL construction ─────────────────────────────
def _download(qcow_image):
    with mock.patch.object(Path, "mkdir"), \
         mock.patch.object(Path, "exists", return_value=False), \
         mock.patch.object(urllib.request, "urlretrieve") as urlretrieve:
        skn.download_automation_image(qcow_image)
        return urlretrieve.call_args[0][0]  # the URL argument


url = _download("openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2")
check("Leap 15.6 filename builds the distribution/leap/15.6/appliances/ URL",
      url == "https://download.opensuse.org/distribution/leap/15.6/appliances/"
             "openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2")

url = _download("Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2")
check("a Leap 16.0-style filename is used verbatim (no guessed transformation) "
      "in the distribution/leap/16.0/appliances/ URL",
      url == "https://download.opensuse.org/distribution/leap/16.0/appliances/"
             "Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2")


# ── _nat_network_xml(): pure XML construction ─────────────────────────────────
xml = skn._nat_network_xml("labnat", "192.168.150.0/24")
check("_nat_network_xml names the network as given", "<name>labnat</name>" in xml)
check("_nat_network_xml sets forward mode='nat'", "<forward mode='nat'/>" in xml)
check("_nat_network_xml's gateway is the CIDR's first usable host address",
      "address='192.168.150.1'" in xml)
check("_nat_network_xml's DHCP range starts at the second usable host address",
      "start='192.168.150.2'" in xml)
check("_nat_network_xml's DHCP range ends at the CIDR's last usable host address",
      "end='192.168.150.254'" in xml)
check("_nat_network_xml derives the correct netmask for a /24",
      "netmask='255.255.255.0'" in xml)


# ── configure_nat_network(): idempotent define/start/autostart ───────────────
def _run_configure_nat_network(net_info_rc, net_list_stdout):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["virsh", "net-info"]:
            return subprocess.CompletedProcess(args, net_info_rc)
        if args[:3] == ["virsh", "net-list", "--name"]:
            return subprocess.CompletedProcess(args, 0, stdout=net_list_stdout)
        return subprocess.CompletedProcess(args, 0)

    with mock.patch.object(subprocess, "run", side_effect=fake_run), \
         mock.patch.object(os, "unlink"):
        skn.configure_nat_network("labnat", "192.168.150.0/24")
    return calls


calls = _run_configure_nat_network(net_info_rc=1, net_list_stdout="")
check("configure_nat_network defines a new network when none exists yet",
      any(c[:2] == ["virsh", "net-define"] for c in calls))
check("configure_nat_network starts the network after defining it",
      ["virsh", "net-start", "labnat"] in calls)
check("configure_nat_network marks the network autostart",
      ["virsh", "net-autostart", "labnat"] in calls)

calls = _run_configure_nat_network(net_info_rc=0, net_list_stdout="labnat\n")
check("configure_nat_network is a no-op when the network already exists and is active",
      not any(c[:2] == ["virsh", "net-define"] for c in calls))


# ── ensure_fusermount_compat(): guestmount/guestunmount's legacy-name shim ────
def _run_ensure_fusermount_compat(fusermount_exists, fusermount3_exists):
    symlink_calls = []

    def fake_exists(self):
        if str(self) == "/usr/bin/fusermount":
            return fusermount_exists
        if str(self) == "/usr/bin/fusermount3":
            return fusermount3_exists
        raise AssertionError("unexpected Path.exists() check on {}".format(self))

    def fake_symlink_to(self, target):
        symlink_calls.append((str(self), str(target)))

    with mock.patch.object(Path, "exists", fake_exists), \
         mock.patch.object(Path, "symlink_to", fake_symlink_to):
        skn.ensure_fusermount_compat()
    return symlink_calls


check("ensure_fusermount_compat is a no-op when a real fusermount already exists",
      _run_ensure_fusermount_compat(fusermount_exists=True, fusermount3_exists=True) == [])
check("ensure_fusermount_compat is a no-op when fusermount3 isn't installed either "
      "(nothing to link to — package installation's own job, not this function's)",
      _run_ensure_fusermount_compat(fusermount_exists=False, fusermount3_exists=False) == [])
check("ensure_fusermount_compat symlinks fusermount -> fusermount3 when only fusermount3 exists "
      "(openSUSE Leap ships no \"fuse\" v2 package, and guestunmount hardcodes the legacy "
      "\"fusermount\" name regardless)",
      _run_ensure_fusermount_compat(fusermount_exists=False, fusermount3_exists=True) ==
      [("/usr/bin/fusermount", "/usr/bin/fusermount3")])


# ── do_it_all(): _extra_host_pkgs / _bridge_nic wiring ────────────────────────
class _FakeProfile:
    name = "opensuse-leap-15"
    unmapped_packages = []

    def __init__(self):
        self.packages = ["libvirt", "podman"]
        self.registered = False
        self.installed_packages = None
        self.bridge_calls = []
        self.os_info = {"ID": "opensuse-leap", "PRETTY_NAME": "openSUSE Leap 15.6"}

    def register_repos(self):
        self.registered = True

    def refresh(self):
        pass

    def update(self):
        pass

    def install(self):
        self.installed_packages = list(self.packages)

    def configure_bridge(self, nic, bridge_name):
        self.bridge_calls.append((nic, bridge_name))


def _run_do_it_all(cfg, fake_profile, nat_calls=None, fusermount_compat_calls=None):
    with mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=fake_profile), \
         mock.patch.object(skn, "install_yq"), \
         mock.patch.object(skn, "ensure_fusermount_compat",
                            side_effect=(lambda: fusermount_compat_calls.append(True))
                            if fusermount_compat_calls is not None else None), \
         mock.patch.object(skn, "_find", return_value=Path("/tmp/setup_lab_automation.sh")), \
         mock.patch.object(Path, "is_file", return_value=True), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(Path, "symlink_to"), \
         mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
         mock.patch.object(skn, "download_automation_image"), \
         mock.patch.object(skn, "configure_nat_network",
                            side_effect=(lambda name, cidr: nat_calls.append((name, cidr))) if nat_calls is not None
                            else None), \
         mock.patch.object(subprocess, "run"):
        skn.do_it_all(cfg, Path("/tmp"))


fake = _FakeProfile()
fusermount_compat_calls = []
_run_do_it_all({"_bridge_nic": ""}, fake, fusermount_compat_calls=fusermount_compat_calls)
check("do_it_all calls ensure_fusermount_compat() after installing packages",
      fusermount_compat_calls == [True])

fake = _FakeProfile()
_run_do_it_all({"_extra_host_pkgs": "extra-pkg-one extra-pkg-two", "_bridge_nic": ""}, fake)
check("_extra_host_pkgs is appended to the profile's package list before install",
      fake.installed_packages == ["libvirt", "podman", "extra-pkg-one", "extra-pkg-two"])
check("configure_bridge is NOT called when _bridge_nic is empty (today's default: assume it exists)",
      fake.bridge_calls == [])

fake = _FakeProfile()
_run_do_it_all({"_bridge_nic": "eth0", "_bridge_name": "labbr0"}, fake)
check("configure_bridge IS called with the configured nic/bridge name when _bridge_nic is set",
      fake.bridge_calls == [("eth0", "labbr0")])

fake = _FakeProfile()
_run_do_it_all({"_bridge_nic": "eth0"}, fake)
check("configure_bridge defaults the bridge name to br0 when _bridge_name is unset",
      fake.bridge_calls == [("eth0", "br0")])


# ── do_it_all(): SUSE_regcode/SUSE_email/SUSE_url wiring, SLES only ─────────
# A real _SuseRegisteredProfile subclass (not _FakeProfile, which isn't one
# and so must never trigger this wiring at all — checked below too), so
# isinstance() in do_it_all() sees it correctly.
class _FakeSuseProfile(skn.kvm_host_profiles._SuseRegisteredProfile):
    name = "sles-15"
    unmapped_packages = []
    _products = ()

    def __init__(self):
        self.packages = ["libvirt"]
        self.os_info = {"ID": "sles", "PRETTY_NAME": "SUSE Linux Enterprise Server 15 SP6"}
        self.registered_calls = []

    def register_repos(self):
        self.registered_calls.append((self.regcode, self.suse_email, self.suse_url))

    def refresh(self):
        pass

    def update(self):
        pass

    def install(self):
        pass

    def configure_bridge(self, nic, bridge_name):
        pass


fake_sles = _FakeSuseProfile()
_run_do_it_all({"SUSE_regcode": "MY-REGCODE", "SUSE_email": "me@example.com",
                "SUSE_url": "https://scc.suse.com"}, fake_sles)
check("do_it_all wires SUSE_regcode/SUSE_email/SUSE_url onto a _SuseRegisteredProfile before register_repos()",
      fake_sles.registered_calls == [("MY-REGCODE", "me@example.com", "https://scc.suse.com")])

fake = _FakeProfile()
_run_do_it_all({"SUSE_regcode": "MY-REGCODE"}, fake)
check("do_it_all never sets regcode-related attributes on a non-SLES profile",
      not hasattr(fake, "regcode"))


# ── do_it_all(): _network_mode=nat wiring — extra, opt-in, never replaces bridge ──
nat_calls = []
fake = _FakeProfile()
_run_do_it_all({"_bridge_nic": "eth0", "_bridge_name": "labbr0"}, fake, nat_calls=nat_calls)
check("configure_nat_network is NOT called when _network_mode is unset (today's default: bridge)",
      nat_calls == [])
check("configure_bridge is still called normally when _network_mode is unset",
      fake.bridge_calls == [("eth0", "labbr0")])

nat_calls = []
fake = _FakeProfile()
_run_do_it_all({"_network_mode": "bridge", "_bridge_nic": "eth0"}, fake, nat_calls=nat_calls)
check("configure_nat_network is NOT called when _network_mode is explicitly \"bridge\"", nat_calls == [])

nat_calls = []
fake = _FakeProfile()
_run_do_it_all({"_network_mode": "nat"}, fake, nat_calls=nat_calls)
check("configure_nat_network IS called when _network_mode=nat, with the default name/cidr",
      nat_calls == [("labnat", "192.168.150.0/24")])
check("configure_bridge is NOT called when _network_mode=nat and _bridge_nic is unset "
      "(NAT mode needs no bridge at all)",
      fake.bridge_calls == [])

nat_calls = []
fake = _FakeProfile()
_run_do_it_all({"_network_mode": "nat", "_nat_network_name": "mylabnet",
                "_nat_network_cidr": "10.10.0.0/24"}, fake, nat_calls=nat_calls)
check("configure_nat_network uses the configured _nat_network_name/_nat_network_cidr when set",
      nat_calls == [("mylabnet", "10.10.0.0/24")])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_kvm_node checks passed")
