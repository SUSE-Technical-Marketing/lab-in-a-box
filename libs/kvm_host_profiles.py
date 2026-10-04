"""
kvm_host_profiles.py: per-OS package and repository setup for KVM hypervisor hosts.

The profiles replace a hardcoded if/elif on the OS id. Each profile knows how to:
  - detect whether it applies to an /etc/os-release id or id_like
  - refresh package metadata
  - install its package list
  - register repositories or subscriptions where needed (SUSEConnect, apt repositories and so on)

Adding a host OS means adding one profile class.

Usage:
    profile = detect_profile()
    if profile is None:
        die(...)
    profile.register_repos()
    profile.refresh()
    profile.update()
    profile.install()
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import platform
import re
import subprocess
from pathlib import Path


def _read_os_release():
    info = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                info[key] = val.strip('"')
    except OSError:
        pass
    return info


class HostOSProfile:
    """
    Base class for a host OS package-installation profile.

    packages           : reliably-available packages installed straight from
                          the distro's own default repos.
    unmapped_packages  : bash's package list for this OS family that this
                          profile does NOT (yet) know how to install
                          automatically here — printed as warnings rather than
                          silently skipped or guessed at with an unverified
                          package/repo name. Empty for the two OSes bash
                          already handled (their lists are ported verbatim).
    """

    name = "generic"
    packages = []
    unmapped_packages = []

    def __init__(self, os_info):
        self.os_info = os_info

    def register_repos(self):
        """Override for any one-time repo/subscription registration. No-op by default."""
        pass

    def refresh(self):
        raise NotImplementedError

    def update(self):
        raise NotImplementedError

    def install(self):
        raise NotImplementedError

    def configure_dns(self, automation_ip, mydomain):
        """
        Point this host's DNS resolution at the automation host. Default
        implementation: rewrite /etc/resolv.conf directly with a fresh
        `search`/`nameserver` pair, dropping any previous ones — simplest
        portable approach, used as-is for OS families with no existing bash
        behavior to match (Debian/RHEL families below). SUSE profiles
        override this with the netconfig-based approach the existing bash
        configure_host_dns() already uses (see setup_lab_automation.sh),
        since that's the established, working mechanism there.

        Only called once an automation host already exists — see
        setup_kvm_node.py, which gates this on /etc/lab_creation.cfg being
        present (never called during the very first bootstrap of the first
        KVM node, before any automation host exists to point at).
        """
        resolv = Path("/etc/resolv.conf")
        try:
            lines = resolv.read_text().splitlines()
        except OSError:
            lines = []
        kept = [l for l in lines if not l.strip().startswith(("nameserver", "search"))]
        new_lines = ["search {}".format(mydomain), "nameserver {}".format(automation_ip)] + kept
        resolv.write_text("\n".join(new_lines) + "\n")

    def _run(self, cmd):
        subprocess.run(cmd, check=True)

    def _service_active(self, name):
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", name], check=False
        ).returncode == 0

    def configure_bridge(self, nic, bridge_name):
        """
        Create bridge_name and enslave nic to it, if it doesn't already
        exist. Detects which network stack is actually live on THIS host at
        runtime (a different question from "which OS" — nmcli vs. wicked is
        an install-time choice independent of distro/version) rather than
        assuming one per OS family. Default: nmcli when NetworkManager is
        active; NotImplementedError otherwise (override per family below for
        anything else this project needs to support).
        """
        if self._service_active("NetworkManager"):
            # Migrate the NIC's current connection to the bridge before creating the bridge. If the NIC keeps its connection, the
            # bridge stays in "activating (waiting for ports)". The bridge also needs the host's static IPv4 config, or the host
            # loses its address. The NIC's current connection and its static IPv4 config are captured, the bridge gets the same
            # config, and the old connection is deactivated, not deleted, so the bridge slave can take over the device.
            existing_conn = subprocess.run(
                ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", nic],
                capture_output=True, text=True,
            ).stdout.strip().split(":", 1)[-1]
            if existing_conn == "--":  # nmcli's "unset" placeholder, seen in some versions/locales
                existing_conn = ""

            method = addr = gw = dns = ""
            if existing_conn:
                def _get(field):
                    return subprocess.run(
                        ["nmcli", "-g", field, "con", "show", existing_conn],
                        capture_output=True, text=True,
                    ).stdout.strip()
                method = _get("ipv4.method")
                addr = _get("ipv4.addresses")
                gw = _get("ipv4.gateway")
                dns = _get("ipv4.dns")

            bridge_args = ["nmcli", "con", "add", "type", "bridge",
                           "con-name", bridge_name, "ifname", bridge_name]
            if method == "manual" and addr:
                bridge_args += ["ipv4.method", "manual", "ipv4.addresses", addr]
                if gw:
                    bridge_args += ["ipv4.gateway", gw]
                if dns:
                    bridge_args += ["ipv4.dns", dns]
            self._run(bridge_args)

            if existing_conn:
                # down, not delete: recoverable if anything below fails.
                subprocess.run(["nmcli", "con", "down", existing_conn], check=False)

            self._run(["nmcli", "con", "add", "type", "bridge-slave",
                       "ifname", nic, "master", bridge_name])
            self._run(["nmcli", "con", "up", bridge_name])
            return
        raise NotImplementedError(
            "configure_bridge: no supported live network stack detected "
            "(NetworkManager not active) for {}".format(self.name)
        )


# ── openSUSE Leap / SLES (zypper) ───────────────────────────────────────────
#
# One shared _SuseZypperProfile supplies the actual mechanics (refresh/update/
# install/configure_dns via zypper+netconfig) so that code is written once,
# not duplicated per version. Each concrete OS+version below is its own plain
# class that just assigns its own literal `packages`/`unmapped_packages` (and,
# for SLES, `_products`) — mirrors the style of the original bash dispatch
# (identify the OS+version, then a branch that just sets variables) rather
# than deriving one version's list from another's via runtime logic. Leap 15
# and SLES 15 lists are ported verbatim from bash — including the gap already
# flagged there: the SLES list is missing libvirt-daemon-qemu/qemu-tools/
# virt-install/libguestfs (present in the Leap list), and bash's own comment
# says guestmount's real dependency there hasn't been verified. Not "fixed"
# here — preserved exactly, since inventing a package name for that gap
# without verifying it against a real SLES host would just be a different
# kind of guess. Leap 16 / SLES 16 lists are UNVERIFIED against a real host
# (see kvm_host_profiles' module docs / TODO) — the OBS-devel-origin packages
# with no confirmed openSUSE-16-era availability are listed in
# unmapped_packages instead of packages, so they're warned about rather than
# silently attempted.

class _SuseZypperProfile(HostOSProfile):
    """Shared zypper/netconfig mechanics for every openSUSE Leap/SLES version below."""

    def refresh(self):
        self._run(["zypper", "refresh"])

    def update(self):
        self._run(["zypper", "update", "-y"])

    def install(self):
        self._run(["zypper", "install", "-y"] + self.packages)

    def configure_dns(self, automation_ip, mydomain):
        """
        Mirrors configure_host_dns() in setup_lab_automation.sh: SUSE's
        netconfig mechanism regenerates /etc/resolv.conf from
        NETCONFIG_DNS_STATIC_SERVERS/_SEARCHLIST in
        /etc/sysconfig/network/config, so that's the file to edit rather than
        resolv.conf directly (which netconfig would just overwrite again).
        """
        cfg = Path("/etc/sysconfig/network/config")
        text = cfg.read_text()
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SERVERS=.*$',
                      'NETCONFIG_DNS_STATIC_SERVERS="{}"'.format(automation_ip), text, flags=re.M)
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SEARCHLIST=.*$',
                      'NETCONFIG_DNS_STATIC_SEARCHLIST="{}"'.format(mydomain), text, flags=re.M)
        cfg.write_text(text)
        subprocess.run(["netconfig", "update", "-f"], check=False)

    def configure_bridge(self, nic, bridge_name):
        """
        nmcli when NetworkManager is live (base class); wicked ifcfg files
        when it isn't — wicked is still a common openSUSE/SLES install-time
        default. Runtime service detection, same reasoning as the base
        class's docstring.
        """
        if self._service_active("NetworkManager"):
            return super().configure_bridge(nic, bridge_name)
        if self._service_active("wickedd"):
            sysconfig = Path("/etc/sysconfig/network")
            (sysconfig / "ifcfg-{}".format(bridge_name)).write_text(
                "BOOTPROTO='dhcp'\nSTARTMODE='auto'\n"
                "BRIDGE='yes'\nBRIDGE_PORTS='{}'\n".format(nic)
            )
            (sysconfig / "ifcfg-{}".format(nic)).write_text(
                "BOOTPROTO='none'\nSTARTMODE='auto'\n"
            )
            subprocess.run(["wicked", "ifreload", "all"], check=False)
            return
        raise NotImplementedError(
            "configure_bridge: neither NetworkManager nor wicked is active "
            "for {}".format(self.name)
        )


class _SuseRegisteredProfile(_SuseZypperProfile):
    """
    Shared SUSEConnect registration mechanics for every SLES version below.

    regcode/suse_email/suse_url are set externally (by the caller, from
    lab.cfg's SUSE_regcode/SUSE_email/SUSE_url — see setup_kvm_node.py's
    do_it_all()) after detect_profile() returns an instance, the same
    post-construction-mutation pattern already used for _extra_host_pkgs.
    Not constructor params: detect_profile() has no access to lab.cfg, only
    the live host's own /etc/os-release.
    """

    _products = ()  # set per concrete class
    regcode = ""
    suse_email = ""
    suse_url = ""

    def register_repos(self):
        # Register the base product first when a registration code is available. Adding a module to an unregistered host fails,
        # so the base product is registered before the modules. Registering an already registered host again is a no-op.
        if self.regcode:
            base_args = ["SUSEConnect", "--regcode", self.regcode]
            if self.suse_email:
                base_args += ["--email", self.suse_email]
            if self.suse_url:
                base_args += ["--url", self.suse_url]
            self._run(base_args)
        else:
            raise RuntimeError(
                "SLES host registration requires a regcode — set SUSE_regcode "
                "(and optionally SUSE_email/SUSE_url) in lab.cfg, or pre-register "
                "this host with SUSEConnect yourself before running this.")

        ver_id = self.os_info.get("VERSION_ID", "")
        arch = platform.machine()
        for product in self._products:
            self._run(["SUSEConnect", "--product", "{}/{}/{}".format(product, ver_id, arch)])


class OpenSUSELeap15Profile(_SuseZypperProfile):
    name = "opensuse-leap-15"
    packages = [
        "libvirt", "podman", "docker", "cri-tools", "minikube-bash-completion",
        "kubectl-who-can", "kubevirt-virtctl", "kubernetes1.28-client",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "ftsteutates-sensors",
        "netcat-openbsd", "gptfdisk", "libvirt-daemon-qemu", "qemu-tools",
        "virt-install", "libguestfs",
        # fuse3 must be installed. guestmount injects files into the automation VM image, and it needs fusermount3 to flush its
        # writes. Without fuse3 the writes are lost and the image boots un-injected, with no error. Minimal installs do not pull
        # fuse3 in as a dependency, so it is listed explicitly.
        "fuse3",
    ]


class OpenSUSELeap16Profile(_SuseZypperProfile):
    name = "opensuse-leap-16"
    # Leap 16.0 names: gpgme-devel is libgpgme-devel, and cri-tools is not available. The other packages in this list install as they
    # are. Packages whose availability is unverified are kept in unmapped_packages, not guessed at.
    packages = [
        "libvirt", "podman", "docker", "libgpgme-devel",
        "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "netcat-openbsd", "gptfdisk",
        "libvirt-daemon-qemu", "qemu-tools", "virt-install", "libguestfs",
    ]
    unmapped_packages = [
        "minikube-bash-completion", "kubectl-who-can", "kubevirt-virtctl",
        "kubernetes1.28-client", "ftsteutates-sensors",
        # cri-tools is not available on Leap 16, and no renamed package exists.
        "cri-tools",
    ]


class SLES15Profile(_SuseRegisteredProfile):
    name = "sles-15"
    packages = [
        "libvirt", "podman", "docker", "cri-tools", "minikube-bash-completion",
        "kubectl-who-can", "kubevirt-virtctl", "kubernetes1.28-client",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "ftsteutates-sensors",
        "netcat-openbsd", "gptfdisk",
        # guestfs-tools provides virt-customize and virt-ls, which the virt_customize provisioning path needs. virt-install is not
        # added, because SLES provides it through the libvirt package's dependencies. guestfs-tools is the package that is missing.
        "guestfs-tools",
        # NOTE: bash's SLES list stops here — no libvirt-daemon-qemu/qemu-tools/
        # virt-install, unlike the Leap list above. Preserved as-is.
    ]
    _products = ("PackageHub", "sle-module-containers", "sle-module-basesystem", "sle-module-legacy")


class SLES16Profile(_SuseRegisteredProfile):
    name = "sles-16"
    packages = [
        "libvirt", "podman", "docker",
        # gpgme-devel is named libgpgme-devel on SLES 16. The other package names below are available as they are.
        "libgpgme-devel",
        "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "netcat-openbsd", "gptfdisk",
        # guestfs-tools: same confirmed-on-15 fix as SLES15Profile (see its
        # own comment) — confirmed available under the same name on SLES 16
        # too, same live host as the rest of this list.
        "guestfs-tools",
    ]
    unmapped_packages = [
        "minikube-bash-completion", "kubectl-who-can", "kubevirt-virtctl",
        "kubernetes1.28-client", "ftsteutates-sensors",
        # cri-tools is not available on SLES 16, and no renamed package exists.
        "cri-tools",
    ]
    # SLES 16 has no separate sle-module-containers, sle-module-basesystem or sle-module-legacy. Those modules are part of the base
    # product. Only PackageHub is needed in addition, for the packages in this list.
    _products = ("PackageHub",)


# ── Ubuntu / Debian (apt) ────────────────────────────────────────────────────
#
# This profile maps the core virtualization and networking packages to their Debian equivalents. Some packages need a third-party
# repository (Docker, and the Kubernetes repository for kubectl and cri-tools). minikube, virtctl and kubectl-who-can are installed
# from upstream binaries. Those packages are listed in unmapped_packages and reported as warnings. ftsteutates-sensors is a SUSE
# kernel-module package with no Debian equivalent.
class DebianProfile(HostOSProfile):
    name = "debian"
    packages = [
        "libvirt-daemon-system", "libvirt-clients", "qemu-kvm", "qemu-utils",
        "virtinst", "libguestfs-tools",
        "libgpgme-dev", "libdevmapper-dev", "libbtrfs-dev",
        "git", "mc", "bridge-utils", "tcpdump", "lm-sensors",
        "netcat-openbsd", "gdisk", "podman",
    ]
    unmapped_packages = [
        "docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
        "kubevirt-virtctl", "kubernetes1.28-client", "ftsteutates-sensors",
    ]

    def refresh(self):
        self._run(["apt-get", "update"])

    def update(self):
        self._run(["apt-get", "upgrade", "-y"])

    def install(self):
        self._run(["apt-get", "install", "-y"] + self.packages)


# ── RHEL / CentOS / Rocky / AlmaLinux / Fedora (dnf) ────────────────────────
#
# As for Debian, docker, cri-tools and kubectl need their own repositories, which this profile does not add. minikube, virtctl,
# kubectl-who-can and ftsteutates-sensors have no dnf package. They are listed in unmapped_packages.
class RHELProfile(HostOSProfile):
    name = "rhel"
    packages = [
        "libvirt", "libvirt-client", "qemu-kvm", "virt-install", "libguestfs-tools",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel",
        "git", "mc", "bridge-utils", "tcpdump", "lm_sensors",
        "nmap-ncat", "gdisk", "podman",
    ]
    unmapped_packages = [
        "docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
        "kubevirt-virtctl", "kubernetes1.28-client", "ftsteutates-sensors",
    ]

    def refresh(self):
        pass  # dnf has no separate metadata-refresh step distinct from install/update

    def update(self):
        self._run(["dnf", "update", "-y"])

    def install(self):
        self._run(["dnf", "install", "-y"] + self.packages)


# ── Registry ──────────────────────────────────────────────────────────────────
# opensuse-leap/sles are keyed by (ID, major VERSION_ID) — one explicit branch
# per concrete OS+version, same shape as bash's own os-release-driven
# if/elif. Everything else stays keyed by ID alone (no version-specific
# packages needed there yet); ID_LIKE is checked as a fallback for anything
# not directly listed, same two-tier pattern used by install_postgresql.py's
# OS dispatch.

_BY_ID_VERSION = {
    ("opensuse-leap", "15"): OpenSUSELeap15Profile,
    ("opensuse-leap", "16"): OpenSUSELeap16Profile,
    ("sles", "15"): SLES15Profile,
    ("sles", "16"): SLES16Profile,
}

_BY_ID = {
    "ubuntu": DebianProfile,
    "debian": DebianProfile,
    "linuxmint": DebianProfile,
    "pop": DebianProfile,
    "raspbian": DebianProfile,
    "rhel": RHELProfile,
    "centos": RHELProfile,
    "rocky": RHELProfile,
    "almalinux": RHELProfile,
    "fedora": RHELProfile,
    "ol": RHELProfile,
    "scientific": RHELProfile,
}

# Fallback version for opensuse-leap/sles when VERSION_ID's major version
# isn't one of the explicit branches above (e.g. a future 17, or a VERSION_ID
# lab-in-a-box hasn't been told about yet) — matches bash's own "assume the
# newest known major version" behavior rather than refusing outright.
_SUSE_FAMILY_DEFAULT_MAJOR = "16"


def detect_profile():
    """
    Detect the local host's OS (and, for opensuse-leap/sles, its major
    version) and return an instantiated profile, or None if unrecognised
    (mirrors bash's OS-detection block and its "Unsupported OS" exit path —
    the caller decides how to report that).
    """
    os_info = _read_os_release()
    os_id = os_info.get("ID", "")
    os_like = os_info.get("ID_LIKE", "").lower()
    major = os_info.get("VERSION_ID", "").split(".")[0]

    if os_id in ("opensuse-leap", "sles") or "suse" in os_like:
        family = os_id if os_id in ("opensuse-leap", "sles") else "opensuse-leap"
        cls = _BY_ID_VERSION.get((family, major)) \
            or _BY_ID_VERSION[(family, _SUSE_FAMILY_DEFAULT_MAJOR)]
        return cls(os_info)

    cls = _BY_ID.get(os_id)
    if cls is None:
        if "debian" in os_like:
            cls = DebianProfile
        elif "rhel" in os_like or "fedora" in os_like:
            cls = RHELProfile

    return cls(os_info) if cls else None
