#!/usr/bin/env python3
# Part of lab-in-a-box, prepares the hypervisor to work as a lab_automation node
# Author/s: Raul Mahiques
# License: GPLv3
#
# The hypervisor setup entrypoint; install_demo_server_scripts.sh installs Python and runs it.
# Package installation uses the per-OS profiles in libs/kvm_host_profiles.py, bridging uses
# libs/host_network.py, and setup_lab_automation.sh builds the automation VM.

"""
setup_kvm_node.py — prepare a hypervisor host to run lab-in-a-box.

Usage:
    setup_kvm_node.py [options] [TARGET]       (see --help for every option)

Without setup_demo_server/automation.cfg, it (and kvm_host.cfg) are written first from the options
and this host's own network settings; with --interactive each value is asked for, with those as
defaults. Existing files are used as is; options that map to their keys update them in place.
automation.cfg is the automation node's own identity (network, hostname, vm/container/kubernetes
shape) — needed regardless of whether a KVM host is even involved. kvm_host.cfg is specific to
this host becoming a KVM hypervisor (bridge/NAT networking, storage, VM-build specifics) — read
only when this script actually prepares a KVM host, which is every run of this script.

This script always configures the host as a KVM node: packages, a bridge on the default-route NIC
that keeps the host's address (rolled back automatically if the gateway stops answering), libvirt,
and the storage pool. Building an automation node on it is the one optional add-on step — asked
about with a sensible default (skip it if one is already reachable at automation.cfg's _myip, e.g.
a 2nd/3rd KVM host joining an existing lab). If an automation host is already up and reachable,
this host's DNS is pointed at it either way.

Interactive mode can also, after this host is set up: add another KVM node over SSH (sharing this
host's storage), and/or add cloud-provider credentials (scripts/setup_credentials.py) — both
optional, both independently re-runnable later.

TARGET sets up that remote host over SSH instead: setup_demo_server/ and libs/ are copied there and
install_demo_server_scripts.sh runs with the same options.
"""

__version__ = "__LABVERSION__"

import argparse
import getpass
import ipaddress
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
for _candidate in ("/usr/local/lib/lab_creation", str(_SCRIPT_DIR.parent / "libs"),
                   str(_SCRIPT_DIR.parent / "scripts")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

def _find(filename):
    """setup_lab_automation.sh/lab.cfg[.template] live right next to this
    script (setup_demo_server/) — a single named lookup point so callers
    don't hardcode _SCRIPT_DIR / filename at every call site."""
    return _SCRIPT_DIR / filename

import primary  # noqa: E402
import kvm_host_profiles  # noqa: E402
import host_network  # noqa: E402
import setup_credentials  # noqa: E402

_BOLD = "\033[1m"
_RESET = "\033[0m"
_RED = "\033[1;31m"


def log(msg):
    print("\n{}###._ {} _.###{}\n".format(_BOLD, msg, _RESET))


def die(msg):
    print("{}ERROR{}: {}".format(_RED, _RESET, msg), file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print("{}WARNING{}: {}".format(_RED, _RESET, msg), file=sys.stderr)


def install_yq():
    """Download yq for the local architecture. Mirrors the inline python3 -c block in bash."""
    arch_map = {"x86_64": "amd64", "aarch64": "arm64"}
    machine = os.uname().machine
    arch = arch_map.get(machine, machine)
    try:
        urllib.request.urlretrieve(
            "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_{}".format(arch),
            "/usr/local/bin/yq")
        os.chmod("/usr/local/bin/yq", 0o755)
        print("yq installed to /usr/local/bin/yq")
    except OSError as e:
        print("{}WARNING{}: yq installation failed ({}), some features may not work".format(_RED, _RESET, e),
              file=sys.stderr)


def ensure_fusermount_compat():
    """
    guestmount/guestunmount (used by setup_lab_automation.sh's configure_image()
    to inject the automation VM's network/SSH/hostname config directly into its
    qcow2) hardcode the legacy FUSE2 binary name "fusermount" internally,
    regardless of which libfuse version actually performed the mount.
    On a Leap 15.6 host with only fuse3 installed (openSUSE's repos have no "fuse" v2 package),
    guestmount succeeds, since it uses libfuse3 directly. guestunmount then fails with "failed to
    unmount /mnt: exec: No such file or directory". This is silent, since setup_lab_automation.sh's own call sites
    redirect its stderr away or run it from an EXIT trap. The mount is never
    released, the qcow2 file stays open, and the VM boots from a completely
    unmodified image (no static IP/SSH key/hostname ever applied — no
    JeOS/SLE-Micro Firstboot-style provisioning ever ran).
    A plain "fusermount -> fusermount3" symlink is the standard, well-known
    workaround (fine to leave in place indefinitely — it never conflicts with
    a real "fuse" package's own fusermount, this just fills the gap when one
    isn't installed). No-op if a real fusermount already exists, or if
    fusermount3 itself isn't there to link to (nothing this function can do
    about that — package installation is the profile's own job).
    """
    if Path("/usr/bin/fusermount").exists():
        return
    fusermount3 = Path("/usr/bin/fusermount3")
    if not fusermount3.exists():
        return
    log("Add fusermount -> fusermount3 compat symlink (guestmount/guestunmount need the legacy name)")
    Path("/usr/bin/fusermount").symlink_to(fusermount3)


def _primary_storage_host(cfg):
    """
    Default host for --share-storage-from/--copy-storage-from when the flag
    is given without an explicit value. Reuses lab.cfg's existing _virt_srv
    (e.g. "root@hypervisor" — already the primary-hypervisor pointer used by
    the automation VM's own sshfs mount of source images) rather than adding
    a new config field: strips any "user@" prefix to get a bare hostname.
    """
    virt_srv = cfg.get("_virt_srv", "") or ""
    return virt_srv.split("@", 1)[-1] if virt_srv else ""


def _automation_host_reachable(myip, timeout=3):
    """
    True if the automation VM (lab.cfg's _myip) is already up and reachable
    over SSH. Gates the DNS-configuration step below: pointing a new KVM
    host's DNS at the automation host only makes sense once that host
    actually exists and is running. On the very first bootstrap of the
    first KVM node the automation VM has not been created yet, so this is
    always False there — the DNS step is skipped and do_it_all() behaves
    with no DNS step.
    """
    if not myip:
        return False
    return subprocess.run(
        ["nc", "-z", "-w", str(timeout), myip, "22"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def setup_shared_storage(share_from, copy_from):
    """
    Make /var/lib/libvirt/images (covers both ISO_LOC's sources/ subdir and
    VM_IMG_LOC) available on this host by sshfs-mounting it from another
    host, or by a one-time rsync copy. At most one of share_from/copy_from
    is set (mutually exclusive CLI flags) — a no-op if neither is given,
    which is the default and today's unchanged behavior (this host manages
    its own local storage).
    """
    path = "/var/lib/libvirt/images"
    Path(path).mkdir(parents=True, exist_ok=True)

    if share_from:
        log("Mount {} storage from {} via sshfs".format(path, share_from))
        fstab_line = (
            "{}:{} {} fuse.sshfs  noauto,x-systemd.automount,_netdev,reconnect,"
            "identityfile=/root/.ssh/id_rsa,allow_other,default_permissions 0 0\n"
        ).format(share_from, path, path)
        fstab = Path("/etc/fstab")
        text = fstab.read_text()
        if fstab_line not in text:
            fstab.write_text(text + fstab_line)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["mount", path], check=False)
    elif copy_from:
        log("Copy {} storage from {} via rsync (one-time)".format(path, copy_from))
        subprocess.run(["rsync", "-a", "{}:{}/".format(copy_from, path), "{}/".format(path)], check=False)


def configure_automation_dns(cfg):
    """
    Point this (additional) KVM host's DNS resolution at the already-running
    automation host, using the modular per-OS profile from kvm_host_profiles.
    Only called by do_it_all() when _automation_host_reachable() is True —
    see that function's docstring for why this never fires on the very
    first bootstrap.
    """
    profile = kvm_host_profiles.detect_profile()
    if profile is None:
        return
    log("Point DNS at automation host {} ({})".format(cfg.get("_myip", ""), cfg.get("_mydomain", "")))
    profile.configure_dns(cfg.get("_myip", ""), cfg.get("_mydomain", ""))


def download_automation_image(qcow_image):
    """
    Mirrors the wget step in do_it_all (bash). URL construction assumes
    Leap 15.x's appliance filename convention
    (openSUSE-Leap-<ver>-Minimal-VM.x86_64-...-kvm-and-xen-...qcow2) — Leap
    16.0's own appliance directory uses a genuinely different filename shape
    (Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2, no "openSUSE-" prefix, "Cloud"
    build variant instead of "kvm-and-xen"), confirmed via the openSUSE
    download mirror. Deliberately NOT auto-transformed here: guessing at a
    16.x filename from a 15.x one risks silently downloading nothing or the
    wrong image. kvm_host.cfg's _QCOW_IMAGE must be set to the correct, real
    filename for whichever Leap version is targeted — see kvm_host.cfg.template's
    comment next to _QCOW_IMAGE.
    """
    Path("/var/lib/libvirt/images/sources/").mkdir(parents=True, exist_ok=True)

    log("Download image to be used for the automation VM")
    qcow_basename = Path(qcow_image).name
    m = re.search(r"\d+\.\d+", qcow_basename)
    vm_ver = m.group(0) if m else ""
    dest = Path("/var/lib/libvirt/images/sources") / qcow_basename
    if dest.exists():
        return
    url = "https://download.opensuse.org/distribution/leap/{}/appliances/{}".format(vm_ver, qcow_basename)
    part = dest.with_name(dest.name + ".part")
    try:
        urllib.request.urlretrieve(url, str(part))
    except BaseException:
        if part.exists():
            part.unlink()
        raise
    part.rename(dest)


_POOL_XML = """\
<!--
WARNING: THIS IS AN AUTO-GENERATED FILE. CHANGES TO IT ARE LIKELY TO BE
OVERWRITTEN AND LOST. Changes to this xml configuration should be made using:
  virsh pool-edit pool
or other application using the libvirt API.
-->

<pool type='dir'>
  <name>pool</name>
  <uuid>8bd63226-f3e4-4a14-965f-a75673a1a291</uuid>
  <capacity unit='bytes'>0</capacity>
  <allocation unit='bytes'>0</allocation>
  <available unit='bytes'>0</available>
  <source>
  </source>
  <target>
    <path>/var/lib/libvirt/images/sources</path>
  </target>
</pool>
"""


_LIBVIRT_SOCKETS = ["virtqemud.socket", "virtnetworkd.socket", "virtstoraged.socket", "virtnodedevd.socket",
                    "virtsecretd.socket", "virtinterfaced.socket", "virtnwfilterd.socket", "virtproxyd.socket"]


def _unit_exists(unit: str) -> bool:
    return subprocess.run(["systemctl", "cat", unit], capture_output=True, check=False).returncode == 0


def enable_libvirt() -> None:
    """
    Start libvirt: libvirtd where it is installed, else the sockets of whichever per-driver daemons
    are installed (EL 10, Fedora, openSUSE Leap 15.6 / 16). virtqemud is required in that case.
    """
    if _unit_exists("libvirtd.service"):
        subprocess.run(["systemctl", "enable", "--now", "libvirtd"], check=True)
        return
    sockets = [s for s in _LIBVIRT_SOCKETS if _unit_exists(s)]
    if "virtqemud.socket" not in sockets:
        die("neither libvirtd.service nor virtqemud.socket is installed")
    subprocess.run(["systemctl", "enable", "--now"] + sockets, check=True)


def define_storage_pool() -> None:
    """Define, autostart and start the "pool" directory pool from _POOL_XML; an existing pool is kept."""
    if subprocess.run(["virsh", "pool-info", "pool"], capture_output=True, check=False).returncode != 0:
        with tempfile.NamedTemporaryFile("w", suffix=".xml") as f:
            f.write(_POOL_XML)
            f.flush()
            subprocess.run(["virsh", "pool-define", f.name], check=True)
    Path("/var/lib/libvirt/images/sources").mkdir(parents=True, exist_ok=True)
    subprocess.run(["virsh", "pool-autostart", "pool"], check=True)
    info = subprocess.run(["virsh", "pool-info", "pool"], capture_output=True, text=True, check=False).stdout
    if "running" not in info:
        subprocess.run(["virsh", "pool-start", "pool"], check=True)


def _nat_network_xml(name, cidr):
    """
    Build a minimal libvirt <network> definition: NAT'd, with libvirt itself
    owning DHCP/gateway for the given CIDR — exactly libvirt's own built-in
    "default" network mechanism, just under our own name/range instead of
    reusing "default" (so it coexists with whatever the host already has,
    including an existing "default" network, untouched).
    """
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts())
    gateway = hosts[0]
    dhcp_start = hosts[1]
    dhcp_end = hosts[-1]
    return (
        "<network>\n"
        "  <name>{name}</name>\n"
        "  <forward mode='nat'/>\n"
        "  <ip address='{gateway}' netmask='{netmask}'>\n"
        "    <dhcp>\n"
        "      <range start='{dhcp_start}' end='{dhcp_end}'/>\n"
        "    </dhcp>\n"
        "  </ip>\n"
        "</network>\n"
    ).format(name=name, gateway=gateway, netmask=net.netmask, dhcp_start=dhcp_start, dhcp_end=dhcp_end)


def configure_nat_network(name, cidr):
    """
    Define+start+autostart a new libvirt NAT'd virtual network, idempotently
    (skip if a network with this name already exists and is active) — the
    "_network_mode=nat" alternative to configure_bridge() above. Deliberately
    NOT a kvm_host_profiles.py method: unlike bridge setup (a genuine
    per-OS-network-stack concern — nmcli vs. wicked), defining a libvirt
    network is a single OS-agnostic `virsh net-define` operation, so keeping
    it flat here avoids conflating the two different kinds of "networking
    setup" this project has.
    """
    existing = subprocess.run(
        ["virsh", "net-info", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if existing.returncode == 0:
        active = subprocess.run(
            ["virsh", "net-list", "--name"], capture_output=True, text=True,
        )
        if name in (active.stdout or "").split():
            log("libvirt network '{}' already exists and is active — leaving it as-is".format(name))
            return
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(_nat_network_xml(name, cidr))
        xml_path = f.name
    try:
        for args in (["net-define", xml_path], ["net-start", name], ["net-autostart", name]):
            r = subprocess.run(["virsh"] + args, check=False)
            if r.returncode != 0:
                die("virsh {} failed for NAT network '{}'".format(args[0], name))
    finally:
        os.unlink(xml_path)


def prepare_kvm_host(cfg, script_dir, share_storage_from=None, copy_storage_from=None):
    """
    Makes this host a real KVM hypervisor: packages, bridge, shared storage, libvirt, the
    storage pool, firewalld disabled, and (nat mode) the libvirt NAT network. Independently
    callable (both setup_kvm_node.py's own run and the automation-centric
    setup_automation.py's optional add-on step use this one function) — no automation-node
    awareness at all, deliberately: whether anything gets built on top of this KVM host is the
    caller's decision, made by calling deploy_automation_node() afterwards or not.
    share_storage_from/copy_storage_from both default to None (today's unchanged single-host
    behavior: this host manages its own local storage, no DNS reconfigured).
    """
    profile = kvm_host_profiles.detect_profile()
    if profile is None:
        die("OS type not detected or unsupported. Supported: openSUSE Leap 15/16, SLES 15/16, "
            "Debian 12/13, Ubuntu 22.04/24.04, RHEL/Rocky/AlmaLinux/CentOS Stream 9/10, Fedora.")

    os_id = profile.os_info.get("ID", "")
    pretty_name = profile.os_info.get("PRETTY_NAME", os_id)
    print("- Installing in {} (profile {})".format(pretty_name, profile.name))
    if not profile.verified:
        warn("{} is not a verified OS version; using the {} package list".format(pretty_name, profile.name))

    if profile.unmapped_packages:
        warn("not installed automatically on {} (no package for this OS): {}".format(
            profile.name, ", ".join(profile.unmapped_packages)))

    extra_pkgs = (cfg.get("_extra_host_pkgs", "") or "").split()
    if extra_pkgs:
        log("Adding extra packages from kvm_host.cfg's _extra_host_pkgs")
        profile.packages = profile.packages + extra_pkgs

    if isinstance(profile, kvm_host_profiles._SuseRegisteredProfile):
        # SLES only — see _SuseRegisteredProfile.register_repos()'s own
        # docstring: SUSEConnect --product fails outright on a genuinely
        # unregistered host without this. The regcode is a real credential,
        # never written into a plaintext config file — see scc_credential().
        scc = scc_credential()
        profile.regcode = scc.get("scc_regcode", "")
        profile.suse_email = scc.get("scc_email", "")
        profile.suse_url = scc.get("scc_url", "")

    log("Configure package repositories")
    profile.register_repos()

    log("Update all packages and install necessary ones")
    profile.refresh()
    profile.update()
    failed_extras = profile.install()
    if failed_extras:
        warn("optional packages not installed: {}".format(", ".join(failed_extras)))

    ensure_fusermount_compat()

    log("Install yq")
    install_yq()

    bridge_nic = cfg.get("_bridge_nic", "") or ""
    if bridge_nic and (cfg.get("_network_mode", "") or "bridge") == "bridge":
        bridge_name = cfg.get("_bridge_name", "") or "br0"
        log("Configure network bridge {} ({})".format(bridge_name, bridge_nic))
        try:
            before, after = host_network.make_bridge(bridge_name, bridge_nic)
        except host_network.BridgeError as e:
            die(str(e))
        if before.ip != after.ip:
            warn("the DHCP server gave {} the new address {} (was {}): reserve an address for MAC {} "
                 "and update _virt_srv in kvm_host.cfg if it named the old one".format(
                     bridge_name, after.ip, before.ip, after.mac))

    if share_storage_from or copy_storage_from:
        setup_shared_storage(share_storage_from, copy_storage_from)

    if _automation_host_reachable(cfg.get("_myip", "")):
        configure_automation_dns(cfg)

    enable_libvirt()
    define_storage_pool()
    subprocess.run(["systemctl", "disable", "--now", "firewalld"], check=False)

    if (cfg.get("_network_mode", "") or "bridge") == "nat":
        nat_name = cfg.get("_nat_network_name", "") or "labnat"
        nat_cidr = cfg.get("_nat_network_cidr", "") or "192.168.150.0/24"
        log("Configure NAT'd libvirt network {} ({})".format(nat_name, nat_cidr))
        configure_nat_network(nat_name, nat_cidr)


def deploy_automation_node(cfg, script_dir):
    """
    Builds the automation node itself (vm/container/kubernetes, cfg's _automation_node) via
    setup_lab_automation.sh. For vm mode, assumes prepare_kvm_host() has already run on this
    same host (libvirt/storage pool must already exist — this function has no KVM-host
    awareness of its own). Independently callable — setup_kvm_node.py's own run and
    setup_automation.py's mandatory core step both use this one function.
    """
    lab_automation_script = _find("setup_lab_automation.sh")
    if not lab_automation_script.is_file():
        die("Missing script, please download setup_lab_automation.sh script from the GIT repository")

    node_mode = cfg.get("_automation_node", "") or "vm"
    fallback = False
    if node_mode == "vm":
        try:
            download_automation_image(cfg.get("_QCOW_IMAGE", ""))
        except OSError as e:
            warn("could not download the automation VM image ({}); creating the automation node as a container "
                 "instead".format(e))
            node_mode, fallback = "container", True

    log("Start setup_lab_automation.sh script to create the automation node ({})".format(node_mode))
    rc = run_lab_automation(lab_automation_script, node_mode)
    if rc != 0 and node_mode == "vm" and rc != 130:
        warn("the automation VM could not be built (exit {}); creating the automation node as a container "
             "instead".format(rc))
        node_mode, fallback = "container", True
        rc = run_lab_automation(lab_automation_script, node_mode)
    if rc == 0 and fallback:
        cfg_path = _find("automation.cfg")
        cfg_path.write_text(render_lab_cfg(cfg_path.read_text(), {"_automation_node": "container"}))
    if rc != 0:
        die("{} failed (exit {}); see its output above".format(lab_automation_script.name, rc))


def do_it_all(cfg, script_dir, share_storage_from=None, copy_storage_from=None, build_automation_node=True):
    """
    setup_kvm_node.py's own combined run: always prepare_kvm_host(), then
    deploy_automation_node() too unless build_automation_node=False — e.g. adding a 2nd/3rd KVM
    host to a lab whose automation node already lives elsewhere, where this host still becomes
    a real KVM node but doesn't also get an automation node built on it.
    """
    prepare_kvm_host(cfg, script_dir, share_storage_from, copy_storage_from)
    if not build_automation_node:
        log("KVM host ready — no automation node built on it (not requested for this run)")
        return
    deploy_automation_node(cfg, script_dir)


def run_lab_automation(script: Path, node_mode: str) -> int:
    """
    Run setup_lab_automation.sh for node_mode ("vm" or "container") and return its exit code. It runs in the
    directory holding lab.cfg (it sources ./lab.cfg) with LAB_AUTOMATION_NODE, LAB_PYTHON (this interpreter) and
    LAB_KUBECTL_INSTALL (the pinned kubectl install script) in its environment.
    """
    env = dict(os.environ, LAB_AUTOMATION_NODE=node_mode, LAB_PYTHON=sys.executable,
               LAB_KUBECTL_INSTALL=kvm_host_profiles.kubectl_binary_script())
    return subprocess.run(["bash", str(script)], cwd=str(script.parent), env=env, check=False).returncode


# ── lab.cfg generation ────────────────────────────────────────────────────────

def reverse_zone(network: ipaddress.IPv4Network) -> str:
    """Reverse-DNS zone prefix of `network`, e.g. 192.168.8.0/24 -> 8.168.192."""
    octets = str(network.network_address).split(".")[:max(1, network.prefixlen // 8)]
    return ".".join(reversed(octets))


def render_lab_cfg(text: str, values: Dict[str, str]) -> str:
    """
    `text` (lab.cfg or its template) with each KEY=value line in `values` replaced; comments are kept. Keys
    `text` lacks (a lab.cfg written before the key existed) are appended.
    """
    if not values:
        return text
    keys = "|".join(re.escape(k) for k in values)

    def repl(m: "re.Match") -> str:
        return "{}={}{}".format(m.group(1), shlex.quote(str(values[m.group(1)])), m.group(3) or "")
    out = re.sub(r"^({})=('[^']*'|\"[^\"]*\"|\S*)(\s+#.*)?$".format(keys), repl, text, flags=re.M)
    missing = [k for k in values if not re.search(r"^{}=".format(re.escape(k)), out, flags=re.M)]
    if missing:
        out = out.rstrip("\n") + "\n" + "".join(
            "{}={}\n".format(k, shlex.quote(str(values[k]))) for k in missing)
    return out


def suggest_free_ip(network: ipaddress.IPv4Network, exclude: Set[str]) -> str:
    """The highest address of `network` (below .250 of a /24's range) that does not answer a ping."""
    hosts = list(network.hosts())
    candidates = [h for h in reversed(hosts[:-5]) if str(h) not in exclude][:20]
    for h in candidates:
        if subprocess.run(["ping", "-c", "1", "-W", "1", str(h)], capture_output=True, check=False).returncode != 0:
            return str(h)
    return str(candidates[0])


def ensure_ssh_key(pub_path: str) -> str:
    """Contents of the public key at pub_path; an ed25519 key pair is created there first when absent."""
    pub = Path(pub_path).expanduser()
    if not pub.is_file():
        priv = pub.with_suffix("")
        priv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(priv)], check=True)
    return pub.read_text().strip()


def password_hash(password: str) -> str:
    return subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True,
                          text=True, check=True).stdout.strip()


def detect_timezone() -> str:
    out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True,
                         text=True, check=False).stdout.strip()
    if out:
        return out
    link = Path("/etc/localtime")
    return str(link.resolve()).split("zoneinfo/", 1)[-1] if link.is_symlink() else "UTC"


class Asker:
    """Prompts for a value with a default; returns the default untouched when not interactive."""

    def __init__(self, interactive: bool):
        self.interactive = interactive

    def __call__(self, question: str, default: str, choices: Optional[Tuple[str, ...]] = None) -> str:
        if not self.interactive:
            return default
        hint = "/".join(choices) if choices else default
        while True:
            answer = input("{} [{}]: ".format(question, hint)).strip() or default
            if not choices or answer in choices:
                return answer
            print("  choose one of: {}".format(", ".join(choices)))

    def password(self, given: str) -> Tuple[str, bool]:
        """(password, generated): `given`, else asked twice, else a random one when not interactive."""
        if given:
            return given, False
        if not self.interactive:
            return secrets.token_urlsafe(12), True
        while True:
            first = getpass.getpass("Root password for the automation VM: ")
            if first and first == getpass.getpass("Repeat it: "):
                return first, False
            print("  the passwords are empty or differ, try again")


# ── detection helpers for the questions below (generic facts about whichever host/cluster this
# runs against — never an assumption baked in from one particular machine) ──────────────────────

def _is_wireless(nic: str) -> bool:
    """True if `nic` is a wireless interface — it cannot be bridged the way a VM's network needs
    (no normal AP supports the 4-address/WDS mode a bridged wireless client would need), so nat
    is the real default there, not bridge."""
    return Path("/sys/class/net/{}/wireless".format(nic)).is_dir()


def _existing_ssh_keys() -> List[Path]:
    """Every ~/.ssh/id_*.pub already on this host."""
    return sorted(Path("~/.ssh").expanduser().glob("id_*.pub"))


def _scc_registered() -> bool:
    """True if this host already has SUSEConnect credentials on file (registered by some other
    means, e.g. at image build time) — asking for a regcode would be redundant."""
    return Path("/etc/zypp/credentials.d/SCCcredentials").is_file()


def scc_credential() -> Dict[str, str]:
    """This host's SCC registration credential (scc_regcode/scc_email/scc_url), from the
    encrypted credential store (libs/primary.py's try_load_service_credential) — never plaintext
    in a config file. {} when none is on file (an already-registered host needs none)."""
    data, _error = primary.try_load_service_credential("scc")
    return data or {}


def _write_scc_credential(regcode: str, email: str, url: str) -> None:
    """Writes this host's SCC registration fields to the encrypted-credential-store location
    (unencrypted here: this is a one-shot value used immediately by this same install run, not a
    long-lived secret an operator manages later — see libs/primary.py's "unencrypted:true" opt-out,
    which write_credential_file(encrypt=False) already produces)."""
    out_dir = Path(primary.credentials_dirs()[0])
    fields = {k: v for k, v in (("scc_regcode", regcode), ("scc_email", email), ("scc_url", url)) if v}
    setup_credentials.write_credential_file(out_dir / "scc.yaml", "scc", fields, encrypt=False)


def _default_kubeconfig() -> str:
    """$KUBECONFIG if set, else ~/.kube/config if it exists, else "" (kubectl's own default)."""
    env = os.environ.get("KUBECONFIG", "")
    if env:
        return env
    default = Path("~/.kube/config").expanduser()
    return str(default) if default.is_file() else ""


def _k8s_has_loadbalancer(kubeconfig: str) -> bool:
    """True if the cluster at `kubeconfig` can already satisfy a LoadBalancer Service — either one
    already has an external address, or a LB controller (MetalLB, a cloud provider's) is running.
    False (the macvlan-leaning default) on any error: kubectl missing, cluster unreachable, no
    kubeconfig at all — never assume a working LoadBalancer without real evidence."""
    cmd = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else [])
    svcs = subprocess.run(cmd + ["get", "svc", "-A", "-o",
                                "jsonpath={range .items[?(@.spec.type==\"LoadBalancer\")]}"
                                "{.status.loadBalancer.ingress}{end}"],
                         capture_output=True, text=True, check=False)
    if svcs.returncode == 0 and svcs.stdout.strip():
        return True
    pods = subprocess.run(cmd + ["get", "pods", "-A", "-o", "name"], capture_output=True, text=True, check=False)
    if pods.returncode != 0:
        return False
    return any("metallb" in line.lower() or "cloud-controller" in line.lower()
               for line in pods.stdout.splitlines())


def build_values(args: argparse.Namespace, ask: Asker,
                  profile_name: str) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """automation.cfg / kvm_host.cfg values from options, detected host settings and answers;
    plus the password if one was generated. Returns (automation_values, kvm_values, password)."""
    net = host_network.current_settings()

    mode_default = args.network_mode
    if not mode_default:
        if _is_wireless(net.nic):
            print("- {} is a wireless interface, which can't be bridged for a VM's network the "
                  "usual way — defaulting the lab network to nat instead of bridge.".format(net.nic))
            mode_default = "nat"
        else:
            mode_default = "bridge"
    mode = ask("Lab network: bridge (VMs on this host's LAN) or nat (private libvirt network)",
               mode_default, ("bridge", "nat"))

    domain = ask("Lab DNS domain", args.domain or "mydemo.lab")

    node_default = args.automation_node
    if not node_default:
        resources = kvm_host_profiles.host_resources()
        low_resource = resources["vcpus"] < 2 or (0 < resources["mem_mib"] < 4096)
        is_vm = kvm_host_profiles.is_virtualized_guest()
        if low_resource or is_vm:
            why = ("only {} vCPU/{} MiB RAM".format(resources["vcpus"], resources["mem_mib"]) if low_resource
                   else "this host is itself a VM (building a nested VM here is unusual)")
            print("- Defaulting the automation node to container: {}.".format(why))
            node_default = "container"
        else:
            node_default = "vm"
    automation = {"_mydomain": domain,
                  "_automation_node": ask("Automation node: vm, container (a podman container on this host) or "
                                          "kubernetes", node_default, ("vm", "container", "kubernetes"))}
    kvm: Dict[str, str] = {"_network_mode": mode}

    if automation["_automation_node"] == "kubernetes":
        automation["_k8s_kubeconfig"] = ask("kubeconfig of the cluster", args.k8s_kubeconfig
                                            or _default_kubeconfig())
        net_default = args.k8s_network or (
            "loadbalancer" if _k8s_has_loadbalancer(automation["_k8s_kubeconfig"]) else "macvlan")
        automation["_k8s_network"] = ask("How the lab reaches the automation node: loadbalancer or macvlan",
                                         net_default, ("loadbalancer", "macvlan"))
        if automation["_k8s_network"] == "macvlan":
            automation["_k8s_macvlan_master"] = ask("Cluster nodes' interface on the lab network",
                                                     args.k8s_macvlan_master or net.nic)
        automation.update({k: v for k, v in (("_k8s_namespace", args.k8s_namespace),
                                             ("_k8s_storage_class", args.k8s_storage_class),
                                             ("_k8s_volume_size", args.k8s_volume_size),
                                             ("_automation_image", args.automation_image)) if v})

    if mode == "bridge":
        on_bridge = host_network.is_bridge(net.nic)
        kvm["_bridge_name"] = net.nic if on_bridge else (args.bridge_name or "br0")
        kvm["_bridge_nic"] = "" if on_bridge else ask(
            "NIC to turn into bridge {}".format(kvm["_bridge_name"]), args.bridge_nic or net.nic)
        network = net.network
        gateway = args.gateway or net.gateway
        dns = args.dns or (net.dns[0] if net.dns else net.gateway)
        default_ip = args.automation_ip or suggest_free_ip(network, {net.ip, gateway, dns})
        kvm["_virt_srv"] = "root@{}".format(net.ip)
    else:
        nat_cidr = ask("NAT network for the automation node", args.nat_cidr or "192.168.150.0/24")
        network = ipaddress.ip_network(nat_cidr)
        gateway = dns = str(network.network_address + 1)
        default_ip = args.automation_ip or str(network.network_address + 10)
        kvm.update({"_bridge_name": "", "_bridge_nic": "", "_nat_network_cidr": str(network),
                   "_virt_srv": "root@{}".format(gateway)})

    ssh_keys = _existing_ssh_keys()
    ssh_default = args.ssh_pub_key or "~/.ssh/id_ed25519.pub"
    if len(ssh_keys) > 1:
        ssh_default = ask("Which SSH public key may log in as root on the automation node?",
                          str(ssh_keys[-1]), tuple(str(k) for k in ssh_keys))
    automation.update({
        "_myip": ask("IP address for the automation node on {}".format(network), default_ip),
        "_mynet": str(network), "_mygw": gateway, "_mydns": dns, "_mynetrev": reverse_zone(network),
        "AUTOMATION_HOSTNAME": "automation.{}".format(domain), "MYREG": "registry.{}".format(domain),
        "_timezone": args.timezone or detect_timezone(),
        "ROOT_SSH_PUB_KEY": ensure_ssh_key(ssh_default),
    })
    password, generated = ask.password(args.root_password or os.environ.get("LAB_ROOT_PASSWORD", ""))
    automation.update({"root_pwd": password, "ROOT_PWD_HASH": password_hash(password)})

    if profile_name.startswith("sles") and not _scc_registered():
        regcode = ask("SUSE registration code (needed: this host's own SLES repos require it)",
                      args.suse_regcode or "")
        if regcode:
            email = ask("SUSE registration e-mail (optional)", args.suse_email or "")
            url = ask("Registration server URL (optional — RMT/SMT instead of SCC)", args.suse_url or "")
            _write_scc_credential(regcode, email, url)

    return automation, kvm, (password if generated else "")


def _automation_flag_values(args: argparse.Namespace) -> Dict[str, str]:
    """automation.cfg keys set directly by options, applied on top of an existing automation.cfg."""
    pairs = {"_mydomain": args.domain, "_myip": args.automation_ip, "_mygw": args.gateway,
             "_mydns": args.dns, "_timezone": args.timezone, "_automation_node": args.automation_node,
             "_k8s_kubeconfig": args.k8s_kubeconfig, "_k8s_namespace": args.k8s_namespace,
             "_k8s_network": args.k8s_network, "_k8s_macvlan_master": args.k8s_macvlan_master,
             "_k8s_storage_class": args.k8s_storage_class, "_k8s_volume_size": args.k8s_volume_size,
             "_automation_image": args.automation_image}
    return {k: v for k, v in pairs.items() if v is not None}


def _kvm_flag_values(args: argparse.Namespace) -> Dict[str, str]:
    """kvm_host.cfg keys set directly by options, applied on top of an existing kvm_host.cfg."""
    pairs = {"_network_mode": args.network_mode, "_bridge_name": args.bridge_name,
             "_bridge_nic": args.bridge_nic, "_nat_network_cidr": args.nat_cidr}
    return {k: v for k, v in pairs.items() if v is not None}


# ── command line ──────────────────────────────────────────────────────────────

def build_arg_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The options setup_kvm_node.py and setup_automation.py both take — shared so each gets an
    accurate --help (its own prog/description), not one script's text leaking into the other's."""
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("target", nargs="?", help="set up this remote host over SSH instead of the local one")
    p.add_argument("--interactive", action="store_true",
                   help="ask for each lab.cfg value (the host's own settings are the defaults) and for confirmation "
                        "before changing the host. Without it nothing is asked: values come from options, an "
                        "existing lab.cfg or the host's own settings, and a root password is generated and printed "
                        "when none is given")
    p.add_argument("-y", "--yes", action="store_true",
                   help="with --interactive, do not ask for confirmation before changing the host")
    p.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--reconfigure", action="store_true", help="write a new lab.cfg even if one exists")
    p.add_argument("--network-mode", choices=("bridge", "nat"),
                   help="bridge: lab VMs on this host's LAN (default); nat: a private libvirt NAT network")
    p.add_argument("--bridge-name", help="bridge the lab VMs attach to (default br0, or the bridge already "
                                         "carrying the default route)")
    p.add_argument("--bridge-nic", help="NIC to turn into the bridge's port (default: the default-route NIC)")
    p.add_argument("--nat-cidr", help="NAT network for --network-mode nat (default 192.168.150.0/24)")
    p.add_argument("--automation-node", choices=("vm", "container", "kubernetes"),
                   help="run the automation node as a VM (default), as a podman container on this host, or in a "
                        "Kubernetes cluster (see the --k8s-* options); the container is also used when the VM "
                        "cannot be built")
    p.add_argument("--k8s-kubeconfig", help="kubeconfig of the cluster for --automation-node kubernetes "
                                            "(default: kubectl's own default)")
    p.add_argument("--k8s-namespace", help="namespace of the automation node (default lab-automation)")
    p.add_argument("--k8s-network", choices=("loadbalancer", "macvlan"),
                   help="how the lab reaches the automation node at its IP: a LoadBalancer Service (default, e.g. "
                        "MetalLB) or a Multus macvlan interface on the lab network")
    p.add_argument("--k8s-macvlan-master", help="cluster nodes' interface on the lab network, for --k8s-network macvlan")
    p.add_argument("--k8s-storage-class", help="storage class of the automation node's persistent volume "
                                               "(default: the cluster's default)")
    p.add_argument("--k8s-volume-size", help="size of the automation node's persistent volume (default 20Gi)")
    p.add_argument("--automation-image", help="automation node image for --automation-node kubernetes "
                                              "(default ghcr.io/rmahique/lab-automation-node:latest)")
    p.add_argument("--automation-ip", help="automation VM address (default: a free address on the lab network)")
    p.add_argument("--domain", help="lab DNS domain (default mydemo.lab)")
    p.add_argument("--gateway", help="gateway for the lab VMs (default: this host's gateway)")
    p.add_argument("--dns", help="upstream DNS server (default: this host's first DNS server)")
    p.add_argument("--root-password", help="root password for the automation VM (also read from "
                                           "LAB_ROOT_PASSWORD; default: asked for)")
    p.add_argument("--ssh-pub-key", help="public key allowed to log in as root on the automation VM "
                                         "(default ~/.ssh/id_ed25519.pub, created when missing)")
    p.add_argument("--timezone", help="automation VM timezone (default: this host's)")
    p.add_argument("--suse-regcode", help="SLES registration code (SLES hosts only)")
    p.add_argument("--suse-email", help="SLES registration e-mail")
    p.add_argument("--suse-url", help="registration server URL (RMT/SMT) instead of SCC")
    p.add_argument("--share-storage-from", nargs="?", const="", metavar="HOST",
                   help="sshfs-mount /var/lib/libvirt/images from HOST (default: kvm_host.cfg's _virt_srv)")
    p.add_argument("--copy-storage-from", nargs="?", const="", metavar="HOST",
                   help="one-time rsync copy of /var/lib/libvirt/images from HOST instead")
    return p


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = build_arg_parser(
        "setup_kvm_node.py",
        "Prepare this host (or TARGET over SSH) as a lab-in-a-box KVM hypervisor: install its "
        "packages, bridge its NIC, write automation.cfg/kvm_host.cfg and optionally create an "
        "automation node. Without those files, they are written from the options and the host's own "
        "settings; --interactive asks for each value.")
    return _finish_parse(p, argv)


def _finish_parse(p: argparse.ArgumentParser, argv: List[str]) -> argparse.Namespace:
    """The validation/defaulting both scripts' parse_args() share, once each has added its own
    extra options to the parser build_arg_parser() returned."""
    args = p.parse_args(argv)
    if args.share_storage_from is not None and args.copy_storage_from is not None:
        p.error("--share-storage-from and --copy-storage-from are mutually exclusive")
    if args.interactive and args.non_interactive:
        p.error("--interactive and --non-interactive are mutually exclusive")
    if not args.interactive:
        args.yes = True
    return args


def run_remote(target: str, argv: List[str]) -> None:
    """Copy setup_demo_server/ and libs/ to TARGET and run the bootstrap there with the same options."""
    if subprocess.run(["nc", "-z", "-w", "5", target, "22"], capture_output=True, check=False).returncode != 0:
        die("{} is not reachable on port 22".format(target))
    if subprocess.run(["ssh-copy-id", "root@{}".format(target)]).returncode != 0:
        die("an SSH key is needed to continue; create one with: ssh-keygen -t ed25519")
    remote_dir = "/var/tmp/lab-in-a-box-{}".format(int(time.time()))
    repo = _SCRIPT_DIR.parent
    tar = subprocess.Popen(["tar", "-C", str(repo), "-cf", "-", "setup_demo_server", "libs",
                            "install_demo_server_scripts.sh"], stdout=subprocess.PIPE)
    subprocess.run(["ssh", "root@{}".format(target), "mkdir -p {0} && tar -C {0} -xf -".format(remote_dir)],
                   stdin=tar.stdout, check=True)
    tar.wait()
    remote_args = " ".join(shlex.quote(a) for a in argv if a != target)
    subprocess.run(["ssh", "-t", "root@{}".format(target),
                    "bash {0}/install_demo_server_scripts.sh --source {0} {1}".format(remote_dir, remote_args)])


def review_cfg(paths: List[Path]) -> None:
    """Prints each of `paths`' full contents and where it lives, so an operator in --interactive
    mode can see exactly what's about to be applied — and, since this runs before the
    confirmation prompt, still has a real chance to edit the file in another terminal before
    answering yes (main() reloads after confirming, so an edit here actually takes effect)."""
    for path in paths:
        if not path.is_file():
            continue
        print("\n--- {} (edit now if you want to change anything, then confirm below) ---".format(path))
        print(path.read_text().rstrip("\n"))
    print()


def _write_cfg(path: Path, template_name: str, reconfigure: bool, values: Dict[str, str],
               flag_values: Dict[str, str]) -> None:
    """Writes `path` from its template+values (fresh/--reconfigure) or updates an existing file
    in place with flag_values only — the same pattern main() always used for the one lab.cfg,
    now shared by automation.cfg and kvm_host.cfg."""
    if reconfigure or not path.is_file():
        path.write_text(render_lab_cfg(_find(template_name).read_text(), values))
    else:
        path.write_text(render_lab_cfg(path.read_text(), flag_values))
    path.chmod(0o600)


def _auto_split_legacy_lab_cfg(automation_path: Path, kvm_path: Path) -> bool:
    """
    The one-time automation.cfg/kvm_host.cfg split, applied automatically on startup rather than
    left as a manual `migrate_config.py` + promote-the-output chore: when neither new file exists
    yet but a pre-split lab.cfg does, split it straight into both real, live files (never
    touching lab.cfg itself) and report what moved. Mirrors libs/migrations.py's
    ensure_current() for lab definitions — detect, migrate, report, continue with the result —
    adapted for a 1-input/2-output structural split instead of an in-place field rewrite.
    Returns True if a split happened (so the caller's own "fresh" check re-evaluates to False).
    """
    legacy_path = _find("lab.cfg")
    if not legacy_path.is_file():
        return False
    import migrate_config  # noqa: E402 (scripts/ sibling — see sys.path setup above)
    log("{} is from before the automation.cfg/kvm_host.cfg split — splitting it now".format(legacy_path))
    for change in migrate_config.split_legacy_lab_cfg(legacy_path, automation_out=automation_path, kvm_out=kvm_path):
        print("- {}".format(change))
    print("{} itself is untouched; it will be regenerated as the derived file once this run finishes.".format(
        legacy_path))
    return True


def write_lab_cfg(cfg: Dict[str, str]) -> Path:
    """Writes the derived setup_demo_server/lab.cfg that setup_lab_automation.sh (bash) still
    sources (`. lab.cfg`) — regenerated from automation.cfg + kvm_host.cfg every run, never
    hand-edited. Must run before deploy_automation_node(), which assumes it is already there.
    Shared by this script's own main() and setup_automation.py's."""
    lab_cfg_path = _find("lab.cfg")
    lab_cfg_path.write_text(
        "# Generated from automation.cfg + kvm_host.cfg — edit those, not this file.\n" +
        "\n".join("{}={}".format(k, shlex.quote(str(v))) for k, v in cfg.items()) + "\n")
    lab_cfg_path.chmod(0o600)
    return lab_cfg_path


def main(argv: Optional[List[str]] = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if args.target:
        run_remote(args.target, argv)
        return

    profile = kvm_host_profiles.detect_profile()
    automation_path, kvm_path = _find("automation.cfg"), _find("kvm_host.cfg")
    if not args.reconfigure and not automation_path.is_file():
        _auto_split_legacy_lab_cfg(automation_path, kvm_path)
    generated_password = ""
    ask = Asker(interactive=args.interactive)
    fresh = args.reconfigure or not automation_path.is_file()
    if fresh:
        log("Writing automation.cfg and kvm_host.cfg")
        automation_values, kvm_values, generated_password = build_values(args, ask, profile.name if profile else "")
    else:
        automation_values = kvm_values = {}
    _write_cfg(automation_path, "automation.cfg.template", fresh, automation_values,
              _automation_flag_values(args))
    _write_cfg(kvm_path, "kvm_host.cfg.template", fresh, kvm_values, _kvm_flag_values(args))
    log("Loading configuration files {} and {}".format(automation_path, kvm_path))
    cfg = dict(primary.load_shell_vars(kvm_path), **primary.load_shell_vars(automation_path))

    share_from, copy_from = args.share_storage_from, args.copy_storage_from
    if share_from == "" or copy_from == "":
        host = _primary_storage_host(cfg)
        if not host:
            die("--share-storage-from/--copy-storage-from need a HOST (kvm_host.cfg's _virt_srv is not set)")
        share_from = host if share_from == "" else share_from
        copy_from = host if copy_from == "" else copy_from

    # Whether this run also builds an automation node here, or just prepares this host as a KVM
    # node (e.g. a 2nd/3rd host joining a lab whose automation node already lives elsewhere).
    # Reachability is only meaningful when automation.cfg already names a real _myip from a
    # previous run; on a genuine first-time install there is nothing to probe, so default yes.
    build_default = "yes"
    if not fresh and _automation_host_reachable(cfg.get("_myip", "")):
        build_default = "no"
        print("- An automation node is already answering at {} — defaulting to not building another "
              "one.".format(cfg.get("_myip", "")))
    build_automation_node = ask("Also build an automation node on this KVM host?", build_default,
                                ("yes", "no")) == "yes"

    print("\nlab network {} ({} mode), KVM host ready{}".format(
        cfg.get("_mynet", ""), cfg.get("_network_mode", "bridge"),
        ", automation node {} ({}) at {}, gateway {}, DNS {}".format(
            cfg.get("AUTOMATION_HOSTNAME", ""), cfg.get("_automation_node", "") or "vm",
            cfg.get("_myip", ""), cfg.get("_mygw", ""), cfg.get("_mydns", "")) if build_automation_node else ""))
    if cfg.get("_bridge_nic"):
        print("{} becomes a port of bridge {} (the host keeps its address)".format(
            cfg["_bridge_nic"], cfg.get("_bridge_name") or "br0"))
    if args.interactive:
        review_cfg([automation_path, kvm_path])
    if not args.yes and input("Set up this host with these settings? (yes/no): ").strip() != "yes":
        print("Nothing changed. Edit {}/{} or run again with different options.".format(
            automation_path, kvm_path))
        return
    # Reload in case the operator edited either file during the pause above — the confirmation
    # is a real chance to review and change something, not just a rubber stamp of what was
    # already decided.
    cfg = dict(primary.load_shell_vars(kvm_path), **primary.load_shell_vars(automation_path))
    write_lab_cfg(cfg)

    do_it_all(cfg, _SCRIPT_DIR, share_storage_from=share_from, copy_storage_from=copy_from,
             build_automation_node=build_automation_node)
    if generated_password:
        print("\nGenerated root password for the automation node: {}  (also in {})".format(
            generated_password, automation_path))

    if args.interactive:
        if ask("Add another KVM node now? (needs its SSH access)", "no", ("yes", "no")) == "yes":
            target = ask("Hostname or IP of the new KVM node", "")
            if target:
                extra_argv = ["--share-storage-from", host_network.current_settings().ip] if ask(
                    "Share this host's storage with it over sshfs?", "yes", ("yes", "no")) == "yes" else []
                run_remote(target, extra_argv)
        if ask("Add cloud credentials now?", "no", ("yes", "no")) == "yes":
            setup_credentials.interactive_build(primary.credentials_dirs()[0])


if __name__ == "__main__":
    main()
