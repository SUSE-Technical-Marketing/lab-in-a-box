#!/usr/bin/env python3.11
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
         mock.patch.object(Path, "rename"), \
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


NET = skn.host_network.HostNet(nic="eth0", mac="52:54:00:12:34:56", address="192.168.8.20/24",
                               gateway="192.168.8.1", dhcp=False, dns=["192.168.8.53"], search=[])


# ── do_it_all(): _extra_host_pkgs / _bridge_nic wiring ────────────────────────
class _FakeProfile:
    name = "opensuse-leap-15"
    unmapped_packages = []
    verified = True

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
        return []


def _run_do_it_all(cfg, fake_profile, nat_calls=None, fusermount_compat_calls=None, downloads=None,
                   download_error=None):
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
         mock.patch.object(skn, "download_automation_image",
                           side_effect=download_error or ((lambda image: downloads.append(image))
                                                          if downloads is not None else None)), \
         mock.patch.object(skn, "configure_nat_network",
                            side_effect=(lambda name, cidr: nat_calls.append((name, cidr))) if nat_calls is not None
                            else None), \
         mock.patch.object(skn.host_network, "make_bridge",
                            side_effect=lambda bridge, nic: fake_profile.bridge_calls.append((nic, bridge)) or (NET, NET)), \
         mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
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
check("make_bridge is NOT called when _bridge_nic is empty (today's default: assume it exists)",
      fake.bridge_calls == [])

fake = _FakeProfile()
_run_do_it_all({"_bridge_nic": "eth0", "_bridge_name": "labbr0"}, fake)
check("make_bridge IS called with the configured nic/bridge name when _bridge_nic is set",
      fake.bridge_calls == [("eth0", "labbr0")])

fake = _FakeProfile()
_run_do_it_all({"_bridge_nic": "eth0"}, fake)
check("make_bridge defaults the bridge name to br0 when _bridge_name is unset",
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
        self.bridge_calls = []
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
        return []


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
check("make_bridge is still called normally when _network_mode is unset",
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
check("make_bridge is NOT called when _network_mode=nat and _bridge_nic is unset "
      "(NAT mode needs no bridge at all)",
      fake.bridge_calls == [])

nat_calls = []
fake = _FakeProfile()
_run_do_it_all({"_network_mode": "nat", "_nat_network_name": "mylabnet",
                "_nat_network_cidr": "10.10.0.0/24"}, fake, nat_calls=nat_calls)
check("configure_nat_network uses the configured _nat_network_name/_nat_network_cidr when set",
      nat_calls == [("mylabnet", "10.10.0.0/24")])


def _virsh_calls(fn, rc_by_cmd, stdout=""):
    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)
        rc = next((rc for prefix, rc in rc_by_cmd.items() if cmd[:len(prefix)] == list(prefix)), 0)
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout)
    with mock.patch.object(subprocess, "run", side_effect=fake_run), mock.patch.object(Path, "mkdir"):
        fn()
    return calls


calls = _virsh_calls(skn.enable_libvirt, {("systemctl", "cat"): 0})
check("enable_libvirt starts libvirtd where it is installed", calls[-1] == ["systemctl", "enable", "--now", "libvirtd"])
calls = _virsh_calls(skn.enable_libvirt, {("systemctl", "cat", "libvirtd.service"): 1,
                                          ("systemctl", "cat", "virtinterfaced.socket"): 1})
check("enable_libvirt starts only the installed per-driver daemons where libvirtd is absent",
      calls[-1][:3] == ["systemctl", "enable", "--now"] and "virtqemud.socket" in calls[-1]
      and "virtinterfaced.socket" not in calls[-1])
raised = False
with mock.patch.object(sys, "stderr"):
    try:
        _virsh_calls(skn.enable_libvirt, {("systemctl", "cat"): 1})
    except SystemExit:
        raised = True
check("enable_libvirt stops when neither libvirtd nor virtqemud is installed", raised)
calls = _virsh_calls(skn.define_storage_pool, {("virsh", "pool-info"): 1})
check("define_storage_pool defines, autostarts and starts a missing pool",
      calls[1][:2] == ["virsh", "pool-define"] and ["virsh", "pool-autostart", "pool"] in calls
      and calls[-1] == ["virsh", "pool-start", "pool"])
calls = _virsh_calls(skn.define_storage_pool, {}, stdout="State:          running\n")
check("define_storage_pool leaves an existing running pool alone apart from autostart",
      not any(c[1] in ("pool-define", "pool-start") for c in calls if c[0] == "virsh"))

fake = _FakeProfile()
_run_do_it_all({"_network_mode": "nat", "_bridge_nic": "eth0"}, fake, nat_calls=[])
check("make_bridge is NOT called in nat mode even when _bridge_nic is set", fake.bridge_calls == [])


# ── lab.cfg generation ────────────────────────────────────────────────────────
import argparse  # noqa: E402
import ipaddress  # noqa: E402
import tempfile  # noqa: E402

check("reverse_zone of a /24 is its three octets reversed",
      skn.reverse_zone(ipaddress.ip_network("192.168.8.0/24")) == "8.168.192")
check("reverse_zone of a /16 is its two octets reversed",
      skn.reverse_zone(ipaddress.ip_network("10.20.0.0/16")) == "20.10")

template = (_REPO / "setup_demo_server" / "lab.cfg.template").read_text()
out = skn.render_lab_cfg(template, {"_mynet": "10.0.0.0/24", "ROOT_PWD_HASH": "$6$ab$cd",
                                    "ROOT_SSH_PUB_KEY": "ssh-ed25519 AAAA root@host", "_bridge_nic": "eth0"})
check("render_lab_cfg replaces a value and keeps the line's trailing comment",
      "_mynet=10.0.0.0/24          # CIDR notation" in out)
check("render_lab_cfg single-quotes values that need it, so bash sources them literally",
      "ROOT_PWD_HASH='$6$ab$cd'" in out and "ROOT_SSH_PUB_KEY='ssh-ed25519 AAAA root@host'" in out)
check("render_lab_cfg replaces a quoted empty value", "\n_bridge_nic=eth0\n" in out)
check("render_lab_cfg keeps every other line and comment", out.count("\n") == template.count("\n")
      and "_mydomain=mydemo.lab" in out)
check("the template's keys all match render_lab_cfg's KEY=value pattern",
      skn.render_lab_cfg(template, {k: "X" for k in ("_mydns", "_mygw", "_myip", "_mynet", "_mydomain",
                                                    "_mynetrev", "ROOT_SSH_PUB_KEY", "ROOT_PWD_HASH",
                                                    "root_pwd", "AUTOMATION_HOSTNAME", "MYREG", "_timezone",
                                                    "_bridge_name", "_bridge_nic", "_network_mode",
                                                    "_nat_network_cidr", "_virt_srv", "SUSE_regcode",
                                                    "SUSE_email", "SUSE_url")}).count("=X") == 20)



def _values(argv, profile="opensuse-leap-15", bridged=False):
    args = skn.parse_args(argv)
    with mock.patch.object(skn.host_network, "current_settings", return_value=NET), \
         mock.patch.object(skn.host_network, "is_bridge", return_value=bridged), \
         mock.patch.object(skn, "suggest_free_ip", return_value="192.168.8.240"), \
         mock.patch.object(skn, "ensure_ssh_key", return_value="ssh-ed25519 AAAA root@host"), \
         mock.patch.object(skn, "password_hash", side_effect=lambda pw: "HASH(" + pw + ")"), \
         mock.patch.object(skn, "detect_timezone", return_value="Europe/Zurich"):
        return skn.build_values(args, skn.Asker(interactive=False), profile)


v, generated = _values([])
check("bridge mode: the lab network, gateway and DNS come from the host",
      v["_mynet"] == "192.168.8.0/24" and v["_mygw"] == "192.168.8.1" and v["_mydns"] == "192.168.8.53"
      and v["_mynetrev"] == "8.168.192")
check("bridge mode: the default-route NIC becomes the port of br0",
      v["_bridge_name"] == "br0" and v["_bridge_nic"] == "eth0" and v["_network_mode"] == "bridge")
check("bridge mode: the automation VM gets a free address and the domain-derived hostnames",
      v["_myip"] == "192.168.8.240" and v["AUTOMATION_HOSTNAME"] == "automation.mydemo.lab"
      and v["MYREG"] == "registry.mydemo.lab" and v["_virt_srv"] == "root@192.168.8.20")
check("non-interactive with no password: one is generated, hashed and returned for printing",
      generated and v["root_pwd"] == generated and v["ROOT_PWD_HASH"] == "HASH({})".format(generated))
check("non-SLES hosts get no SUSE registration keys", "SUSE_regcode" not in v)

v, _ = _values([], bridged=True)
check("a host already on a bridge keeps it and creates none", v["_bridge_name"] == "eth0" and v["_bridge_nic"] == "")

v, generated = _values(["--network-mode", "nat", "--domain", "lab.example",
                        "--root-password", "s3cret"], profile="sles-16")
check("nat mode: the lab network is the NAT range, with .1 as gateway/DNS and .10 for the automation VM",
      v["_mynet"] == "192.168.150.0/24" and v["_mygw"] == "192.168.150.1" and v["_mydns"] == "192.168.150.1"
      and v["_myip"] == "192.168.150.10" and v["_bridge_nic"] == "" and v["_virt_srv"] == "root@192.168.150.1")
check("a given password is used and not reported as generated",
      v["root_pwd"] == "s3cret" and generated == "" and v["AUTOMATION_HOSTNAME"] == "automation.lab.example")
check("SLES hosts get the SUSE registration keys", v["SUSE_regcode"] == "")

check("without --interactive nothing is asked, confirmation included", skn.parse_args([]).yes and not skn.parse_args([]).interactive)
check("--interactive asks for confirmation unless -y is given", not skn.parse_args(["--interactive"]).yes and skn.parse_args(["--interactive", "-y"]).yes)
check("the old --non-interactive option is still accepted", skn.parse_args(["--non-interactive"]).yes)
check("--share-storage-from without a value means 'use _virt_srv'", skn.parse_args(["--share-storage-from"]).share_storage_from == "")
raised = False
with mock.patch.object(sys, "stderr"):
    try:
        skn.parse_args(["--share-storage-from=a", "--copy-storage-from=b"])
    except SystemExit:
        raised = True
check("--share-storage-from and --copy-storage-from together are rejected", raised)

tmp = Path(tempfile.mkdtemp())
(tmp / "lab.cfg").write_text(template)
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all") as dia, \
     mock.patch.object(skn, "build_values") as bv:
    skn.main(["-y", "--network-mode", "nat", "--domain", "lab.example"])
updated = (tmp / "lab.cfg").read_text()
check("main() keeps an existing lab.cfg and only updates the keys given as options",
      bv.call_count == 0 and '_network_mode=nat' in updated and "_mydomain=lab.example" in updated
      and "_myip=192.168.8.71" in updated)
check("main() runs the setup with the updated lab.cfg", dia.call_args[0][0]["_network_mode"] == "nat")
check("main() leaves lab.cfg readable by root only", (tmp / "lab.cfg").stat().st_mode & 0o777 == 0o600)


# ── automation node: --automation-node, fallback to the container, environment ──
check("--automation-node takes vm or container", skn.parse_args(["--automation-node", "container"]).automation_node
      == "container" and skn.parse_args([]).automation_node is None)
check("--automation-node is written to lab.cfg",
      skn._flag_values(skn.parse_args(["--automation-node", "container"]))["_automation_node"] == "container")
check("the template defaults _automation_node to vm", '_automation_node="vm"' in template)
k8s_args = skn.parse_args(["--automation-node", "kubernetes", "--k8s-kubeconfig", "/k.conf", "--k8s-network", "macvlan",
                           "--k8s-macvlan-master", "eth1", "--k8s-namespace", "labs", "--k8s-storage-class", "longhorn",
                           "--k8s-volume-size", "50Gi", "--automation-image", "reg/img:1"])
check("the --k8s-* options and --automation-image are written to lab.cfg",
      {k: v for k, v in skn._flag_values(k8s_args).items() if k.startswith(("_k8s", "_automation"))}
      == {"_automation_node": "kubernetes", "_k8s_kubeconfig": "/k.conf", "_k8s_network": "macvlan",
          "_k8s_macvlan_master": "eth1", "_k8s_namespace": "labs", "_k8s_storage_class": "longhorn",
          "_k8s_volume_size": "50Gi", "_automation_image": "reg/img:1"})
check("the template has every _k8s_* key",
      all("\n{}=".format(k) in template for k in ("_k8s_kubeconfig", "_k8s_namespace", "_k8s_network",
                                                 "_k8s_macvlan_master", "_k8s_storage_class", "_k8s_volume_size",
                                                 "_automation_image")))
modes, written, died = None, None, None
check("render_lab_cfg appends a key the file lacks",
      skn.render_lab_cfg("_myip=1.2.3.4\n", {"_automation_node": "container"}) == "_myip=1.2.3.4\n_automation_node=container\n")

with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
    skn.run_lab_automation(Path("/x/setup_lab_automation.sh"), "container")
env = run.call_args[1]["env"]
check("run_lab_automation runs the script in lab.cfg's directory",
      run.call_args[0][0] == ["bash", "/x/setup_lab_automation.sh"] and run.call_args[1]["cwd"] == "/x")
check("run_lab_automation passes LAB_AUTOMATION_NODE, LAB_PYTHON and LAB_KUBECTL_INSTALL",
      env["LAB_AUTOMATION_NODE"] == "container" and env["LAB_PYTHON"] == sys.executable
      and "dl.k8s.io" in env["LAB_KUBECTL_INSTALL"])


def _interrupted_download(url, dest):
    Path(dest).write_text("partial")
    raise OSError("connection reset")


tmpd = Path(tempfile.mkdtemp())
with mock.patch.object(skn, "Path", side_effect=lambda p: tmpd if str(p).startswith("/var/lib") else Path(p)), \
     mock.patch.object(urllib.request, "urlretrieve", side_effect=_interrupted_download):
    try:
        skn.download_automation_image("openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2")
        raised = False
    except OSError:
        raised = True
check("a failed image download raises and leaves no file behind", raised and not list(tmpd.iterdir()))
with mock.patch.object(skn, "Path", side_effect=lambda p: tmpd if str(p).startswith("/var/lib") else Path(p)), \
     mock.patch.object(urllib.request, "urlretrieve", side_effect=lambda url, dest: Path(dest).write_text("image")):
    skn.download_automation_image("openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2")
check("a complete image download is stored under its own name",
      [f.name for f in tmpd.iterdir()] == ["openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2"])


def _run_with_results(cfg, results, download_error=None):
    modes, written = [], []

    def fake_run(script, mode):
        modes.append(mode)
        return results[len(modes) - 1]
    with mock.patch.object(skn, "run_lab_automation", side_effect=fake_run), \
         mock.patch.object(Path, "read_text", return_value=""), \
         mock.patch.object(skn, "render_lab_cfg", side_effect=lambda text, values: written.append(values) or ""):
        try:
            _run_do_it_all(cfg, _FakeProfile(), download_error=download_error)
            died = False
        except SystemExit:
            died = True
    return modes, written, died


modes, written, died = _run_with_results({}, [1, 0])
check("a failed VM build is retried as a container", modes == ["vm", "container"] and not died)
check("after the fallback lab.cfg records _automation_node=container",
      written == [{"_automation_node": "container"}])
modes, written, died = _run_with_results({}, [0], download_error=OSError("record layer failure"))
check("a failed image download goes straight to the container and records it",
      modes == ["container"] and not died and written == [{"_automation_node": "container"}])
modes, written, died = _run_with_results({}, [130])
check("Ctrl-C during the VM build is not retried", modes == ["vm"] and died)
modes, written, died = _run_with_results({"_automation_node": "container"}, [1])
check("a failed container build is not retried", modes == ["container"] and died)
modes, written, died = _run_with_results({"_automation_node": "kubernetes"}, [1])
check("a failed kubernetes deployment is not retried", modes == ["kubernetes"] and died)
modes, written, died = _run_with_results({"_automation_node": "container"}, [1])
check("a failed container build is not retried", modes == ["container"] and died)
downloads = []
with mock.patch.object(skn, "run_lab_automation", return_value=0):
    _run_do_it_all({"_automation_node": "container"}, _FakeProfile(), downloads=downloads)
    _run_do_it_all({"_QCOW_IMAGE": "leap.qcow2"}, _FakeProfile(), downloads=downloads)
check("only VM mode downloads the VM image", downloads == ["leap.qcow2"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_kvm_node checks passed")
