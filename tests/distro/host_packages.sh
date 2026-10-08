#!/bin/bash
# Verifies libs/kvm_host_profiles.py against real distributions: for each supported OS+version it
# runs the profile's repo_setup and installs its `packages` in that distribution's official
# container image, then checks every REQUIRED_COMMANDS entry and a QEMU binary are present, and that
# kubectl (the profile's kubectl_package, else the upstream binary) reports KUBECTL_VERSION. It also
# feeds libs/host_network.py's netplan and ifupdown output to the real netplan / ifup tools.
#
# Not part of tests/run_tests.sh: it pulls ~13 images and installs a libvirt/QEMU stack in each
# (network access and ~1.5 GB of free space in podman's storage needed). Runs one image at a time
# and removes each image afterwards unless it was already present.
#
# Usage: tests/distro/host_packages.sh [NAME...]     (default: every entry of the matrix below)
# SLES needs a registered system for its full repositories, so SLES 15/16 are only covered by the
# matching openSUSE Leap entries here.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_py=$(command -v python3.11 || command -v python3)

# name  image  profile-class  os-release ID
_matrix="
leap15   registry.opensuse.org/opensuse/leap:15.6    OpenSUSELeap15Profile opensuse-leap
leap16   registry.opensuse.org/opensuse/leap:16.0    OpenSUSELeap16Profile opensuse-leap
debian12 docker.io/library/debian:12                 Debian12Profile       debian
debian13 docker.io/library/debian:13                 Debian13Profile       debian
ubuntu22 docker.io/library/ubuntu:22.04              Ubuntu2204Profile     ubuntu
ubuntu24 docker.io/library/ubuntu:24.04              Ubuntu2404Profile     ubuntu
rocky9   docker.io/rockylinux/rockylinux:9           EL9Profile            rocky
rocky10  docker.io/rockylinux/rockylinux:10          EL10Profile           rocky
alma9    docker.io/library/almalinux:9               EL9Profile            almalinux
alma10   docker.io/library/almalinux:10              EL10Profile           almalinux
fedora   registry.fedoraproject.org/fedora:latest    FedoraProfile         fedora
"

# Shell commands (one per line) for a profile: its repo_setup, then installing its packages
# without recommends/weak deps (enough to prove every name resolves and provides the commands).
_commands() {
    "$_py" - "$1" "$2" <<'EOF'
import shlex, sys
sys.path.insert(0, "libs")
import kvm_host_profiles as k
p = getattr(k, sys.argv[1])({"ID": sys.argv[2]})
for cmd in p.repo_setup:
    print(" ".join(shlex.quote(c) for c in cmd))
pkgs = " ".join(shlex.quote(x) for x in p.packages)
if isinstance(p, k._SuseZypperProfile):
    print("zypper --non-interactive --gpg-auto-import-keys install -y --no-recommends " + pkgs)
elif isinstance(p, k._AptProfile):
    print("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends " + pkgs)
else:
    print("dnf install -y -q --setopt=install_weak_deps=False " + pkgs)
install = {"zypper": "zypper --non-interactive install -y --no-recommends",
           "apt": "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends",
           "dnf": "dnf install -y -q --setopt=install_weak_deps=False"}
pm = "zypper" if isinstance(p, k._SuseZypperProfile) else "apt" if isinstance(p, k._AptProfile) else "dnf"
if p.kubectl_package:
    get = "{} {}".format(install[pm], p.kubectl_package)
else:
    get = "sh -c " + shlex.quote(k.kubectl_binary_script())
want = "v" + k.KUBECTL_VERSION
print("{} && kubectl version --client 2>/dev/null | grep -qF '{}' || {{ echo 'kubectl from {} is not {}'; exit 1; }}".format(
    get, want, p.kubectl_package or "the upstream binary", want))
print("echo REQUIRED " + " ".join(k.REQUIRED_COMMANDS))
EOF
}

_check='
set -e
eval "$(cat /cmds)" >/tmp/install.log 2>&1 || { echo "INSTALL FAILED:"; tail -15 /tmp/install.log; exit 1; }
missing=""
for c in $(grep "^REQUIRED " /tmp/install.log | cut -d" " -f2-); do command -v "$c" >/dev/null || missing="$missing $c"; done
command -v qemu-system-x86_64 >/dev/null || [ -x /usr/libexec/qemu-kvm ] || missing="$missing qemu"
[ -z "$missing" ] || { echo "MISSING COMMANDS:$missing"; exit 1; }
'

_status=0
_free_mb() { df --output=avail -B1M "$(podman info -f '{{.Store.GraphRoot}}')" | tail -1; }
while read -r name image cls os_id; do
    [[ -z $name ]] && continue
    [[ $# -gt 0 && " $* " != *" $name "* ]] && continue
    if (( $(_free_mb) < 1500 )); then echo "$name: SKIPPED (less than 1.5 GB free)"; _status=1; continue; fi
    had=0; podman image exists "$image" && had=1
    cmds=$(mktemp)
    _commands "$cls" "$os_id" >"$cmds"
    if out=$(podman run --rm -v "$cmds:/cmds:ro" "$image" bash -c "$_check" 2>&1); then
        echo "$name: OK"
    else
        echo "$name: FAILED"; echo "$out" | sed 's/^/    /'; _status=1
    fi
    rm -f "$cmds"
    (( had )) || podman rmi -f "$image" >/dev/null 2>&1
done <<<"$_matrix"

# netplan: the merged-file conversion host_network.make_bridge() performs, checked by `netplan generate`.
if [[ $# -eq 0 || " $* " == *" netplan "* ]]; then
    plan=$("$_py" -c '
import shlex, sys; sys.path.insert(0, "libs")
import host_network as hn
net = hn.HostNet(nic="eth0", mac="52:54:00:12:34:56", address="192.168.8.20/24", gateway="192.168.8.1",
                 dhcp=False, dns=["192.168.8.1"], search=["mydemo.lab"])
for c in hn.netplan_commands(net, "br0"):
    print(" ".join(shlex.quote(x) for x in c))')
    if out=$(podman run --rm docker.io/library/ubuntu:24.04 bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive; apt-get -qq update >/dev/null; apt-get -qq install -y netplan.io >/dev/null
mkdir -p /etc/netplan
printf "network:\n  version: 2\n  ethernets:\n    eth0:\n      addresses: [192.168.8.20/24]\n      routes: [{to: default, via: 192.168.8.1}]\n    eth1:\n      dhcp4: true\n" >/etc/netplan/50-cloud-init.yaml
chmod 600 /etc/netplan/*.yaml
netplan get >/tmp/m.yaml; rm /etc/netplan/*.yaml; install -m600 /tmp/m.yaml /etc/netplan/90-lab-in-a-box.yaml
'"$plan"'
netplan generate 2>&1 | grep -iE "error|warn" && exit 1
grep -q "Bridge=br0" /run/systemd/network/10-netplan-eth0.network
! grep -q "Address=" /run/systemd/network/10-netplan-eth0.network
grep -q "Address=192.168.8.20/24" /run/systemd/network/10-netplan-br0.network
test -f /run/systemd/network/10-netplan-eth1.network' 2>&1); then
        echo "netplan: OK"
    else
        echo "netplan: FAILED"; echo "$out" | sed 's/^/    /'; _status=1
    fi
fi

# ifupdown: the rewritten interfaces file must parse and bring up br0 with eth0 as its port.
if [[ $# -eq 0 || " $* " == *" ifupdown "* ]]; then
    "$_py" -c '
import sys; sys.path.insert(0, "libs")
import host_network as hn
net = hn.HostNet(nic="eth0", mac="52:54:00:12:34:56", address="192.168.8.20/24", gateway="192.168.8.1",
                 dhcp=False, dns=["192.168.8.1"])
src = "source /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\nallow-hotplug eth0\niface eth0 inet static\n    address 192.168.8.20/24\n    gateway 192.168.8.1\n"
sys.stdout.write(hn.render_ifupdown(src, net, "br0"))' >"${TMPDIR:-/tmp}/lab-interfaces"
    if out=$(podman run --rm -v "${TMPDIR:-/tmp}/lab-interfaces:/tmp/interfaces:ro" docker.io/library/debian:12 bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive; apt-get -qq update >/dev/null; apt-get -qq install -y ifupdown bridge-utils >/dev/null
ifquery -i /tmp/interfaces br0 | grep -q "bridge_ports: eth0"
ifquery -i /tmp/interfaces br0 | grep -q "address: 192.168.8.20$"
ifquery -i /tmp/interfaces eth0 >/dev/null
ifup -n -i /tmp/interfaces br0 >/dev/null 2>&1' 2>&1); then
        echo "ifupdown: OK"
    else
        echo "ifupdown: FAILED"; echo "$out" | sed 's/^/    /'; _status=1
    fi
    rm -f "${TMPDIR:-/tmp}/lab-interfaces"
fi

exit $_status
