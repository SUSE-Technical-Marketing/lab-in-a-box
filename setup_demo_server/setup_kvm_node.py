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

Without setup_demo_server/lab.cfg, it is written first: each value is asked for, with this host's
own network settings as defaults, or taken from options / detected values with --non-interactive.
An existing lab.cfg is used as is; options that map to its keys update them in place.

Then it installs the hypervisor packages, turns the default-route NIC into a bridge that keeps the
host's address (rolled back automatically if the gateway stops answering), configures libvirt and
creates the automation VM. If an automation host (lab.cfg's _myip) is already up and reachable,
this host's DNS is pointed at it.

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
for _candidate in ("/usr/local/lib/lab_creation", str(_SCRIPT_DIR.parent / "libs")):
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
    wrong image. lab.cfg's _QCOW_IMAGE must be set to the correct, real
    filename for whichever Leap version is targeted — see lab.cfg.template's
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
    urllib.request.urlretrieve(url, str(dest))


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


def do_it_all(cfg, script_dir, share_storage_from=None, copy_storage_from=None):
    """
    Mirrors do_it_all (bash). share_storage_from/copy_storage_from are new,
    additive, and both default to None (today's unchanged single-host
    behavior: this host manages its own local storage, no DNS reconfigured
    — identical to the original first-KVM-node bootstrap flow)."""
    lab_automation_script = _find("setup_lab_automation.sh")
    if not lab_automation_script.is_file():
        die("Missing script, please download setup_lab_automation.sh script from the GIT repository")

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
        log("Adding extra packages from lab.cfg's _extra_host_pkgs")
        profile.packages = profile.packages + extra_pkgs

    if isinstance(profile, kvm_host_profiles._SuseRegisteredProfile):
        # SLES only — see _SuseRegisteredProfile.register_repos()'s own
        # docstring: SUSEConnect --product fails outright on a genuinely
        # unregistered host without this.
        profile.regcode = cfg.get("SUSE_regcode", "")
        profile.suse_email = cfg.get("SUSE_email", "")
        profile.suse_url = cfg.get("SUSE_url", "")

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
                 "and update _virt_srv in lab.cfg if it named the old one".format(
                     bridge_name, after.ip, before.ip, after.mac))

    if share_storage_from or copy_storage_from:
        setup_shared_storage(share_storage_from, copy_storage_from)

    if _automation_host_reachable(cfg.get("_myip", "")):
        configure_automation_dns(cfg)

    download_automation_image(cfg.get("_QCOW_IMAGE", ""))

    enable_libvirt()
    define_storage_pool()
    subprocess.run(["systemctl", "disable", "--now", "firewalld"], check=False)

    if (cfg.get("_network_mode", "") or "bridge") == "nat":
        nat_name = cfg.get("_nat_network_name", "") or "labnat"
        nat_cidr = cfg.get("_nat_network_cidr", "") or "192.168.150.0/24"
        log("Configure NAT'd libvirt network {} ({})".format(nat_name, nat_cidr))
        configure_nat_network(nat_name, nat_cidr)

    log("Start setup_lab_automation.sh script to create the automation VM")
    # cwd must be wherever lab.cfg actually lives (setup_lab_automation.sh
    # sources ./lab.cfg relative to its cwd, not its own script path) — that
    # may be _find()'s bash-tree sibling directory, not script_dir.
    rc = subprocess.run(["bash", str(lab_automation_script)], cwd=str(lab_automation_script.parent),
                        check=False).returncode
    if rc != 0:
        die("{} failed (exit {}); see its output above".format(lab_automation_script.name, rc))


# ── lab.cfg generation ────────────────────────────────────────────────────────

def reverse_zone(network: ipaddress.IPv4Network) -> str:
    """Reverse-DNS zone prefix of `network`, e.g. 192.168.8.0/24 -> 8.168.192."""
    octets = str(network.network_address).split(".")[:max(1, network.prefixlen // 8)]
    return ".".join(reversed(octets))


def render_lab_cfg(text: str, values: Dict[str, str]) -> str:
    """`text` (lab.cfg or its template) with each KEY=value line in `values` replaced; comments are kept."""
    if not values:
        return text
    keys = "|".join(re.escape(k) for k in values)

    def repl(m: "re.Match") -> str:
        return "{}={}{}".format(m.group(1), shlex.quote(str(values[m.group(1)])), m.group(3) or "")
    return re.sub(r"^({})=('[^']*'|\"[^\"]*\"|\S*)(\s+#.*)?$".format(keys), repl, text, flags=re.M)


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


def build_values(args: argparse.Namespace, ask: Asker, profile_name: str) -> Tuple[Dict[str, str], str]:
    """lab.cfg values from options, detected host settings and answers; plus the password if one was generated."""
    net = host_network.current_settings()
    mode = ask("Lab network: bridge (VMs on this host's LAN) or nat (private libvirt network)",
               args.network_mode or "bridge", ("bridge", "nat"))
    domain = ask("Lab DNS domain", args.domain or "mydemo.lab")
    values = {"_network_mode": mode, "_mydomain": domain}
    if mode == "bridge":
        on_bridge = host_network.is_bridge(net.nic)
        values["_bridge_name"] = net.nic if on_bridge else (args.bridge_name or "br0")
        values["_bridge_nic"] = "" if on_bridge else ask(
            "NIC to turn into bridge {}".format(values["_bridge_name"]), args.bridge_nic or net.nic)
        network = net.network
        gateway = args.gateway or net.gateway
        dns = args.dns or (net.dns[0] if net.dns else net.gateway)
        default_ip = args.automation_ip or suggest_free_ip(network, {net.ip, gateway, dns})
        values["_virt_srv"] = "root@{}".format(net.ip)
    else:
        network = ipaddress.ip_network(args.nat_cidr or "192.168.150.0/24")
        gateway = dns = str(network.network_address + 1)
        default_ip = args.automation_ip or str(network.network_address + 10)
        values.update({"_bridge_name": "", "_bridge_nic": "", "_nat_network_cidr": str(network),
                       "_virt_srv": "root@{}".format(gateway)})
    values.update({
        "_myip": ask("IP address for the automation VM on {}".format(network), default_ip),
        "_mynet": str(network), "_mygw": gateway, "_mydns": dns, "_mynetrev": reverse_zone(network),
        "AUTOMATION_HOSTNAME": "automation.{}".format(domain), "MYREG": "registry.{}".format(domain),
        "_timezone": args.timezone or detect_timezone(),
        "ROOT_SSH_PUB_KEY": ensure_ssh_key(args.ssh_pub_key or "~/.ssh/id_ed25519.pub"),
    })
    password, generated = ask.password(args.root_password or os.environ.get("LAB_ROOT_PASSWORD", ""))
    values.update({"root_pwd": password, "ROOT_PWD_HASH": password_hash(password)})
    if profile_name.startswith("sles"):
        values["SUSE_regcode"] = ask("SUSE registration code (empty if this host is already registered)",
                                     args.suse_regcode or "")
        values["SUSE_email"] = args.suse_email or ""
        values["SUSE_url"] = args.suse_url or ""
    return values, password if generated else ""


def _flag_values(args: argparse.Namespace) -> Dict[str, str]:
    """lab.cfg keys set directly by options, applied on top of an existing lab.cfg."""
    pairs = {"_network_mode": args.network_mode, "_mydomain": args.domain, "_bridge_name": args.bridge_name,
             "_bridge_nic": args.bridge_nic, "_myip": args.automation_ip, "_mygw": args.gateway,
             "_mydns": args.dns, "_nat_network_cidr": args.nat_cidr, "_timezone": args.timezone,
             "SUSE_regcode": args.suse_regcode, "SUSE_email": args.suse_email, "SUSE_url": args.suse_url}
    return {k: v for k, v in pairs.items() if v is not None}


# ── command line ──────────────────────────────────────────────────────────────

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="setup_kvm_node.py",
        description="Prepare this host (or TARGET over SSH) as a lab-in-a-box KVM hypervisor: install its "
                    "packages, bridge its NIC, write lab.cfg and create the automation VM. Without lab.cfg, "
                    "every value is asked for, with the host's own settings as defaults.")
    p.add_argument("target", nargs="?", help="set up this remote host over SSH instead of the local one")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation before changing the host")
    p.add_argument("--non-interactive", action="store_true",
                   help="never prompt: values come from options, an existing lab.cfg or the host's own "
                        "settings; a root password is generated and printed when none is given (implies --yes)")
    p.add_argument("--reconfigure", action="store_true", help="write a new lab.cfg even if one exists")
    p.add_argument("--network-mode", choices=("bridge", "nat"),
                   help="bridge: lab VMs on this host's LAN (default); nat: a private libvirt NAT network")
    p.add_argument("--bridge-name", help="bridge the lab VMs attach to (default br0, or the bridge already "
                                         "carrying the default route)")
    p.add_argument("--bridge-nic", help="NIC to turn into the bridge's port (default: the default-route NIC)")
    p.add_argument("--nat-cidr", help="NAT network for --network-mode nat (default 192.168.150.0/24)")
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
                   help="sshfs-mount /var/lib/libvirt/images from HOST (default: lab.cfg's _virt_srv)")
    p.add_argument("--copy-storage-from", nargs="?", const="", metavar="HOST",
                   help="one-time rsync copy of /var/lib/libvirt/images from HOST instead")
    args = p.parse_args(argv)
    if args.share_storage_from is not None and args.copy_storage_from is not None:
        p.error("--share-storage-from and --copy-storage-from are mutually exclusive")
    if args.non_interactive:
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


def main(argv: Optional[List[str]] = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if args.target:
        run_remote(args.target, argv)
        return

    profile = kvm_host_profiles.detect_profile()
    cfg_path = _find("lab.cfg")
    generated_password = ""
    if args.reconfigure or not cfg_path.is_file():
        log("Writing lab.cfg")
        ask = Asker(interactive=not args.non_interactive)
        values, generated_password = build_values(args, ask, profile.name if profile else "")
        cfg_path.write_text(render_lab_cfg(_find("lab.cfg.template").read_text(), values))
    else:
        cfg_path.write_text(render_lab_cfg(cfg_path.read_text(), _flag_values(args)))
    cfg_path.chmod(0o600)
    log("Loading configuration file {}".format(cfg_path))
    cfg = primary.load_shell_vars(cfg_path)

    share_from, copy_from = args.share_storage_from, args.copy_storage_from
    if share_from == "" or copy_from == "":
        host = _primary_storage_host(cfg)
        if not host:
            die("--share-storage-from/--copy-storage-from need a HOST (lab.cfg's _virt_srv is not set)")
        share_from = host if share_from == "" else share_from
        copy_from = host if copy_from == "" else copy_from

    print("\nlab network {} ({} mode), automation VM {} at {}, gateway {}, DNS {}".format(
        cfg.get("_mynet", ""), cfg.get("_network_mode", "bridge"), cfg.get("AUTOMATION_HOSTNAME", ""),
        cfg.get("_myip", ""), cfg.get("_mygw", ""), cfg.get("_mydns", "")))
    if cfg.get("_bridge_nic"):
        print("{} becomes a port of bridge {} (the host keeps its address)".format(
            cfg["_bridge_nic"], cfg.get("_bridge_name") or "br0"))
    if not args.yes and input("Set up this host with these settings? (yes/no): ").strip() != "yes":
        print("Nothing changed. Edit {} or run again with different options.".format(cfg_path))
        return

    do_it_all(cfg, _SCRIPT_DIR, share_storage_from=share_from, copy_storage_from=copy_from)
    if generated_password:
        print("\nGenerated root password for the automation VM: {}  (also in {})".format(
            generated_password, cfg_path))


if __name__ == "__main__":
    main()
