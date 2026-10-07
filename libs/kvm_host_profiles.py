"""
kvm_host_profiles.py: per-OS package and repository setup for KVM hypervisor hosts.

One profile class per supported OS+version, each assigning its own literal lists:
  packages          what lab-in-a-box needs on the hypervisor; installed in one transaction
                    and the install fails if any is missing. Every name is verified by
                    tests/distro/host_packages.sh, which installs the list in that distro's
                    official container image and checks REQUIRED_COMMANDS afterwards. SLES is
                    covered by the Leap entry of the same major version (same package names).
  extra_packages    operator conveniences (editors, sensors, container/Kubernetes CLIs);
                    installed best-effort, each failure reported as a warning.
  unmapped_packages conveniences with no package on this OS; reported as a warning.
  repo_setup        commands that enable the repositories `packages` needs (EPEL/CRB on EL).
  kubectl_package   the extra that provides kubectl at KUBECTL_VERSION; "" where the OS has none, in
                    which case install() installs the official upstream binary of that version.

Network bridging is independent of the OS; see libs/host_network.py.

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
from typing import Dict, List, Optional

# Commands the hypervisor must provide once `packages` is installed (the QEMU binary is
# checked separately: /usr/libexec/qemu-kvm on EL, qemu-system-x86_64 elsewhere).
REQUIRED_COMMANDS = [
    "virsh", "virt-install", "qemu-img", "virt-customize", "virt-ls", "guestmount",
    "guestfish", "fusermount3", "sshfs", "rsync", "nc", "podman", "git", "jq", "curl",
    "ssh", "xorriso", "mkisofs", "openssl", "lsof",
]


# kubectl: the distribution's package where one exists, else the official upstream binary of the same
# version. Both verified by tests/distro/host_packages.sh (`kubectl version --client`).
KUBECTL_VERSION = "1.35.0"


def kubectl_binary_script(version: str = KUBECTL_VERSION) -> str:
    """
    Shell script installing the official upstream kubectl `version` to /usr/local/bin/kubectl,
    checked against the SHA-256 published next to it on dl.k8s.io. Needs curl and sha256sum.
    """
    return (
        "set -e; a=$(uname -m); case $a in x86_64) a=amd64;; aarch64) a=arm64;; esac; "
        "u=https://dl.k8s.io/release/v{v}/bin/linux/$a/kubectl; t=$(mktemp); "
        "curl -fsSLo \"$t\" \"$u\"; "
        "echo \"$(curl -fsSL \"$u.sha256\")  $t\" | sha256sum -c - >/dev/null; "
        "install -m 0755 \"$t\" /usr/local/bin/kubectl; rm -f \"$t\""
    ).format(v=version)


def _read_os_release() -> Dict[str, str]:
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
    """Base class: the package-manager mechanics are filled in per family below."""

    name = "generic"
    packages: List[str] = []
    extra_packages: List[str] = []
    unmapped_packages: List[str] = []
    repo_setup: List[List[str]] = []
    kubectl_package = ""
    verified = True

    def __init__(self, os_info: Dict[str, str]):
        self.os_info = os_info

    def register_repos(self) -> None:
        for cmd in self.repo_setup:
            self._run(cmd)

    def refresh(self) -> None:
        raise NotImplementedError

    def update(self) -> None:
        raise NotImplementedError

    def _install_cmd(self, pkgs: List[str]) -> List[str]:
        raise NotImplementedError

    def install(self) -> List[str]:
        """
        Install `packages` (must succeed), then `extra_packages` best-effort, then the upstream kubectl
        binary where the OS has no kubectl package. Returns what failed among the latter two.
        """
        self._run(self._install_cmd(self.packages))
        failed = []
        if self.extra_packages and not self._try(self._install_cmd(self.extra_packages)):
            failed = [p for p in self.extra_packages if not self._try(self._install_cmd([p]))]
        if not self.kubectl_package and not self._try(["sh", "-c", kubectl_binary_script()]):
            failed.append("kubectl {} (upstream binary)".format(KUBECTL_VERSION))
        return failed

    def configure_dns(self, automation_ip: str, mydomain: str) -> None:
        """
        Point this host's DNS resolution at the automation host by rewriting /etc/resolv.conf with a
        fresh search/nameserver pair. SUSE profiles override this with netconfig. Only called once an
        automation host exists.
        """
        resolv = Path("/etc/resolv.conf")
        try:
            lines = resolv.read_text().splitlines()
        except OSError:
            lines = []
        kept = [l for l in lines if not l.strip().startswith(("nameserver", "search"))]
        new_lines = ["search {}".format(mydomain), "nameserver {}".format(automation_ip)] + kept
        resolv.write_text("\n".join(new_lines) + "\n")

    def _run(self, cmd: List[str]) -> None:
        subprocess.run(cmd, check=True)

    def _try(self, cmd: List[str]) -> bool:
        return subprocess.run(cmd, check=False).returncode == 0


# ── openSUSE Leap / SLES (zypper) ───────────────────────────────────────────

# SLES uses the same package names as the openSUSE Leap release of the same major version.
_SUSE15_REQUIRED = [
    "libvirt-daemon-qemu", "libvirt-client", "qemu-x86", "qemu-tools", "virt-install",
    "guestfs-tools", "libguestfs", "fuse3", "sshfs", "netcat-openbsd", "git-core", "podman",
    "rsync", "xorriso", "mkisofs", "jq", "curl", "openssh-clients", "openssl", "lsof",
]
_SUSE16_REQUIRED = [
    "libvirt-daemon-qemu", "libvirt-client", "qemu-x86", "qemu-tools", "virt-install",
    "guestfs-tools", "libguestfs", "fuse3", "sshfs", "netcat-openbsd", "git-core", "podman",
    "rsync", "xorriso", "mkisofs", "jq", "curl", "openssh-clients", "openssl", "lsof",
]
_SUSE15_KUBECTL = "kubernetes1.35-client"
_SUSE15_EXTRA = [
    "docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can", "kubevirt-virtctl",
    _SUSE15_KUBECTL, "gpgme-devel", "device-mapper-devel", "libbtrfs-devel", "mc",
    "bridge-utils", "tcpdump", "sensors", "ftsteutates-sensors", "gptfdisk",
]
_SUSE16_EXTRA = [
    "docker", "kubevirt-virtctl", "libgpgme-devel", "device-mapper-devel", "libbtrfs-devel", "mc",
    "bridge-utils", "tcpdump", "sensors", "gptfdisk",
]
_SUSE16_UNMAPPED = [
    "minikube-bash-completion", "kubectl-who-can", "ftsteutates-sensors", "cri-tools",
]


class _SuseZypperProfile(HostOSProfile):
    """Shared zypper/netconfig mechanics for every openSUSE Leap/SLES version below."""

    def refresh(self) -> None:
        self._run(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh"])

    def update(self) -> None:
        self._run(["zypper", "--non-interactive", "update", "-y"])

    def _install_cmd(self, pkgs: List[str]) -> List[str]:
        return ["zypper", "--non-interactive", "install", "-y"] + pkgs

    def configure_dns(self, automation_ip: str, mydomain: str) -> None:
        """Set NETCONFIG_DNS_STATIC_SERVERS/_SEARCHLIST and regenerate resolv.conf with netconfig."""
        cfg = Path("/etc/sysconfig/network/config")
        text = cfg.read_text()
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SERVERS=.*$',
                      'NETCONFIG_DNS_STATIC_SERVERS="{}"'.format(automation_ip), text, flags=re.M)
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SEARCHLIST=.*$',
                      'NETCONFIG_DNS_STATIC_SEARCHLIST="{}"'.format(mydomain), text, flags=re.M)
        cfg.write_text(text)
        subprocess.run(["netconfig", "update", "-f"], check=False)


class _SuseRegisteredProfile(_SuseZypperProfile):
    """
    SUSEConnect registration for SLES. regcode/suse_email/suse_url are set by the caller from
    lab.cfg's SUSE_regcode/SUSE_email/SUSE_url after detect_profile() returns.
    """

    _products: tuple = ()
    regcode = ""
    suse_email = ""
    suse_url = ""

    def register_repos(self) -> None:
        # The base product is registered before the modules: adding a module to an unregistered
        # host fails. Registering an already registered host again is a no-op.
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
    packages = list(_SUSE15_REQUIRED)
    extra_packages = list(_SUSE15_EXTRA)
    kubectl_package = _SUSE15_KUBECTL


class OpenSUSELeap16Profile(_SuseZypperProfile):
    name = "opensuse-leap-16"
    packages = list(_SUSE16_REQUIRED)
    extra_packages = list(_SUSE16_EXTRA)
    unmapped_packages = list(_SUSE16_UNMAPPED)


class SLES15Profile(_SuseRegisteredProfile):
    name = "sles-15"
    packages = list(_SUSE15_REQUIRED)
    extra_packages = list(_SUSE15_EXTRA)
    kubectl_package = _SUSE15_KUBECTL
    _products = ("PackageHub", "sle-module-containers", "sle-module-basesystem", "sle-module-legacy")


class SLES16Profile(_SuseRegisteredProfile):
    name = "sles-16"
    packages = list(_SUSE16_REQUIRED)
    extra_packages = list(_SUSE16_EXTRA)
    unmapped_packages = list(_SUSE16_UNMAPPED)
    # SLES 16 includes the containers/basesystem/legacy modules in the base product; sshfs
    # comes from PackageHub.
    _products = ("PackageHub",)


# ── Debian / Ubuntu (apt) ───────────────────────────────────────────────────

_DEB_REQUIRED = [
    "libvirt-daemon-system", "libvirt-clients", "qemu-system-x86", "qemu-utils", "virtinst",
    "guestfs-tools", "libguestfs-tools", "fuse3", "sshfs", "netcat-openbsd", "git", "podman",
    "rsync", "xorriso", "genisoimage", "jq", "curl", "openssh-client", "openssl",
    "bridge-utils", "lsof",
]
_DEB_EXTRA = ["mc", "tcpdump", "lm-sensors", "gdisk", "libgpgme-dev", "libdevmapper-dev", "libbtrfs-dev"]
_DEB_UNMAPPED = ["docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
                 "kubevirt-virtctl", "ftsteutates-sensors"]


class _AptProfile(HostOSProfile):
    _env = ["env", "DEBIAN_FRONTEND=noninteractive"]

    def refresh(self) -> None:
        self._run(["apt-get", "update"])

    def update(self) -> None:
        self._run(self._env + ["apt-get", "upgrade", "-y"])

    def _install_cmd(self, pkgs: List[str]) -> List[str]:
        return self._env + ["apt-get", "install", "-y"] + pkgs


class Debian12Profile(_AptProfile):
    name = "debian-12"
    packages = list(_DEB_REQUIRED)
    extra_packages = list(_DEB_EXTRA)
    unmapped_packages = list(_DEB_UNMAPPED)


class Debian13Profile(_AptProfile):
    name = "debian-13"
    packages = list(_DEB_REQUIRED)
    extra_packages = list(_DEB_EXTRA)
    unmapped_packages = list(_DEB_UNMAPPED)


class Ubuntu2204Profile(_AptProfile):
    name = "ubuntu-22.04"
    packages = list(_DEB_REQUIRED)
    extra_packages = list(_DEB_EXTRA)
    unmapped_packages = list(_DEB_UNMAPPED)


class Ubuntu2404Profile(_AptProfile):
    name = "ubuntu-24.04"
    packages = list(_DEB_REQUIRED)
    extra_packages = list(_DEB_EXTRA)
    unmapped_packages = list(_DEB_UNMAPPED)


# ── RHEL / Rocky / AlmaLinux / CentOS Stream / Fedora (dnf) ─────────────────
# curl is not listed: minimal EL installs ship curl-minimal, which conflicts with curl and
# already provides the command.

_RPM_REQUIRED = [
    "libvirt", "libvirt-client", "qemu-kvm", "qemu-img", "virt-install", "guestfs-tools",
    "libguestfs", "fuse3", "fuse-sshfs", "nmap-ncat", "git-core", "podman", "rsync", "xorriso",
    "genisoimage", "jq", "openssh-clients", "openssl", "lsof",
]
_RPM_EXTRA = ["mc", "tcpdump", "lm_sensors", "gdisk", "gpgme-devel", "device-mapper-devel"]
_RPM_UNMAPPED = ["docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
                 "kubevirt-virtctl", "ftsteutates-sensors"]


def _el_repo_setup(os_id: str, major: str) -> List[List[str]]:
    """EPEL and CodeReady Builder (fuse-sshfs and genisoimage come from EPEL)."""
    if os_id == "rhel":
        return [
            ["subscription-manager", "repos", "--enable",
             "codeready-builder-for-rhel-{}-{}-rpms".format(major, platform.machine())],
            ["dnf", "install", "-y",
             "https://dl.fedoraproject.org/pub/epel/epel-release-latest-{}.noarch.rpm".format(major)],
        ]
    return [
        ["dnf", "install", "-y", "epel-release", "dnf-plugins-core"],
        ["dnf", "config-manager", "--set-enabled", "crb"],
    ]


class _DnfProfile(HostOSProfile):
    def __init__(self, os_info: Dict[str, str]):
        super().__init__(os_info)
        self.repo_setup = self._repo_setup()

    def _repo_setup(self) -> List[List[str]]:
        return []

    def refresh(self) -> None:
        self._run(["dnf", "makecache"])

    def update(self) -> None:
        self._run(["dnf", "update", "-y"])

    def _install_cmd(self, pkgs: List[str]) -> List[str]:
        return ["dnf", "install", "-y"] + pkgs


class EL9Profile(_DnfProfile):
    name = "el-9"
    packages = list(_RPM_REQUIRED)
    extra_packages = list(_RPM_EXTRA)
    unmapped_packages = list(_RPM_UNMAPPED)

    def _repo_setup(self) -> List[List[str]]:
        return _el_repo_setup(self.os_info.get("ID", ""), "9")


class EL10Profile(_DnfProfile):
    name = "el-10"
    packages = list(_RPM_REQUIRED)
    extra_packages = list(_RPM_EXTRA)
    unmapped_packages = list(_RPM_UNMAPPED)

    def _repo_setup(self) -> List[List[str]]:
        return _el_repo_setup(self.os_info.get("ID", ""), "10")


class FedoraProfile(_DnfProfile):
    name = "fedora"
    packages = list(_RPM_REQUIRED)
    extra_packages = list(_RPM_EXTRA)
    unmapped_packages = list(_RPM_UNMAPPED)


# ── Registry ──────────────────────────────────────────────────────────────────
# Keyed by (os-release ID, major VERSION_ID). An unknown version of a known ID, or an OS only
# matched through ID_LIKE, gets the newest profile of its family with verified=False, so the
# caller can warn that the package names were not checked for it.

_BY_ID_VERSION = {
    ("opensuse-leap", "15"): OpenSUSELeap15Profile,
    ("opensuse-leap", "16"): OpenSUSELeap16Profile,
    ("sles", "15"): SLES15Profile,
    ("sles", "16"): SLES16Profile,
    ("debian", "12"): Debian12Profile,
    ("debian", "13"): Debian13Profile,
    ("ubuntu", "22"): Ubuntu2204Profile,
    ("ubuntu", "24"): Ubuntu2404Profile,
    ("rhel", "9"): EL9Profile,
    ("rhel", "10"): EL10Profile,
    ("rocky", "9"): EL9Profile,
    ("rocky", "10"): EL10Profile,
    ("almalinux", "9"): EL9Profile,
    ("almalinux", "10"): EL10Profile,
    ("centos", "9"): EL9Profile,
    ("centos", "10"): EL10Profile,
}

_NEWEST = {
    "opensuse-leap": OpenSUSELeap16Profile,
    "sles": SLES16Profile,
    "debian": Debian13Profile,
    "ubuntu": Ubuntu2404Profile,
    "rhel": EL10Profile,
    "rocky": EL10Profile,
    "almalinux": EL10Profile,
    "centos": EL10Profile,
}


def _family(os_id: str, os_like: str) -> Optional[str]:
    if os_id in _NEWEST or os_id == "fedora":
        return os_id
    if "suse" in os_like:
        return "opensuse-leap"
    if "ubuntu" in os_like:
        return "ubuntu"
    if "debian" in os_like:
        return "debian"
    if "rhel" in os_like or "centos" in os_like:
        return "rhel"
    if "fedora" in os_like:
        return "fedora"
    return None


def detect_profile() -> Optional[HostOSProfile]:
    """Instantiate the profile for this host's /etc/os-release, or None for an unsupported OS."""
    os_info = _read_os_release()
    os_id = os_info.get("ID", "")
    major = os_info.get("VERSION_ID", "").split(".")[0]
    family = _family(os_id, os_info.get("ID_LIKE", "").lower())
    if family is None:
        return None
    if family == "fedora":
        return FedoraProfile(os_info)
    cls = _BY_ID_VERSION.get((family, major)) if family == os_id else None
    profile = (cls or _NEWEST[family])(os_info)
    if cls is None:
        profile.verified = False
    return profile
