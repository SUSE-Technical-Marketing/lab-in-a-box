#!/bin/bash
# Part of lab-in-a-box, it will create the automation VM that orchestrates the creation of the labs
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


if [[ -f lab.cfg ]]; then
    echo "Loading configuration file lab.cfg"
    . lab.cfg
else
    echo -e "\033[1;31mERROR\033[0m: Missing configuration file lab.cfg"
    exit 1
fi

function show_nicer_messages() {
    tput bold
    echo -e "\n###._ ${_msg} _.###\n"
    tput sgr0
}

_SLA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Configuration steps of the automation node, shared with the node itself (see automation-node-lib.sh).
# shellcheck source=automation-node-lib.sh
. "${_SLA_DIR}/automation-node-lib.sh"

# Derive CIDR prefix length from _mynet (e.g. 192.168.8.0/24 → "24")
_mymask_cidr="${_mynet##*/}"

# Auto-detect KVM bridge if _bridge_name not set in lab.cfg
function detect_bridge() {
    [[ -n "${_bridge_name}" ]] && return
    _bridge_name=$(ip -br link show type bridge | awk 'NR==1{print $1}')
    [[ -z "${_bridge_name}" ]] && _bridge_name="br0"
    echo "Using bridge: ${_bridge_name}"
}

# Create _bridge_name and enslave _bridge_nic to it, if _bridge_nic is set in
# lab.cfg (empty = skip entirely, matching detect_bridge()'s existing
# assume-it-already-exists default). Mirrors
# kvm_host_profiles.py's configure_bridge(): nmcli when NetworkManager is
# live, wicked ifcfg files otherwise — which stack is live is a runtime
# question, not an OS-version one, so it's detected here rather than
# hardcoded per OS.
function configure_bridge() {
    [[ -z "${_bridge_nic}" ]] && return
    # setup_kvm_node.py creates the bridge (libs/host_network.py) before running this script.
    [[ -d "/sys/class/net/${_bridge_name}/bridge" ]] && return
    _msg="Configure network bridge ${_bridge_name} (${_bridge_nic})" show_nicer_messages
    if systemctl is-active --quiet NetworkManager; then
        nmcli con add type bridge con-name "${_bridge_name}" ifname "${_bridge_name}"
        nmcli con add type bridge-slave ifname "${_bridge_nic}" master "${_bridge_name}"
        nmcli con up "${_bridge_name}"
    elif systemctl is-active --quiet wickedd; then
        cat > "/etc/sysconfig/network/ifcfg-${_bridge_name}" <<EOF
BOOTPROTO='dhcp'
STARTMODE='auto'
BRIDGE='yes'
BRIDGE_PORTS='${_bridge_nic}'
EOF
        cat > "/etc/sysconfig/network/ifcfg-${_bridge_nic}" <<EOF
BOOTPROTO='none'
STARTMODE='auto'
EOF
        wicked ifreload all
    else
        echo -e "\033[1;31mERROR\033[0m: neither NetworkManager nor wicked is active — cannot configure bridge ${_bridge_name}" >&2
        exit 1
    fi
}

# Derive MAC from last 3 octets of _myip using QEMU OUI (52:54:00)
function generate_mac() {
    [[ -n "${_automation_mac}" ]] && return
    IFS=. read -r _ _oct2 _oct3 _oct4 <<< "${_myip}"
    _automation_mac=$(printf "52:54:00:%02x:%02x:%02x" "$_oct2" "$_oct3" "$_oct4")
}

# Get virt-install --osinfo from _QCOW_IMAGE filename; fall back to nearest supported version
function detect_vm_osinfo() {
    local _vm_ver _fb
    _vm_ver=$(grep -oP '\d+\.\d+' <<< "${_QCOW_IMAGE##*/}" | head -1)
    _vm_osinfo="opensuse${_vm_ver:-15.5}"
    if command -v osinfo-query &>/dev/null; then
        if ! osinfo-query os short-id="${_vm_osinfo}" &>/dev/null 2>&1; then
            # 16.0 added explicitly rather than derived — osinfo-query's own
            # database may simply lack an "opensuse16.0" short-id regardless
            # of whether the OS itself is fine, so this is a real fallback
            # entry, not a guess.
            for _fb in 16.0 15.5 15.4 15.3; do
                if osinfo-query os short-id="opensuse${_fb}" &>/dev/null 2>&1; then
                    _vm_osinfo="opensuse${_fb}"
                    break
                fi
            done
        fi
    fi
}

function configure_image() {
    _msg="Copy image and resize" show_nicer_messages
    cp "${_QCOW_IMAGE}" /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2
    qemu-img resize /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 ${_disk_size:-40}G
    trap 'unmount_image 2>/dev/null' EXIT
    _msg="Mount image for configuration" show_nicer_messages
    guestmount -i --rw -a /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 /mnt/
    # The chroot needs /proc, /sys and /dev for zypper and systemctl.
    for _d in proc sys dev; do
        mount --bind "/${_d}" "/mnt/${_d}"
    done
}









# Prints this host's IPv4 address on the lab network: the bridge's address, or the NAT network's gateway.
function host_lab_ip() {
    if [[ "${_network_mode:-bridge}" == "nat" ]]; then
        echo "${_mygw}"
    else
        ip -4 -br addr show "${_bridge_name}" | awk '{split($3, a, "/"); print a[1]; exit}'
    fi
}



# Sets the node settings this host derives (automation-node-lib.sh's NODE_SETTINGS) for _automation_node.
function derive_node_settings() {
    _node_kind="${_automation_node}"
    _host_ip="$(host_lab_ip)"
    _host_name="$(hostname)"
    _host_fqdn="$(hostname -f)"
    [[ -f /root/.ssh/id_rsa ]] || ssh-keygen -q -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    HOST_SSH_PUB_KEY="$(cat /root/.ssh/id_rsa.pub)"
    # shellcheck disable=SC2034  # read through NODE_SETTINGS by write_node_settings
    [[ -z "${root_pwd}" ]] || ROOT_PWD_HASH="$(openssl passwd -6 -stdin <<< "${root_pwd}")"
    if [[ "${_node_kind}" != vm ]]; then
        _python_bin=python3.13 _node_netstack=none
    elif _lab_host_is_leap16; then
        _python_bin=python3.11 _node_netstack=wicked
    else
        _python_bin=python3.11 _node_netstack=nm
    fi
}

# Copies the tracked files of this lab-in-a-box tree (lab.cfg is not tracked) to directory $1, with .lab-versions:
# the last commit of every file and directory, for install_automation_node_scripts.sh's version stamps. A setup tree
# that is not a git checkout is replaced by a clone of the GitHub repository.
function stage_lab_tree() {
    local _dest="$1" _src _clone=""
    _src="$(cd "${_SLA_DIR}/.." && pwd)"
    if ! git -C "${_src}" rev-parse --is-inside-work-tree &>/dev/null; then
        _clone="$(mktemp -d)"
        git clone -q https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git "${_clone}" || return 1
        _src="${_clone}"
    fi
    mkdir -p "${_dest}"
    ( set -o pipefail
      git -C "${_src}" ls-files -z | tar -C "${_src}" --null --ignore-failed-read -T - -cf - | tar -C "${_dest}" -xf - ) \
        || return 1
    git -C "${_src}" log --format='@%h' --name-only | awk '
        /^@/ { h = substr($0, 2); next }
        NF {
            if (!($0 in seen)) { seen[$0] = 1; print h, $0 }
            n = split($0, part, "/"); dir = part[1]
            for (i = 1; i < n; i++) {
                if (i > 1) dir = dir "/" part[i]
                if (!(dir in seen)) { seen[dir] = 1; print h, dir }
            }
        }' > "${_dest}/.lab-versions"
    [[ -z "${_clone}" ]] || rm -rf "${_clone}"
}

# Writes the node's inputs under directory $1: NODE_DIR with the settings, automation-node-lib.sh and the lab tree.
function stage_node_inputs() {
    local _root="$1"
    write_node_settings "${_root}"
    install -m 0755 "${_SLA_DIR}/automation-node-lib.sh" "${_root}${NODE_DIR}/automation-node-lib.sh"
    stage_lab_tree "${_root}${NODE_DIR}/lab-in-a-box" \
        || { echo -e "\033[1;31mERROR\033[0m: staging lab-in-a-box for the automation node failed" >&2; exit 1; }
}

# Runs a command inside the automation node through _node_transport: chroot (the VM image mounted at /mnt), ssh (the
# running VM), podman (the container) or kubectl (the pod). -i as the first argument passes stdin through.
function node_run() {
    local _i=() _n=(-n)
    [[ "$1" == -i ]] && { _i=(-i); _n=(); shift; }
    case "${_node_transport}" in
        chroot)  chroot /mnt "$@" ;;
        ssh)     ssh "${_n[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@${_myip}" "$(printf '%q ' "$@")" ;;
        podman)  podman exec "${_i[@]}" "${AUTOMATION_HOSTNAME}" "$@" ;;
        kubectl) k8s_kubectl -n "${_k8s_namespace:-lab-automation}" exec "${_i[@]}" "${_AUTOMATION_POD}" -c automation -- "$@" ;;
    esac
}

# Puts the inputs staged under $1 into the node (replacing its lab tree), runs "automation-node-lib.sh configure"
# there ($2: its mode option, if any) and authorizes the node's SSH key on this host.
function configure_node_from() {
    local _stage="$1" _mode="${2:-}" _pub
    _msg="Configure the automation node" show_nicer_messages
    node_run rm -rf "${NODE_DIR}/lab-in-a-box"
    tar -C "${_stage}" -cf - . | node_run -i tar -C / --no-overwrite-dir -xf - \
        || { echo -e "\033[1;31mERROR\033[0m: copying the configuration into the automation node failed" >&2; exit 1; }
    node_run bash "${NODE_DIR}/automation-node-lib.sh" configure ${_mode:+"${_mode}"} \
        || { echo -e "\033[1;31mERROR\033[0m: configuring the automation node failed" >&2; exit 1; }
    _pub="$(node_run cat /root/.ssh/id_rsa.pub)"
    grep -qxF "${_pub}" /root/.ssh/authorized_keys 2>/dev/null || echo "${_pub}" >> /root/.ssh/authorized_keys
    echo -e "\n# Automation node public key:\n${_pub}\n"
}

# Prepares the VM image with guestfish (see _lab_host_is_leap16): its network configuration and the keys this host
# needs to reach it, plus the staged inputs under $1. The VM is configured over SSH once it runs.
function prepare_image_via_guestfish() {
    local _stage="$1" _gf _d
    _msg="Copy image and resize" show_nicer_messages
    cp "${_QCOW_IMAGE}" "/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2"
    qemu-img resize "/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2" "${_disk_size:-40}G"
    _msg="Prepare the image (guestfish)" show_nicer_messages
    write_vm_system "${_stage}"
    write_authorized_keys "${_stage}" "${ROOT_SSH_PUB_KEY}" "${HOST_SSH_PUB_KEY}"
    _gf="$(mktemp)"
    {
        echo 'rm-f /var/lib/YaST2/reconfig_system'
        echo 'sh "systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true"'
        for _d in "${_stage}"/*; do echo "copy-in ${_d} /"; done
    } > "${_gf}"
    guestfish --rw -i -a "/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2" -f "${_gf}"
    local _rc=$?
    rm -f "${_gf}"
    return "${_rc}"
}

# On an openSUSE Leap 16 host guestmount's FUSE layer returns I/O errors for populated directories, so the VM image is
# prepared with guestfish there (prepare_image_via_guestfish) and configured over SSH after its first boot.
function _lab_host_is_leap16() {
    . /etc/os-release 2>/dev/null
    [[ "${ID}" == "opensuse-leap" && "${VERSION_ID}" == 16* ]]
}

# libguestfs on the RHEL family and Fedora cannot read the btrfs root filesystem of the openSUSE Leap image.
function _host_can_prepare_image() {
    . /etc/os-release 2>/dev/null
    [[ " ${ID} ${ID_LIKE} " != *" rhel "* && " ${ID} ${ID_LIKE} " != *" fedora "* ]]
}

function unmount_image() {
    sync
    # Unmount the /proc,/sys,/dev bind mounts configure_image() set up
    # BEFORE guestunmount — guestunmount only knows about the guestmount
    # FUSE mount itself, so these would otherwise stay mounted after /mnt
    # is torn down out from under them.
    for _d in dev sys proc; do
        mountpoint -q "/mnt/${_d}" && umount "/mnt/${_d}"
    done
    # ssh-keygen (configure_ssh(), chrooted into /mnt) can leave an orphaned gpg-agent or scdaemon
    # with its cwd inside /mnt. That keeps the FUSE mount busy, so guestunmount fails ("Device or
    # resource busy") and the following virt-install fails ("Failed to get 'write' lock"). Kill any
    # process still holding /mnt open before the unmount.
    if command -v lsof &>/dev/null; then
        lsof +D /mnt 2>/dev/null | awk 'NR>1{print $2}' | sort -u | xargs -r kill -9
        sleep 1
    fi
    if ! guestunmount /mnt; then
        echo -e "\033[1;31mERROR\033[0m: guestunmount /mnt failed, so the changes to the automation VM image were not written; stopping before creating the VM" >&2
        exit 1
    fi
    trap - EXIT
    # guestunmount returning success only means the FUSE mountpoint is gone. libguestfs's internal
    # helper VM, which backs read/write access to the qcow2, can still hold its lock on the file for a
    # moment. virt-install run immediately after can then fail with "Failed to get 'write' lock".
    # Poll briefly for the lock to release, so the common case is not slowed down.
    local _qcow="/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2"
    if command -v lsof &>/dev/null; then
        for _i in 1 2 3 4 5 6 7 8 9 10; do
            lsof "${_qcow}" &>/dev/null || break
            sleep 1
        done
    fi
}

# "bridge" (default, today's exact unchanged behavior) puts the automation
# VM directly on _bridge_name; "nat" is the extra, opt-in alternative (see
# lab.cfg.template's own comment) that attaches it to the libvirt NAT'd
# virtual network setup_kvm_node.py's configure_nat_network() already
# defined instead — same --network flag shape virt-install already expects
# for a named libvirt network (network=<name> instead of bridge=<name>).
function vm_network_arg() {
    if [[ "${_network_mode:-bridge}" == "nat" ]]; then
        echo "network=${_nat_network_name:-labnat},mac.address=${_automation_mac}"
    else
        echo "bridge=${_bridge_name},mac.address=${_automation_mac}"
    fi
}

function create_vm() {
    _msg="Create virtual machine" show_nicer_messages
    # lab.cfg's _automation_graphics (default spice). When it is spice and this host's QEMU lists
    # graphics types without spice (EL 10, SLES 16, some minimal openSUSE installs), vnc is used
    # instead, the same check libs/backends.py's LibvirtBackend._graphics() makes for lab VMs.
    local _graphics="${_automation_graphics:-spice}"
    if [[ "${_graphics}" == spice ]]; then
        local _caps
        _caps=$(virsh --connect "${_qemu_addr}" domcapabilities 2>/dev/null)
        if [[ "${_caps}" == *"<graphics supported='yes'>"* && "${_caps}" != *"<value>spice</value>"* ]]; then
            _graphics=vnc
        fi
    fi
    virt-install --connect ${_qemu_addr} \
        --name "${AUTOMATION_HOSTNAME}" \
        --autostart \
        --vcpus 1 \
        --memory 2048 \
        --osinfo="${_vm_osinfo}" \
        --import \
        --disk "size=${_disk_size:-40},path=/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2,sparse=no,boot.order=1" \
        --graphics="${_graphics}" \
        --network "$(vm_network_arg)" \
        --noautoconsole
}

# Only relevant when _network_mode=nat: forwards the automation node's ports from the KVM host's IP with
# libs/portforward.py's apply_forwarded_ports(), run with LAB_PYTHON (the interpreter setup_kvm_node.py runs with).

function configure_nat_port_forwarding() {
    [[ "${_network_mode:-bridge}" != "nat" ]] && return
    _msg="Forward automation VM ports from the KVM host" show_nicer_messages
    "${LAB_PYTHON:-python3}" -c "
import sys
sys.path.insert(0, '${_SLA_DIR}/../libs')
import portforward
portforward.apply_forwarded_ports({'${_myip}': '${_nat_forwarded_ports:-22:22/TCP 80:80/TCP 443:443/TCP}'.split()})
"
}

function wait_for_vm() {
    _msg="Waiting for ${AUTOMATION_HOSTNAME} to come online" show_nicer_messages
    local _count=0
    while ! nc -z -w 2 "${_myip}" 22 &>/dev/null; do
        ((_count++))
        if [[ $_count -gt 60 ]]; then
            echo -e "\033[1;31mERROR\033[0m: Timeout waiting for ${AUTOMATION_HOSTNAME} (${_myip})"
            exit 1
        fi
        sleep 5
    done
    echo "${AUTOMATION_HOSTNAME} is online"
}

function configure_host_dns() {
    _msg="Reconfigure host to use new VM as DNS server" show_nicer_messages
    # netconfig (SUSE) generates /etc/resolv.conf from /etc/sysconfig/network/config; other hosts get the lines
    # added to /etc/resolv.conf itself.
    if [[ -f /etc/sysconfig/network/config ]] && command -v netconfig &>/dev/null; then
        sed "s/NETCONFIG_DNS_STATIC_SERVERS=.*/NETCONFIG_DNS_STATIC_SERVERS=\"${_myip} ${_mydns}\"/;s/NETCONFIG_DNS_STATIC_SEARCHLIST=.*/NETCONFIG_DNS_STATIC_SEARCHLIST=\"${_mydomain}\"/" \
            -i /etc/sysconfig/network/config
        netconfig update -f
    elif grep -qi "^search " /etc/resolv.conf; then
        sed --follow-symlinks -i "/^search /a nameserver ${_myip}" /etc/resolv.conf
    else
        sed --follow-symlinks -i "1i search ${_mydomain}\nnameserver ${_myip}" /etc/resolv.conf
    fi
}


# ── Automation node as a container or on Kubernetes ───────────────────────
#
# _automation_node=container: a privileged openSUSE Leap container with systemd as init, built from
# automation-node.Containerfile. It has no podman network: lab-automation.service connects it to the lab bridge with
# a veth pair, so it gets _myip, _automation_mac and the default route _mygw, like the automation VM.
# _automation_node=kubernetes: the same image as a StatefulSet in the cluster _k8s_kubeconfig points to, reachable at
# _myip through a LoadBalancer Service (_k8s_network=loadbalancer, default; spec.loadBalancerIP, which MetalLB,
# kube-vip and most cloud load balancers honour) or a Multus macvlan interface on the lab
# network (_k8s_network=macvlan, interface _k8s_macvlan_master).
# Both keep the node's configuration and user data on persistent volumes; setup never removes them.

_AUTOMATION_IMAGE="localhost/lab-automation-node:latest"
_AUTOMATION_POD="lab-automation-0"

# Persistent data of the automation node, "<volume name>:<path>" per entry.
_AUTOMATION_DATA="etc-lab-creation:/etc/lab_creation etc-lab-builder:/etc/lab-builder etc-lab-mcp:/etc/lab-mcp
etc-ssh:/etc/ssh named:/var/lib/named root:/root provisioning:/srv/www/htdocs/lab_creation helm:/srv/www/htdocs/helm"

function _automation_is_container() {
    [[ "${_automation_node}" == container ]]
}

function _automation_is_kubernetes() {
    [[ "${_automation_node}" == kubernetes ]]
}

# Runs kubectl against _k8s_kubeconfig (empty: kubectl's default).
function k8s_kubectl() {
    kubectl ${_k8s_kubeconfig:+--kubeconfig "${_k8s_kubeconfig}"} "$@"
}


# Prints the bridge the automation node attaches to: _bridge_name, or the libvirt NAT network's bridge with
# _network_mode=nat.
function container_bridge() {
    if [[ "${_network_mode:-bridge}" == "nat" ]]; then
        virsh -c "${_qemu_addr}" net-info "${_nat_network_name:-labnat}" | awk '/^Bridge:/{print $2}'
    else
        echo "${_bridge_name}"
    fi
}

# Builds the automation node image. LAB_KUBECTL_INSTALL: the kubectl install script setup_kvm_node.py passes
# (libs/kvm_host_profiles.py's kubectl_binary_script()).
function build_automation_image() {
    _msg="Build the automation node container image" show_nicer_messages
    if [[ -z "${LAB_KUBECTL_INSTALL}" ]]; then
        echo -e "\033[1;31mERROR\033[0m: LAB_KUBECTL_INSTALL is not set; run this through setup_kvm_node.py" >&2
        exit 1
    fi
    podman build --pull=newer --build-arg KUBECTL_INSTALL="${LAB_KUBECTL_INSTALL}" -t "${_AUTOMATION_IMAGE}" \
        -f "${_SLA_DIR}/automation-node.Containerfile" "${_SLA_DIR}" \
        || { echo -e "\033[1;31mERROR\033[0m: building the automation node image failed" >&2; exit 1; }
}

# Writes /usr/local/sbin/lab-automation-net and lab-automation.service, which starts the container at boot and
# connects it to the bridge.
function install_automation_unit() {
    _msg="Install lab-automation.service" show_nicer_messages
    local _bridge
    _bridge="$(container_bridge)"
    [[ -n "${_bridge}" ]] || { echo -e "\033[1;31mERROR\033[0m: no bridge found for the automation node" >&2; exit 1; }
    cat > /usr/local/sbin/lab-automation-net <<'EOF'
#!/bin/bash
# Usage: lab-automation-net CONTAINER BRIDGE IP/PREFIX MAC GATEWAY
# Connects the running CONTAINER to BRIDGE through the veth pair labauto0 (host side) / eth0 (container side).
set -e
_pid=$(podman inspect -f '{{.State.Pid}}' "$1")
ip link del labauto0 2>/dev/null || true
ip link add labauto0 type veth peer name labauto0c
ip link set labauto0 master "$2" up
ip link set labauto0c netns "${_pid}"
nsenter -t "${_pid}" -n ip link set lo up
nsenter -t "${_pid}" -n ip link set labauto0c name eth0
nsenter -t "${_pid}" -n ip link set eth0 address "$4" up
nsenter -t "${_pid}" -n ip addr add "$3" dev eth0
nsenter -t "${_pid}" -n ip route add default via "$5"
EOF
    chmod 0755 /usr/local/sbin/lab-automation-net
    cat > /etc/systemd/system/lab-automation.service <<EOF
[Unit]
Description=lab-in-a-box automation node container (${AUTOMATION_HOSTNAME})
Wants=network-online.target
After=network-online.target libvirtd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/podman start ${AUTOMATION_HOSTNAME}
ExecStart=/usr/local/sbin/lab-automation-net ${AUTOMATION_HOSTNAME} ${_bridge} ${_myip}/${_mymask_cidr} ${_automation_mac} ${_mygw}
ExecStop=/usr/bin/podman stop -t 30 ${AUTOMATION_HOSTNAME}
ExecStopPost=-ip link del labauto0

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

# Removes the automation node container and its unit, if present. Its volumes are kept.
function remove_automation_container() {
    if systemctl cat lab-automation.service &>/dev/null; then
        systemctl disable --now lab-automation.service
    fi
    podman rm -f --ignore "${AUTOMATION_HOSTNAME}" >/dev/null
}

# Creates the container with one named volume (lab-automation-<name>) per _AUTOMATION_DATA entry; podman fills a new,
# empty volume from the image's files at that path.
function create_automation_container() {
    _msg="Create the automation node container" show_nicer_messages
    local _vols=() _d
    for _d in ${_AUTOMATION_DATA}; do
        _vols+=(-v "lab-automation-${_d%%:*}:${_d#*:}")
    done
    podman create --name "${AUTOMATION_HOSTNAME}" --hostname "${AUTOMATION_HOSTNAME}" --privileged \
        --network none --systemd always "${_vols[@]}" "${_AUTOMATION_IMAGE}" >/dev/null \
        || { echo -e "\033[1;31mERROR\033[0m: creating the automation node container failed" >&2; exit 1; }
    systemctl enable --now lab-automation.service \
        || { echo -e "\033[1;31mERROR\033[0m: lab-automation.service failed to start" >&2; exit 1; }
}

# Prints the Kubernetes manifests of the automation node: namespace, StatefulSet with one PersistentVolumeClaim
# (each _AUTOMATION_DATA path a subPath of it, filled from the image by the seed init container while empty), and the
# LoadBalancer Service at _myip or, with _k8s_network=macvlan, the Multus NetworkAttachmentDefinition.
function k8s_manifests() {
    local _ns="${_k8s_namespace:-lab-automation}" _image="${_automation_image:-ghcr.io/rmahique/lab-automation-node:latest}"
    local _host="${AUTOMATION_HOSTNAME%%.*}" _mounts="" _d _annotations="{}"
    for _d in ${_AUTOMATION_DATA}; do
        _mounts+="
            - {name: data, mountPath: ${_d#*:}, subPath: ${_d%%:*}}"
    done
    if [[ "${_k8s_network:-loadbalancer}" == macvlan ]]; then
        _annotations="{k8s.v1.cni.cncf.io/networks: '[{\"name\": \"lab-automation-lan\", \"ips\": [\"${_myip}/${_mymask_cidr}\"], \"mac\": \"${_automation_mac}\"}]'}"
    fi
    cat <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${_ns}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: lab-automation
  namespace: ${_ns}
spec:
  serviceName: lab-automation
  replicas: 1
  selector:
    matchLabels: {app: lab-automation}
  template:
    metadata:
      labels: {app: lab-automation}
      annotations: ${_annotations}
    spec:
      hostname: ${_host}
      dnsPolicy: None
      dnsConfig:
        nameservers: [127.0.0.1, ${_mydns}]
        searches: [${_mydomain}]
      terminationGracePeriodSeconds: 60
      initContainers:
        - name: seed
          image: ${_image}
          imagePullPolicy: IfNotPresent
          env:
            - {name: DATA, value: "${_AUTOMATION_DATA//$'\n'/ }"}
          command:
            - /bin/sh
            - -c
            - |
              for e in \$DATA; do
                  n=\${e%%:*} p=\${e#*:}
                  if [ -z "\$(ls -A "/data/\$n" 2>/dev/null)" ]; then
                      mkdir -p "/data/\$n" && cp -a "\$p/." "/data/\$n/"
                  fi
              done
          volumeMounts:
            - {name: data, mountPath: /data}
      containers:
        - name: automation
          image: ${_image}
          imagePullPolicy: IfNotPresent
          securityContext: {privileged: true}
          env:
            - {name: container, value: oci}
          lifecycle:
            preStop:
              exec: {command: [/bin/sh, -c, "kill -s RTMIN+3 1"]}
          volumeMounts:
            - {name: run, mountPath: /run}
            - {name: tmp, mountPath: /tmp}${_mounts}
      volumes:
        - {name: run, emptyDir: {medium: Memory}}
        - {name: tmp, emptyDir: {}}
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
${_k8s_storage_class:+        storageClassName: ${_k8s_storage_class}
}        resources:
          requests: {storage: ${_k8s_volume_size:-20Gi}}
EOF
    if [[ "${_k8s_network:-loadbalancer}" == macvlan ]]; then
        cat <<EOF
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: lab-automation-lan
  namespace: ${_ns}
spec:
  config: '{"cniVersion": "0.3.1", "type": "macvlan", "master": "${_k8s_macvlan_master}", "mode": "bridge", "capabilities": {"ips": true, "mac": true}, "ipam": {"type": "static"}}'
EOF
    else
        cat <<EOF
---
apiVersion: v1
kind: Service
metadata:
  name: lab-automation
  namespace: ${_ns}
spec:
  type: LoadBalancer
  loadBalancerIP: ${_myip}
  selector: {app: lab-automation}
  ports:
    - {name: dns-udp, port: 53, protocol: UDP}
    - {name: dns-tcp, port: 53, protocol: TCP}
    - {name: ssh, port: 22, protocol: TCP}
    - {name: http, port: 80, protocol: TCP}
    - {name: https, port: 443, protocol: TCP}
EOF
    fi
}

# Applies the manifests and waits until the pod runs. kubectl apply only changes what differs, so a re-run keeps the
# pod and its volume.
function deploy_automation_k8s() {
    _msg="Deploy the automation node to Kubernetes" show_nicer_messages
    if [[ "${_k8s_network:-loadbalancer}" == macvlan && -z "${_k8s_macvlan_master}" ]]; then
        echo -e "\033[1;31mERROR\033[0m: _k8s_network=macvlan needs _k8s_macvlan_master (the cluster nodes' lab network interface)" >&2
        exit 1
    fi
    k8s_manifests | k8s_kubectl apply -f - \
        || { echo -e "\033[1;31mERROR\033[0m: kubectl apply of the automation node failed" >&2; exit 1; }
    k8s_kubectl -n "${_k8s_namespace:-lab-automation}" rollout status statefulset/lab-automation --timeout=15m \
        || { echo -e "\033[1;31mERROR\033[0m: the automation node pod did not become ready" >&2; exit 1; }
}


function remove_automation_vm() {
    if virsh -c "${_qemu_addr}" desc "${AUTOMATION_HOSTNAME}" &>/dev/null; then
        virsh -c "${_qemu_addr}" destroy  "${AUTOMATION_HOSTNAME}" 2>/dev/null
        virsh -c "${_qemu_addr}" undefine "${AUTOMATION_HOSTNAME}" --remove-all-storage
    fi
}


# --- Main ---

# LAB_AUTOMATION_NODE (vm, container or kubernetes) overrides lab.cfg's _automation_node; default vm.
_automation_node="${LAB_AUTOMATION_NODE:-${_automation_node:-vm}}"
case "${_automation_node}" in
    vm|container|kubernetes) ;;
    *) echo -e "\033[1;31mERROR\033[0m: _automation_node must be vm, container or kubernetes, not '${_automation_node}'" >&2; exit 1 ;;
esac

if [[ "${_automation_node}" == vm ]] && ! _host_can_prepare_image; then
    echo -e "\033[1;31mERROR\033[0m: the automation VM cannot be built on ${PRETTY_NAME:-this host}: libguestfs on the RHEL family and Fedora cannot read the btrfs filesystem of the openSUSE Leap automation image. Use the container automation node instead (setup_kvm_node.py --automation-node container)." >&2
    exit 1
fi

_msg="Delete automation node \"${AUTOMATION_HOSTNAME}\" if it exists" show_nicer_messages
remove_automation_vm
remove_automation_container

detect_bridge
configure_bridge
generate_mac
derive_node_settings
_stage="$(mktemp -d)"
stage_node_inputs "${_stage}"

case "${_automation_node}" in
    container)
        build_automation_image
        install_automation_unit
        create_automation_container
        wait_for_vm
        _node_transport=podman configure_node_from "${_stage}"
        ;;
    kubernetes)
        deploy_automation_k8s
        wait_for_vm
        _node_transport=kubectl configure_node_from "${_stage}"
        ;;
    vm)
        detect_vm_osinfo
        if _lab_host_is_leap16; then
            prepare_image_via_guestfish "${_stage}" \
                || { echo -e "\033[1;31mERROR\033[0m: preparing the automation VM image failed" >&2; exit 1; }
            create_vm
            wait_for_vm
            _node_transport=ssh configure_node_from "${_stage}"
        else
            configure_image
            rm -f /mnt/var/lib/YaST2/reconfig_system
            _node_transport=chroot configure_node_from "${_stage}" --offline
            unmount_image
            create_vm
            wait_for_vm
        fi
        ;;
esac
rm -rf "${_stage}"
configure_nat_port_forwarding
configure_host_dns
