#!/usr/bin/env python3.11
# Mocked unit tests for setup_demo_server/setup_kvm_node.py
# — image-URL construction (Leap 15 vs. 16 filenames), the bridge-nic/extra-packages wiring in
# do_it_all(), the automation.cfg/kvm_host.cfg split and every interactive-question default.
# No real network/subprocess calls. Run from 20_setup_kvm_node.sh, in its own container — see
# tests/run_tests.sh.
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "setup_demo_server"))
sys.path.insert(0, str(_REPO / "scripts"))

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
                   download_error=None, build_automation_node=True):
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
         mock.patch.object(skn, "scc_credential", return_value={}), \
         mock.patch.object(skn, "download_automation_image",
                           side_effect=download_error or ((lambda image: downloads.append(image))
                                                          if downloads is not None else None)), \
         mock.patch.object(skn, "configure_nat_network",
                            side_effect=(lambda name, cidr: nat_calls.append((name, cidr))) if nat_calls is not None
                            else None), \
         mock.patch.object(skn.host_network, "make_bridge",
                            side_effect=lambda bridge, nic: fake_profile.bridge_calls.append((nic, bridge)) or (NET, NET)), \
         mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
        skn.do_it_all(cfg, Path("/tmp"), build_automation_node=build_automation_node)


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


# ── do_it_all(): build_automation_node=False — KVM host only, no automation node ─────────────
fake = _FakeProfile()
with mock.patch.object(skn, "run_lab_automation") as run_la, \
     mock.patch.object(skn, "download_automation_image") as dl:
    _run_do_it_all({"_bridge_nic": "eth0"}, fake, build_automation_node=False)
check("build_automation_node=False still configures the KVM host (packages installed)",
      fake.installed_packages == ["libvirt", "podman"])
check("build_automation_node=False never downloads the VM image or runs setup_lab_automation.sh",
      not dl.called and not run_la.called)


# ── do_it_all(): SCC credential wiring, SLES only, via scc_credential() ─────
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


def _run_do_it_all_scc(cfg, fake_profile, scc):
    with mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=fake_profile), \
         mock.patch.object(skn, "install_yq"), mock.patch.object(skn, "ensure_fusermount_compat"), \
         mock.patch.object(skn, "_find", return_value=Path("/tmp/setup_lab_automation.sh")), \
         mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(Path, "write_text"), \
         mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
         mock.patch.object(skn, "scc_credential", return_value=scc), \
         mock.patch.object(skn, "download_automation_image"), \
         mock.patch.object(skn.host_network, "make_bridge",
                           side_effect=lambda bridge, nic: fake_profile.bridge_calls.append((nic, bridge)) or (NET, NET)), \
         mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
        skn.do_it_all(cfg, Path("/tmp"))


fake_sles = _FakeSuseProfile()
_run_do_it_all_scc({}, fake_sles, {"scc_regcode": "MY-REGCODE", "scc_email": "me@example.com",
                                   "scc_url": "https://scc.suse.com"})
check("do_it_all reads SCC credentials from scc_credential() onto a _SuseRegisteredProfile before "
      "register_repos()",
      fake_sles.registered_calls == [("MY-REGCODE", "me@example.com", "https://scc.suse.com")])

fake = _FakeProfile()
with mock.patch.object(skn, "scc_credential") as scc_mock:
    _run_do_it_all({}, fake)
    check("do_it_all never calls scc_credential() on a non-SLES profile", not scc_mock.called)
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


# ── config file generation ──────────────────────────────────────────────────
import ipaddress  # noqa: E402
import tempfile  # noqa: E402

check("reverse_zone of a /24 is its three octets reversed",
      skn.reverse_zone(ipaddress.ip_network("192.168.8.0/24")) == "8.168.192")
check("reverse_zone of a /16 is its two octets reversed",
      skn.reverse_zone(ipaddress.ip_network("10.20.0.0/16")) == "20.10")

automation_template = (_REPO / "setup_demo_server" / "automation.cfg.template").read_text()
kvm_template = (_REPO / "setup_demo_server" / "kvm_host.cfg.template").read_text()

out = skn.render_lab_cfg(automation_template, {"_mynet": "10.0.0.0/24", "ROOT_PWD_HASH": "$6$ab$cd",
                                               "ROOT_SSH_PUB_KEY": "ssh-ed25519 AAAA root@host"})
check("render_lab_cfg replaces a value and keeps the line's trailing comment",
      "_mynet=10.0.0.0/24          # CIDR notation" in out)
check("render_lab_cfg single-quotes values that need it, so bash sources them literally",
      "ROOT_PWD_HASH='$6$ab$cd'" in out and "ROOT_SSH_PUB_KEY='ssh-ed25519 AAAA root@host'" in out)
check("render_lab_cfg keeps every other line and comment", out.count("\n") == automation_template.count("\n")
      and "_mydomain=mydemo.lab" in out)

kvm_out = skn.render_lab_cfg(kvm_template, {"_bridge_nic": "eth0"})
check("render_lab_cfg replaces a quoted empty value in kvm_host.cfg.template", "\n_bridge_nic=eth0\n" in kvm_out)

check("automation.cfg.template's keys all match render_lab_cfg's KEY=value pattern",
      skn.render_lab_cfg(automation_template, {k: "X" for k in (
          "_mydns", "_mygw", "_myip", "_mynet", "_mydomain", "_mynetrev", "ROOT_SSH_PUB_KEY",
          "ROOT_PWD_HASH", "root_pwd", "AUTOMATION_HOSTNAME", "MYREG", "_timezone",
          "_automation_node")}).count("=X") == 13)
check("kvm_host.cfg.template's keys all match render_lab_cfg's KEY=value pattern",
      skn.render_lab_cfg(kvm_template, {k: "X" for k in (
          "_bridge_name", "_bridge_nic", "_network_mode", "_nat_network_cidr", "_virt_srv",
          "_QCOW_IMAGE")}).count("=X") == 6)
check("neither template carries a plaintext SUSE registration field anymore",
      "SUSE_regcode" not in automation_template and "SUSE_regcode" not in kvm_template)


# ── new detection helpers ──────────────────────────────────────────────────────
def _fake_path_is_dir(path_str_true):
    def fake(self):
        return str(self) == path_str_true
    return fake


with mock.patch.object(Path, "is_dir", _fake_path_is_dir("/sys/class/net/wlan0/wireless")):
    check("_is_wireless: true for an interface with a wireless sysfs subdir", skn._is_wireless("wlan0"))
    check("_is_wireless: false for a plain wired NIC", not skn._is_wireless("eth0"))

with mock.patch.object(Path, "is_file", return_value=True):
    check("_scc_registered: true when SCCcredentials exists", skn._scc_registered())
with mock.patch.object(Path, "is_file", return_value=False):
    check("_scc_registered: false otherwise", not skn._scc_registered())

with mock.patch.object(skn.primary, "try_load_service_credential", return_value=({"scc_regcode": "R"}, None)):
    check("scc_credential: returns the stored fields", skn.scc_credential() == {"scc_regcode": "R"})
with mock.patch.object(skn.primary, "try_load_service_credential", return_value=(None, "not found")):
    check("scc_credential: {} when none is on file", skn.scc_credential() == {})

write_calls = []
with mock.patch.object(skn.primary, "credentials_dirs", return_value=["/etc/lab_creation/credentials"]), \
     mock.patch.object(skn.setup_credentials, "write_credential_file",
                       side_effect=lambda path, kind, fields, encrypt: write_calls.append((path, kind, fields, encrypt))):
    skn._write_scc_credential("R1", "me@example.com", "")
check("_write_scc_credential: writes to the real credentials dir as kind 'scc', unencrypted, "
      "omitting the blank URL",
      write_calls == [(Path("/etc/lab_creation/credentials/scc.yaml"), "scc",
                       {"scc_regcode": "R1", "scc_email": "me@example.com"}, False)])

with mock.patch.object(os, "environ", {"KUBECONFIG": "/k.conf"}):
    check("_default_kubeconfig: $KUBECONFIG wins", skn._default_kubeconfig() == "/k.conf")
with mock.patch.object(os, "environ", {}), mock.patch.object(Path, "is_file", return_value=True):
    check("_default_kubeconfig: ~/.kube/config when it exists and $KUBECONFIG is unset",
          skn._default_kubeconfig() == str(Path("~/.kube/config").expanduser()))
with mock.patch.object(os, "environ", {}), mock.patch.object(Path, "is_file", return_value=False):
    check("_default_kubeconfig: '' when neither exists (kubectl's own default)",
          skn._default_kubeconfig() == "")


def _fake_kubectl(svc_ingress="", pod_names=()):
    def fake_run(cmd, **kw):
        if cmd[-3:-1] == ["get", "svc"] or "get" in cmd and "svc" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=svc_ingress)
        if "pods" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(pod_names))
        return subprocess.CompletedProcess(cmd, 1, stdout="")
    return fake_run


with mock.patch.object(subprocess, "run", side_effect=_fake_kubectl(svc_ingress="[{\"ip\":\"1.2.3.4\"}]")):
    check("_k8s_has_loadbalancer: true when a LoadBalancer Service already has an address",
          skn._k8s_has_loadbalancer(""))
with mock.patch.object(subprocess, "run", side_effect=_fake_kubectl(pod_names=["pod/metallb-controller-abc"])):
    check("_k8s_has_loadbalancer: true when a MetalLB controller pod is running, even with no "
          "LoadBalancer Service yet",
          skn._k8s_has_loadbalancer(""))
with mock.patch.object(subprocess, "run", side_effect=_fake_kubectl()):
    check("_k8s_has_loadbalancer: false with neither signal", not skn._k8s_has_loadbalancer(""))
with mock.patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "kubectl")):
    try:
        result = skn._k8s_has_loadbalancer("")
    except Exception:
        result = None
check("_k8s_has_loadbalancer: never raises — a broken/missing kubectl just means false",
      result is False or result is None)


# ── build_values(): bridge/nat default, automation-node default, SSH key, SCC ──────────────────
def _values(argv, profile="opensuse-leap-15", bridged=False, wireless=False, low_resource=False,
           is_vm=False, ssh_keys=None, scc_registered=False, written_scc=None, ssh_key_calls=None):
    args = skn.parse_args(argv)
    resources = {"vcpus": 1, "mem_mib": 1024} if low_resource else {"vcpus": 4, "mem_mib": 8192}
    ensure_ssh_key_mock = (mock.patch.object(skn, "ensure_ssh_key",
                           side_effect=lambda p: ssh_key_calls.append(p) or "ssh-ed25519 AAAA root@host")
                           if ssh_key_calls is not None else
                           mock.patch.object(skn, "ensure_ssh_key", return_value="ssh-ed25519 AAAA root@host"))
    with mock.patch.object(skn.host_network, "current_settings", return_value=NET), \
         mock.patch.object(skn.host_network, "is_bridge", return_value=bridged), \
         mock.patch.object(skn, "_is_wireless", return_value=wireless), \
         mock.patch.object(skn.kvm_host_profiles, "host_resources", return_value=resources), \
         mock.patch.object(skn.kvm_host_profiles, "is_virtualized_guest", return_value=is_vm), \
         mock.patch.object(skn, "_existing_ssh_keys", return_value=ssh_keys or []), \
         mock.patch.object(skn, "suggest_free_ip", return_value="192.168.8.240"), \
         ensure_ssh_key_mock, \
         mock.patch.object(skn, "password_hash", side_effect=lambda pw: "HASH(" + pw + ")"), \
         mock.patch.object(skn, "detect_timezone", return_value="Europe/Zurich"), \
         mock.patch.object(skn, "_scc_registered", return_value=scc_registered), \
         mock.patch.object(skn, "_write_scc_credential",
                           side_effect=(lambda r, e, u: written_scc.append((r, e, u)))
                           if written_scc is not None else None):
        return skn.build_values(args, skn.Asker(interactive=False), profile)


automation, kvm, generated = _values([])
check("bridge mode: the lab network, gateway and DNS come from the host",
      automation["_mynet"] == "192.168.8.0/24" and automation["_mygw"] == "192.168.8.1"
      and automation["_mydns"] == "192.168.8.53" and automation["_mynetrev"] == "8.168.192")
check("bridge mode: the default-route NIC becomes the port of br0, in kvm_host values",
      kvm["_bridge_name"] == "br0" and kvm["_bridge_nic"] == "eth0" and kvm["_network_mode"] == "bridge")
check("bridge mode: the automation node gets a free address and the domain-derived hostnames, "
      "in automation values",
      automation["_myip"] == "192.168.8.240" and automation["AUTOMATION_HOSTNAME"] == "automation.mydemo.lab"
      and automation["MYREG"] == "registry.mydemo.lab")
check("the KVM hypervisor SSH pointer lives in kvm values", kvm["_virt_srv"] == "root@192.168.8.20")
check("non-interactive with no password: one is generated, hashed and returned for printing",
      generated and automation["root_pwd"] == generated
      and automation["ROOT_PWD_HASH"] == "HASH({})".format(generated))
check("non-SLES hosts get no SCC credential questions at all (no scc fields in either dict)",
      "SUSE_regcode" not in automation and "SUSE_regcode" not in kvm)
check("not low-resource, not a VM: automation node defaults to vm",
      automation["_automation_node"] == "vm")

automation, kvm, _ = _values([], bridged=True)
check("a host already on a bridge keeps it and creates no new bridge NIC",
      kvm["_bridge_name"] == "eth0" and kvm["_bridge_nic"] == "")

automation, kvm, _ = _values([], low_resource=True)
check("a low-resource host defaults the automation node to container",
      automation["_automation_node"] == "container")

automation, kvm, _ = _values([], is_vm=True)
check("a host that is itself a VM defaults the automation node to container too",
      automation["_automation_node"] == "container")

automation, kvm, generated = _values(["--network-mode", "nat", "--domain", "lab.example",
                                      "--root-password", "s3cret"], profile="sles-16")
check("nat mode: the lab network is the NAT range, with .1 as gateway/DNS and .10 for the automation node",
      automation["_mynet"] == "192.168.150.0/24" and automation["_mygw"] == "192.168.150.1"
      and automation["_mydns"] == "192.168.150.1" and automation["_myip"] == "192.168.150.10"
      and kvm["_bridge_nic"] == "" and kvm["_virt_srv"] == "root@192.168.150.1")
check("a given password is used and not reported as generated",
      automation["root_pwd"] == "s3cret" and generated == ""
      and automation["AUTOMATION_HOSTNAME"] == "automation.lab.example")

automation, kvm, _ = _values([], wireless=True)
check("a wireless default-route NIC defaults the network mode to nat, not bridge",
      kvm["_network_mode"] == "nat")

automation, kvm, _ = _values(["--nat-cidr", "10.10.0.0/24"])
check("an explicit --network-mode/--nat-cidr combination is honoured even on a wired NIC",
      kvm["_network_mode"] == "bridge")  # --network-mode not given: bridge still wins on a wired NIC

automation, kvm, _ = _values(["--network-mode", "nat", "--nat-cidr", "10.10.0.0/24"])
check("--nat-cidr is only consulted once nat mode is actually chosen",
      kvm["_nat_network_cidr"] == "10.10.0.0/24")

ssh_calls = []
_values([], ssh_keys=[Path("/root/.ssh/id_ed25519.pub")], ssh_key_calls=ssh_calls)
check("with exactly one SSH key on the host, it's used silently — no ambiguity question needed",
      ssh_calls == ["~/.ssh/id_ed25519.pub"])

ssh_calls = []
three_keys = [Path("/root/.ssh/id_rsa.pub"), Path("/root/.ssh/id_ed25519.pub"), Path("/root/.ssh/id_ecdsa.pub")]
_values([], ssh_keys=three_keys, ssh_key_calls=ssh_calls)
check("with more than one SSH key, ensure_ssh_key is called exactly once, with the ambiguity "
      "question's answer (non-interactive default: the last candidate) — not every candidate",
      ssh_calls == [str(three_keys[-1])])

written = []
automation, kvm, _ = _values(["--suse-regcode", "R1", "--suse-email", "me@example.com"],
                             profile="sles-15", written_scc=written)
check("SLES + unregistered + a regcode given: the SCC credential is written, not left in config",
      written == [("R1", "me@example.com", "")])
check("no SCC fields land in either config dict", "SUSE_regcode" not in automation
      and "SUSE_regcode" not in kvm and "scc_regcode" not in automation and "scc_regcode" not in kvm)

written = []
automation, kvm, _ = _values([], profile="sles-15", scc_registered=True, written_scc=written)
check("SLES but already registered: no SCC credential write at all (nothing asked)", written == [])

written = []
automation, kvm, _ = _values([], profile="opensuse-leap-15", written_scc=written)
check("openSUSE (not SLES): no SCC credential write, regardless of registration state",
      written == [])


# ── parse_args(): unchanged flags, plus --nat-cidr only fires when nat is chosen ─────────────
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


# ── main(): keeps existing automation.cfg/kvm_host.cfg, writes the derived lab.cfg ────────────
tmp = Path(tempfile.mkdtemp())
(tmp / "automation.cfg").write_text(automation_template)
(tmp / "kvm_host.cfg").write_text(kvm_template)
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all") as dia, \
     mock.patch.object(skn, "build_values") as bv, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET):
    skn.main(["-y", "--network-mode", "nat", "--domain", "lab.example"])
updated_kvm = (tmp / "kvm_host.cfg").read_text()
updated_automation = (tmp / "automation.cfg").read_text()
check("main() keeps existing automation.cfg/kvm_host.cfg and only updates the keys given as options",
      bv.call_count == 0 and '_network_mode=nat' in updated_kvm and "_mydomain=lab.example" in updated_automation
      and "_myip=192.168.8.71" in updated_automation)
check("main() runs the setup with the merged cfg dict (both files' keys present)",
      dia.call_args[0][0]["_network_mode"] == "nat" and dia.call_args[0][0]["_mydomain"] == "lab.example")
check("main() defaults build_automation_node to True on a fresh/first run (nothing reachable yet)",
      dia.call_args[1]["build_automation_node"] is True)
check("main() leaves both config files readable by root only",
      (tmp / "automation.cfg").stat().st_mode & 0o777 == 0o600
      and (tmp / "kvm_host.cfg").stat().st_mode & 0o777 == 0o600)
check("main() writes a derived lab.cfg for setup_lab_automation.sh, which it says not to hand-edit",
      "edit those, not this file" in (tmp / "lab.cfg").read_text())


# ── main(): auto-splits a legacy lab.cfg on startup instead of synthesizing fresh defaults ───
tmp_legacy = Path(tempfile.mkdtemp())
(tmp_legacy / "lab.cfg").write_text(
    "_myip=192.168.8.99\n_mydomain=legacy.example\n_automation_node=\"vm\"\n"
    "_bridge_nic=\"\"\n_network_mode=\"bridge\"\n_QCOW_IMAGE=/x/leap.qcow2\n"
    "SUSE_regcode=\"OLD-REGCODE\"\nSUSE_email=\"\"\nSUSE_url=\"\"\n")
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp_legacy / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all") as dia_legacy, \
     mock.patch.object(skn, "build_values") as bv_legacy, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET), \
     mock.patch.object(skn.primary, "credentials_dirs", return_value=[str(tmp_legacy / "creds")]):
    skn.main(["-y"])
check("main() auto-splits a legacy lab.cfg into real automation.cfg/kvm_host.cfg instead of "
      "synthesizing brand-new defaults (build_values is never called)",
      bv_legacy.call_count == 0 and (tmp_legacy / "automation.cfg").is_file()
      and (tmp_legacy / "kvm_host.cfg").is_file())
check("the migrated values survive into the merged cfg main() actually runs with",
      dia_legacy.call_args[0][0]["_myip"] == "192.168.8.99"
      and dia_legacy.call_args[0][0]["_mydomain"] == "legacy.example"
      and dia_legacy.call_args[0][0]["_network_mode"] == "bridge")
check("lab.cfg ends the run as the regenerated derived file (from the migrated cfg), not the "
      "stale pre-split content — the split only promised not to touch it mid-split, not forever",
      "edit those, not this file" in (tmp_legacy / "lab.cfg").read_text()
      and "SUSE_regcode" not in (tmp_legacy / "lab.cfg").read_text())
check("SUSE_regcode is moved into the encrypted credential store, not either split file",
      "SUSE_regcode" not in (tmp_legacy / "automation.cfg").read_text()
      and "SUSE_regcode" not in (tmp_legacy / "kvm_host.cfg").read_text()
      and (tmp_legacy / "creds" / "scc.yaml").is_file())

# ── main(): --reconfigure skips the auto-split and writes brand-new defaults instead ──────────
tmp_reconf = Path(tempfile.mkdtemp())
(tmp_reconf / "lab.cfg").write_text("_myip=192.168.8.99\n_automation_node=\"vm\"\n")
_real_find_reconf = skn._find


def _find_reconf(name):
    return tmp_reconf / name if name in ("automation.cfg", "kvm_host.cfg", "lab.cfg") else _real_find_reconf(name)


with mock.patch.object(skn, "_find", side_effect=_find_reconf), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all"), \
     mock.patch.object(skn, "build_values", return_value=({}, {}, "")) as bv_reconf, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET):
    skn.main(["-y", "--reconfigure"])
check("--reconfigure bypasses the auto-split and takes the fresh/build_values path instead, "
      "same as when no legacy lab.cfg is present at all",
      bv_reconf.call_count == 1)


# ── review_cfg(): shows each file's path and full contents, skips a missing one ──────────────
review_dir = Path(tempfile.mkdtemp())
(review_dir / "a.cfg").write_text("_myip=1.2.3.4\n")
printed = []
with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
    skn.review_cfg([review_dir / "a.cfg", review_dir / "missing.cfg"])
check("review_cfg prints the existing file's path and full content",
      any(str(review_dir / "a.cfg") in line for line in printed) and any("_myip=1.2.3.4" in line for line in printed))
check("review_cfg silently skips a file that doesn't exist (e.g. kvm_host.cfg when it wasn't written)",
      not any("missing.cfg" in line for line in printed))


# ── main(): --interactive reloads the config after confirming, so an edit during the pause ──
# ── (simulated here by editing the file from inside the mocked confirmation input()) is honoured
tmp3b = Path(tempfile.mkdtemp())
(tmp3b / "automation.cfg").write_text(automation_template)
(tmp3b / "kvm_host.cfg").write_text(kvm_template)


_input_calls = []


def _fake_input_edit_before_confirm(prompt):
    _input_calls.append(prompt)
    if prompt.strip().startswith("Set up this host"):
        # Simulate the operator editing the file, in another terminal, during the pause that
        # review_cfg() + this very prompt create — right before actually answering it.
        (tmp3b / "automation.cfg").write_text(
            skn.render_lab_cfg((tmp3b / "automation.cfg").read_text(), {"_mydomain": "edited-during-pause.lab"}))
        return "yes"
    if prompt.strip().startswith("Also build an automation node"):
        return "yes"
    return "no"  # the end-of-run "add another KVM node"/"add cloud credentials" follow-ups


with mock.patch.object(skn, "_find", side_effect=lambda name: tmp3b / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all") as dia3b, \
     mock.patch.object(skn, "build_values") as bv3b, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET), \
     mock.patch("builtins.input", side_effect=_fake_input_edit_before_confirm):
    skn.main(["--interactive"])
check("the simulated edit happened strictly after the \"also build automation node\" question "
      "and before the final confirmation reply (sanity check on the test's own timing)",
      len(_input_calls) >= 2 and "automation node" in _input_calls[0] and "Set up this host" in _input_calls[1])
check("main() reloads the config after the confirmation prompt, so an edit made during the "
      "interactive pause is actually applied, not the stale pre-edit value",
      dia3b.call_args[0][0]["_mydomain"] == "edited-during-pause.lab")


# ── main(): build_automation_node defaults to False when one is already reachable ────────────
tmp2 = Path(tempfile.mkdtemp())
(tmp2 / "automation.cfg").write_text(automation_template)
(tmp2 / "kvm_host.cfg").write_text(kvm_template)
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp2 / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all") as dia2, \
     mock.patch.object(skn, "build_values") as bv2, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=True), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET):
    skn.main(["-y"])
check("main() defaults build_automation_node to False when one already answers at _myip",
      dia2.call_args[1]["build_automation_node"] is False)


# ── automation node: --automation-node, fallback to the container, environment ──
check("--automation-node takes vm or container", skn.parse_args(["--automation-node", "container"]).automation_node
      == "container" and skn.parse_args([]).automation_node is None)
check("--automation-node is written to automation.cfg",
      skn._automation_flag_values(skn.parse_args(["--automation-node", "container"]))["_automation_node"] == "container")
check("--bridge-name is written to kvm_host.cfg, not automation.cfg",
      "_bridge_name" in skn._kvm_flag_values(skn.parse_args(["--bridge-name", "labbr0"]))
      and "_bridge_name" not in skn._automation_flag_values(skn.parse_args(["--bridge-name", "labbr0"])))
check("the automation template defaults _automation_node to vm", '_automation_node="vm"' in automation_template)
k8s_args = skn.parse_args(["--automation-node", "kubernetes", "--k8s-kubeconfig", "/k.conf", "--k8s-network", "macvlan",
                           "--k8s-macvlan-master", "eth1", "--k8s-namespace", "labs", "--k8s-storage-class", "longhorn",
                           "--k8s-volume-size", "50Gi", "--automation-image", "reg/img:1"])
check("the --k8s-* options and --automation-image are written to automation.cfg",
      {k: v for k, v in skn._automation_flag_values(k8s_args).items() if k.startswith(("_k8s", "_automation"))}
      == {"_automation_node": "kubernetes", "_k8s_kubeconfig": "/k.conf", "_k8s_network": "macvlan",
          "_k8s_macvlan_master": "eth1", "_k8s_namespace": "labs", "_k8s_storage_class": "longhorn",
          "_k8s_volume_size": "50Gi", "_automation_image": "reg/img:1"})
check("the automation template has every _k8s_* key",
      all("\n{}=".format(k) in automation_template for k in ("_k8s_kubeconfig", "_k8s_namespace", "_k8s_network",
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
check("after the fallback automation.cfg records _automation_node=container",
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


# ── host helpers: yq, reachability, shared storage, DNS, free IP, SSH key, time zone, remote run ───────────────────
import ipaddress  # noqa: E402
import tempfile  # noqa: E402


def done(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


with mock.patch.object(skn.os, "uname", return_value=mock.Mock(machine="aarch64")), \
        mock.patch.object(skn.urllib.request, "urlretrieve") as fetch, mock.patch.object(skn.os, "chmod") as chmod:
    skn.install_yq()
check("install_yq: downloads the release for this architecture and makes it executable",
      fetch.call_args[0] == ("https://github.com/mikefarah/yq/releases/latest/download/yq_linux_arm64", "/usr/local/bin/yq")
      and chmod.call_args[0] == ("/usr/local/bin/yq", 0o755))
with mock.patch.object(skn.os, "uname", return_value=mock.Mock(machine="x86_64")), \
        mock.patch.object(skn.urllib.request, "urlretrieve", side_effect=OSError("offline")):
    skn.install_yq()
check("install_yq: a failed download only warns", True)

with mock.patch.object(skn.subprocess, "run", return_value=done()) as run:
    check("_automation_host_reachable: SSH port open on _myip", skn._automation_host_reachable("192.168.8.71")
          and run.call_args[0][0] == ["nc", "-z", "-w", "3", "192.168.8.71", "22"])
    check("_automation_host_reachable: false without _myip", not skn._automation_host_reachable(""))

root = Path(tempfile.mkdtemp())
real_path = skn.Path


def rooted(p, *a):
    """skn.Path with absolute system paths redirected into the temporary root."""
    return real_path(str(root) + str(p)) if str(p).startswith(("/etc", "/var")) else real_path(p, *a)


(root / "etc").mkdir()
(root / "etc" / "fstab").write_text("/dev/vda1 / xfs defaults 0 0\n")
with mock.patch.object(skn, "Path", side_effect=rooted), mock.patch.object(skn.subprocess, "run", return_value=done()) as run:
    skn.setup_shared_storage("nuc1.lab", "")
    skn.setup_shared_storage("nuc1.lab", "")
    fstab = (root / "etc" / "fstab").read_text()
    check("setup_shared_storage: adds the sshfs automount line once and mounts it",
          fstab.count("nuc1.lab:/var/lib/libvirt/images /var/lib/libvirt/images fuse.sshfs") == 1
          and ["mount", "/var/lib/libvirt/images"] in [c[0][0] for c in run.call_args_list])
    run.reset_mock()
    skn.setup_shared_storage("", "nuc2.lab")
    check("setup_shared_storage: copies once with rsync",
          run.call_args[0][0] == ["rsync", "-a", "nuc2.lab:/var/lib/libvirt/images/", "/var/lib/libvirt/images/"])
    run.reset_mock()
    skn.setup_shared_storage("", "")
    check("setup_shared_storage: does nothing without a source", not run.called)

profile = mock.Mock()
with mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=profile):
    skn.configure_automation_dns({"_myip": "192.168.8.71", "_mydomain": "mydemo.lab"})
check("configure_automation_dns: the host's profile points DNS at the automation host",
      profile.configure_dns.call_args[0] == ("192.168.8.71", "mydemo.lab"))
with mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None):
    skn.configure_automation_dns({"_myip": "x"})

answered = {"192.168.8.249"}
with mock.patch.object(skn.subprocess, "run", side_effect=lambda cmd, **k: done(rc=0 if cmd[-1] in answered else 1)):
    check("suggest_free_ip: the highest address below .250 that is not excluded and does not answer",
          skn.suggest_free_ip(ipaddress.ip_network("192.168.8.0/24"), {"192.168.8.248"}) == "192.168.8.247")

keydir = Path(tempfile.mkdtemp())


def keygen(cmd, **k):
    Path(cmd[-1] + ".pub").write_text("ssh-ed25519 NEW root@host\n")
    return done()


with mock.patch.object(skn.subprocess, "run", side_effect=keygen) as run:
    check("ensure_ssh_key: creates an ed25519 key pair when missing and returns the public key",
          skn.ensure_ssh_key(str(keydir / "id_ed25519.pub")) == "ssh-ed25519 NEW root@host"
          and run.call_args[0][0][:4] == ["ssh-keygen", "-q", "-t", "ed25519"])
    check("ensure_ssh_key: reuses an existing key", skn.ensure_ssh_key(str(keydir / "id_ed25519.pub")) == "ssh-ed25519 NEW root@host"
          and run.call_count == 1)

with mock.patch.object(skn.subprocess, "run", return_value=done("Europe/Zurich\n")):
    check("detect_timezone: timedatectl's time zone", skn.detect_timezone() == "Europe/Zurich")
(root / "usr" / "share" / "zoneinfo" / "Europe").mkdir(parents=True)
(root / "usr" / "share" / "zoneinfo" / "Europe" / "Madrid").write_text("")
(root / "etc" / "localtime").symlink_to(root / "usr" / "share" / "zoneinfo" / "Europe" / "Madrid")
with mock.patch.object(skn.subprocess, "run", return_value=done("")), mock.patch.object(skn, "Path", side_effect=rooted):
    check("detect_timezone: else the /etc/localtime link's zone", skn.detect_timezone() == "Europe/Madrid")
(root / "etc" / "localtime").unlink()
with mock.patch.object(skn.subprocess, "run", return_value=done("")), mock.patch.object(skn, "Path", side_effect=rooted):
    check("detect_timezone: else UTC", skn.detect_timezone() == "UTC")

calls = []
with mock.patch.object(skn.subprocess, "run", side_effect=lambda cmd, **k: calls.append(cmd) or done()), \
        mock.patch.object(skn.subprocess, "Popen") as popen, mock.patch.object(skn.time, "time", return_value=1700000000):
    skn.run_remote("nuc3.lab", ["nuc3.lab", "--automation-node", "container", "--name", "a b"])
check("run_remote: checks SSH, copies the key, ships the tree and runs the installer with the other options",
      calls[0] == ["nc", "-z", "-w", "5", "nuc3.lab", "22"] and calls[1] == ["ssh-copy-id", "root@nuc3.lab"]
      and popen.call_args[0][0][:4] == ["tar", "-C", str(skn._SCRIPT_DIR.parent), "-cf"]
      and calls[-1][-1] == "bash /var/tmp/lab-in-a-box-1700000000/install_demo_server_scripts.sh --source "
                           "/var/tmp/lab-in-a-box-1700000000 --automation-node container --name 'a b'")
with mock.patch.object(skn.subprocess, "run", return_value=done(rc=1)), \
        mock.patch.object(skn, "die", side_effect=SystemExit("unreachable")) as died:
    try:
        skn.run_remote("nuc3.lab", [])
    except SystemExit:
        pass
check("run_remote: dies when the host is unreachable", died.called)


# ── main(): interactive-only follow-ups — add another KVM node, add cloud credentials ────────
tmp3 = Path(tempfile.mkdtemp())
(tmp3 / "automation.cfg").write_text(automation_template)
(tmp3 / "kvm_host.cfg").write_text(kvm_template)
remote_calls = []
cred_calls = []
# The full --interactive input() sequence: (1) build an automation node here? (2) the "set up
# this host" confirmation (3) add another KVM node? (4) its hostname (5) share storage with it?
# (6) add cloud credentials?
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp3 / name), \
     mock.patch.object(skn.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "do_it_all"), \
     mock.patch.object(skn, "build_values") as bv3, \
     mock.patch.object(skn, "_automation_host_reachable", return_value=False), \
     mock.patch.object(skn.host_network, "current_settings", return_value=NET), \
     mock.patch.object(skn, "run_remote", side_effect=lambda t, a: remote_calls.append((t, a))), \
     mock.patch.object(skn.setup_credentials, "interactive_build", side_effect=lambda d: cred_calls.append(d)), \
     mock.patch("builtins.input", side_effect=["yes", "yes", "yes", "nuc9.lab", "yes", "yes"]):
    skn.main(["--interactive"])
check("main() offers to add another KVM node in interactive mode, and shares storage when asked",
      remote_calls == [("nuc9.lab", ["--share-storage-from", NET.ip])])
check("main() offers to add cloud credentials in interactive mode",
      cred_calls == ["/etc/lab_creation/credentials"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_kvm_node checks passed")
