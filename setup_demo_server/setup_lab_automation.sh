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
    # guestmount only mounts the guest's own filesystem — it does NOT bind
    # /proc, /sys, /dev the way a real chroot jail needs. Without /proc,
    # `chroot /mnt/ zypper install NetworkManager`
    # (install_packages() below) fails outright ("Warning: No repositories
    # defined. Operating only with the installed resolvables." / "Failed to
    # disable: Input/output error." on the systemctl calls) — NetworkManager
    # never gets installed, wicked stays the active network stack, and the
    # static IP .nmconnection file this whole flow writes (configure_os())
    # never takes effect: the automation VM boots to a login prompt with NO
    # IP on eth0 at all, silently, no error anywhere in this script's own
    # log. This was masked for a long time by two OTHER, since-fixed issues
    # (spice graphics failing outright, then --graphics=none itself causing
    # a boot-time busy-loop) that both stopped the VM from ever reaching
    # this point in the first place.
    for _d in proc sys dev; do
        mount --bind "/${_d}" "/mnt/${_d}"
    done
}

function configure_os() {
    rm /mnt/var/lib/YaST2/reconfig_system
    cp /etc/resolv.conf /mnt/etc/

    echo "${AUTOMATION_HOSTNAME}" > /mnt/etc/hostname

    umask 077
    mkdir -p /mnt/etc/NetworkManager/system-connections/
    cat > /mnt/etc/NetworkManager/system-connections/static.nmconnection <<EOF
[connection]
id=static
type=ethernet
autoconnect=true

[ipv4]
method=manual
dns-search=${_mydomain}
dns=${_myip};${_mydns}
address1=${_myip}/${_mymask_cidr}
gateway=${_mygw}
EOF
    umask 022

    echo "KEYMAP=us" >> /mnt/etc/vconsole.conf
    ln -sf "/usr/share/zoneinfo/${_timezone:-Europe/Zurich}" /mnt/etc/localtime
}

function install_packages() {
    _msg="Install required packages" show_nicer_messages
    # python311: install_automation_node_scripts.sh needs python3.11. kubernetes1.35-client: same kubectl
    # as the hypervisors (libs/kvm_host_profiles.py's KUBECTL_VERSION). Nothing that pulls in a kernel:
    # installing one here rebuilds the initrd inside the chroot, and the VM then cannot find its root disk.
    chroot /mnt/ zypper install -y vim-small git rsync apache2 bind-utils bind docker podman \
        libvirt-client jq NetworkManager virt-install salt-ssh ipcalc fuse3 sshfs netcat-openbsd \
        python311 kubernetes1.35-client \
        || { echo -e "\033[1;31mERROR\033[0m: package installation in the automation VM image failed" >&2; exit 1; }
    _msg="Enable/Disable services" show_nicer_messages
    chroot /mnt/ bash -c "
        systemctl disable firewalld.service wicked.service
        systemctl enable sshd.service NetworkManager.service named apache2
        systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true
    "
}

function configure_ssh() {
    _msg="Generate SSH key" show_nicer_messages
    chroot /mnt/ ssh-keygen -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    cp /mnt/root/.ssh/id_rsa.pub /mnt/srv/www/htdocs/ && chmod 0644 /mnt/srv/www/htdocs/id_rsa.pub
    _msg="Setup SSH keys" show_nicer_messages
    cat /mnt/root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys
    echo -e "\n# Automation VM public key:\n$(cat /mnt/root/.ssh/id_rsa.pub)\n"
    echo "${ROOT_SSH_PUB_KEY}" >> /mnt/root/.ssh/authorized_keys
    echo "root:${root_pwd}" | chroot /mnt/ chpasswd -c SHA512
}

function install_lab_scripts() {
    _msg="Clone repository" show_nicer_messages
    # Clone to a real (non-FUSE) path first, then copy in. Cloning directly onto /mnt (guestmount's
    # FUSE mount) fails on openSUSE Leap 16.0 with "update_ref failed ... trying to write ref
    # 'refs/heads/main' with nonexistent object <sha>". The same clone to a normal path succeeds, so
    # the failure is a git/FUSE ref-write incompatibility.
    rm -rf /tmp/lab-in-a-box-clone
    git clone https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git /tmp/lab-in-a-box-clone
    rm -rf /mnt/var/tmp/lab-in-a-box
    cp -a /tmp/lab-in-a-box-clone /mnt/var/tmp/lab-in-a-box
    rm -rf /tmp/lab-in-a-box-clone
    export _scripts_path=/var/tmp/lab-in-a-box/
    _msg="Run install_automation_node_scripts.sh" show_nicer_messages
    # install_automation_node_scripts.sh uses paths relative to its own repo root
    # (templates/addons/*, scripts/install_*, ...), so it must run with that directory as cwd.
    # Otherwise every relative copy fails with "cannot stat", which is non-fatal and silently skips
    # the addon templates and scripts.
    chroot /mnt/ bash -c "cd /var/tmp/lab-in-a-box && bash install_automation_node_scripts.sh" \
        || { echo -e "\033[1;31mERROR\033[0m: install_automation_node_scripts.sh failed in the automation VM image" >&2; exit 1; }
}

function configure_helm() {
    _msg="Download latest helm and install it" show_nicer_messages
    mkdir -p /mnt/srv/www/htdocs/helm /mnt/srv/www/sources
    chmod 0755 /mnt/srv/www/htdocs/helm /mnt/srv/www/sources
    curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS \
        --output /mnt/srv/www/htdocs/helm/KEYS
    chmod 0644 /mnt/srv/www/htdocs/helm/KEYS
    curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail \
        'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-${myarch:-amd64}.tar.gz" \
        --output /mnt/srv/www/htdocs/helm/helm-latest-linux-${myarch:-amd64}.tar.gz

    # download_latest_helm.sh: arch embedded now, helm version resolved at runtime
    cat > /mnt/usr/local/bin/download_latest_helm.sh << 'HELMSCRIPT'
#!/bin/bash
curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail 'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-MYARCH.tar.gz" \
    --output /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output /srv/www/htdocs/helm/KEYS
chmod 0644 /srv/www/htdocs/helm/KEYS /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
HELMSCRIPT
    sed -i "s/MYARCH/${myarch:-amd64}/g" /mnt/usr/local/bin/download_latest_helm.sh

    # install_helm.sh: served by the automation VM, run on client nodes
    cat > /mnt/srv/www/htdocs/helm/install_helm.sh << HELMSCRIPT
#!/bin/bash
[ -d /tmp/helm ] || mkdir /tmp/helm
curl -SsL "http://${AUTOMATION_HOSTNAME}/helm/helm-latest-linux-${myarch:-amd64}.tar.gz" -o /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz
tar xf /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz -C /tmp/helm
cp /tmp/helm/linux-${myarch:-amd64}/helm /usr/local/bin
HELMSCRIPT

    chmod 0755 /mnt/usr/local/bin/download_latest_helm.sh /mnt/srv/www/htdocs/helm/install_helm.sh
}

function configure_sshfs() {
    echo "${_virt_srv:-root@hypervisor}:/var/lib/libvirt/images/sources /srv/www/htdocs/sources fuse.sshfs  noauto,x-systemd.automount,_netdev,reconnect,identityfile=/root/.ssh/id_rsa,allow_other,default_permissions 0 0" >> /mnt/etc/fstab
}

function configure_dns_server() {
    _msg="Configure DNS server" show_nicer_messages
    cat > /mnt/etc/named.conf <<EOF
options {
        directory "/var/lib/named";
        managed-keys-directory "/var/lib/named/dyn/";
        dump-file "/var/log/named_dump.db";
        statistics-file "/var/log/named.stats";
        listen-on port 53 { any; };
        listen-on-v6 { any; };
        allow-query { 127.0.0.1; 0.0.0.0/0; };
        recursion yes;
        dnssec-validation no;
        forward only;
        forwarders {
            ${_mydns};
        };
};
zone "." in {
        type hint;
        file "root.hint";
};
zone "localhost" in {
        type master;
        file "localhost.zone";
};
zone "0.0.127.in-addr.arpa" in {
        type master;
        file "127.0.0.zone";
};
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
        type master;
        file "127.0.0.zone";
};
zone "${_mydomain}" in {
        type master;
        file "${_mydomain}.lan";
        allow-update { none; };
};
zone "${_mynetrev}.in-addr.arpa" in {
        type master;
        file "${_mynetrev}.db";
        allow-update { none; };
};
EOF

    cat > /mnt/var/lib/named/${_mynetrev}.db <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011601  ;Serial
        3600        ;Refresh
        1800        ;Retry
        604800      ;Expire
        86400       ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  PTR     ${_mydomain}.

${_myip//*.}      IN  PTR     ${AUTOMATION_HOSTNAME}.
$(ip -4 --brief a show "${_bridge_name}" primary | awk -F'.' '{print $NF}' | cut -d/ -f1)      IN  PTR     $(hostname -f).

EOF

    cat > /mnt/var/lib/named/${_mydomain}.lan <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011603  ;Serial
        1m        ;Refresh
        15m        ;Retry
        3w        ;Expire
        2h        ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  A       ${_myip}
        IN  MX 10   ${AUTOMATION_HOSTNAME}.

${AUTOMATION_HOSTNAME//.$_mydomain}         IN  A       ${_myip}
${MYREG//.$_mydomain}         IN  CNAME   ${AUTOMATION_HOSTNAME}
bastion          IN  CNAME   ${AUTOMATION_HOSTNAME}.
$(hostname)         IN  A       $(getent hosts "${HOSTNAME}" | awk '{print $1; exit}')

EOF

    chmod 0644 /mnt/var/lib/named/${_mydomain}.lan /mnt/var/lib/named/${_mynetrev}.db
}

# ── Leap 16 exception ────────────────────────────────────────────────────
#
# On openSUSE Leap 16.0 as a lab-host, guestmount's FUSE layer fails: `ls` of a populated
# directory (/etc/zypp/repos.d) returns "Input/output error". guestfish (libguestfs's native RPC
# interface, no FUSE involved) reads the same qcow2 without error.
# libguestfs's own verbose trace shows its internal readdir/stat/xattr
# calls all succeeding; the failure is specifically in guestmount's FUSE
# reply layer. No older, compatible fuse3/libfuse3-3 build is available via
# zypper on Leap 16 (only one version exists in its repos) to pin/downgrade
# instead. Leap 15.6 and every other tested OS use the unchanged
# guestmount+chroot path above — this is scoped to Leap 16 only, not a
# rewrite of the whole mechanism.
function _lab_host_is_leap16() {
    . /etc/os-release 2>/dev/null
    [[ "${ID}" == "opensuse-leap" && "${VERSION_ID}" == 16* ]]
}

# The automation VM image is edited offline with libguestfs, which on the RHEL family and Fedora
# cannot read the btrfs root filesystem of the openSUSE Leap image, so neither guestmount nor
# guestfish can prepare it there.
function _host_can_prepare_image() {
    . /etc/os-release 2>/dev/null
    [[ " ${ID} ${ID_LIKE} " != *" rhel "* && " ${ID} ${ID_LIKE} " != *" fedora "* ]]
}

# Does the whole configure_image()+configure_os()+install_packages()+
# configure_ssh()+install_lab_scripts()+configure_helm()+configure_sshfs()+
# configure_dns_server()+unmount_image() job in one guestfish session
# instead — same sequence, same generated file content (the heredocs below
# are the exact same text those functions produce), just delivered via
# `upload`/`write`/`sh` instead of a live FUSE mount + chroot. guestfish's
# own internal appliance boots a real minimal Linux environment, so `sh`
# already has a working /proc,/sys,/dev with no bind-mount step needed.
function configure_and_prepare_image_via_guestfish() {
    _msg="Copy image and resize" show_nicer_messages
    cp "${_QCOW_IMAGE}" /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2
    qemu-img resize /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 ${_disk_size:-40}G

    # finish_automation_vm_setup_over_ssh() (below) needs to SSH from THIS
    # host into the automation VM once it boots — the original,
    # non-Leap-16 flow never SSHes anywhere from here (everything happens
    # offline via guestmount), so it only ever authorized ROOT_SSH_PUB_KEY
    # (the operator's own key, on whatever machine runs this script). That
    # assumption breaks in the nested lab-host case: this script runs ON
    # the lab-host VM, which doesn't hold the operator's private key.
    # Ensure this host has its own keypair and authorize both.
    [[ -f /root/.ssh/id_rsa ]] || ssh-keygen -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    local _lab_host_pubkey
    _lab_host_pubkey="$(cat /root/.ssh/id_rsa.pub)"

    local _stage
    _stage="$(mktemp -d)"

    # Static IP via wicked ifcfg, not a NetworkManager .nmconnection — this
    # image's default network stack is wicked (the boot console shows "wicked AutoIPv4/DHCPv4/DHCPv6
    # supplicant service"), and NetworkManager isn't installed at
    # this point at all (that install itself needs internet — see below).
    # Same wicked ifcfg shape configure_bridge() already uses elsewhere in
    # this project for the exact same reason.
    cat > "${_stage}/ifcfg-eth0" <<EOF
BOOTPROTO='static'
IPADDR='${_myip}'
PREFIXLEN='${_mymask_cidr}'
STARTMODE='auto'
EOF
    cat > "${_stage}/routes" <<EOF
default ${_mygw} - -
EOF
    cat > "${_stage}/resolv.conf" <<EOF
search ${_mydomain}
nameserver ${_mydns}
EOF

    cat > "${_stage}/named.conf" <<EOF
options {
        directory "/var/lib/named";
        managed-keys-directory "/var/lib/named/dyn/";
        dump-file "/var/log/named_dump.db";
        statistics-file "/var/log/named.stats";
        listen-on port 53 { any; };
        listen-on-v6 { any; };
        allow-query { 127.0.0.1; 0.0.0.0/0; };
        recursion yes;
        dnssec-validation no;
        forward only;
        forwarders {
            ${_mydns};
        };
};
zone "." in {
        type hint;
        file "root.hint";
};
zone "localhost" in {
        type master;
        file "localhost.zone";
};
zone "0.0.127.in-addr.arpa" in {
        type master;
        file "127.0.0.zone";
};
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
        type master;
        file "127.0.0.zone";
};
zone "${_mydomain}" in {
        type master;
        file "${_mydomain}.lan";
        allow-update { none; };
};
zone "${_mynetrev}.in-addr.arpa" in {
        type master;
        file "${_mynetrev}.db";
        allow-update { none; };
};
EOF

    cat > "${_stage}/${_mynetrev}.db" <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011601  ;Serial
        3600        ;Refresh
        1800        ;Retry
        604800      ;Expire
        86400       ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  PTR     ${_mydomain}.

${_myip//*.}      IN  PTR     ${AUTOMATION_HOSTNAME}.
$(ip -4 --brief a show "${_bridge_name}" primary | awk -F'.' '{print $NF}' | cut -d/ -f1)      IN  PTR     $(hostname -f).

EOF

    cat > "${_stage}/${_mydomain}.lan" <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011603  ;Serial
        1m        ;Refresh
        15m        ;Retry
        3w        ;Expire
        2h        ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  A       ${_myip}
        IN  MX 10   ${AUTOMATION_HOSTNAME}.

${AUTOMATION_HOSTNAME//.$_mydomain}         IN  A       ${_myip}
${MYREG//.$_mydomain}         IN  CNAME   ${AUTOMATION_HOSTNAME}
bastion          IN  CNAME   ${AUTOMATION_HOSTNAME}.
$(hostname)         IN  A       $(getent hosts "${HOSTNAME}" | awk '{print $1; exit}')

EOF

    cat > "${_stage}/download_latest_helm.sh" << 'HELMSCRIPT'
#!/bin/bash
curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail 'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-MYARCH.tar.gz" \
    --output /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output /srv/www/htdocs/helm/KEYS
chmod 0644 /srv/www/htdocs/helm/KEYS /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
HELMSCRIPT
    sed -i "s/MYARCH/${myarch:-amd64}/g" "${_stage}/download_latest_helm.sh"

    cat > "${_stage}/install_helm.sh" <<HELMSCRIPT
#!/bin/bash
[ -d /tmp/helm ] || mkdir /tmp/helm
curl -SsL "http://${AUTOMATION_HOSTNAME}/helm/helm-latest-linux-${myarch:-amd64}.tar.gz" -o /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz
tar xf /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz -C /tmp/helm
cp /tmp/helm/linux-${myarch:-amd64}/helm /usr/local/bin
HELMSCRIPT

    _msg="Clone repository" show_nicer_messages
    rm -rf /tmp/lab-in-a-box
    git clone https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git /tmp/lab-in-a-box

    _msg="Download latest helm and install it" show_nicer_messages
    curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output "${_stage}/KEYS"
    curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail \
        'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-${myarch:-amd64}.tar.gz" \
        --output "${_stage}/helm-latest-linux-${myarch:-amd64}.tar.gz"

    local _gf="${_stage}/script.gf"
    {
        echo 'rm /var/lib/YaST2/reconfig_system'
        echo "upload ${_stage}/resolv.conf /etc/resolv.conf"
        echo "sh \"echo '${AUTOMATION_HOSTNAME}' > /etc/hostname\""
        echo "upload ${_stage}/ifcfg-eth0 /etc/sysconfig/network/ifcfg-eth0"
        echo "upload ${_stage}/routes /etc/sysconfig/network/routes"
        echo 'sh "echo KEYMAP=us >> /etc/vconsole.conf"'
        echo "ln-sf /usr/share/zoneinfo/${_timezone:-Europe/Zurich} /etc/localtime"

        # zypper install / service enable / running install_automation_node_
        # scripts.sh all need internet or already-installed packages this
        # image does not ship. guestfish's own --network appliance interface never gets a usable IP
        # (up, no DHCP lease, `getent hosts` fails), unlike a booted VM's own network stack. Deferred to
        # finish_automation_vm_setup_over_ssh(), run once wait_for_vm()
        # confirms the VM is actually up and reachable at ${_myip} — plain
        # SSH against a normally-booted host, no libguestfs involved at
        # all, exactly like provisioning any other already-running KVM
        # guest. Only jeos-firstboot is disabled here (needs no network,
        # and must be done before first boot to have any effect).
        echo 'sh "systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true"'

        echo "sh \"ssh-keygen -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa\""
        echo 'sh "cp /root/.ssh/id_rsa.pub /srv/www/htdocs/id_rsa.pub && chmod 0644 /srv/www/htdocs/id_rsa.pub"'
        echo "sh \"echo '${ROOT_SSH_PUB_KEY}' >> /root/.ssh/authorized_keys\""
        echo "sh \"echo '${_lab_host_pubkey}' >> /root/.ssh/authorized_keys\""
        echo "sh \"echo 'root:${root_pwd}' | chpasswd -c SHA512\""

        echo 'rm-rf /var/tmp/lab-in-a-box'
        # copy-in takes the LOCAL basename and creates it inside the given
        # remote directory (i.e. this creates /var/tmp/lab-in-a-box because
        # the local source dir is itself named lab-in-a-box) — not the same
        # semantics as `cp -a src/. dst`, so the local clone dir above is
        # named to match exactly what this needs to produce. Pure file
        # copy, no network needed inside the guest for this part.
        echo 'copy-in /tmp/lab-in-a-box /var/tmp'

        echo 'mkdir-p /srv/www/htdocs/helm'
        echo 'mkdir-p /srv/www/sources'
        echo 'chmod 0755 /srv/www/htdocs/helm'
        echo 'chmod 0755 /srv/www/sources'
        echo "upload ${_stage}/KEYS /srv/www/htdocs/helm/KEYS"
        echo 'chmod 0644 /srv/www/htdocs/helm/KEYS'
        echo "upload ${_stage}/helm-latest-linux-${myarch:-amd64}.tar.gz /srv/www/htdocs/helm/helm-latest-linux-${myarch:-amd64}.tar.gz"
        echo "upload ${_stage}/download_latest_helm.sh /usr/local/bin/download_latest_helm.sh"
        echo "upload ${_stage}/install_helm.sh /srv/www/htdocs/helm/install_helm.sh"
        echo 'chmod 0755 /usr/local/bin/download_latest_helm.sh'
        echo 'chmod 0755 /srv/www/htdocs/helm/install_helm.sh'

        echo "sh \"echo '${_virt_srv:-root@hypervisor}:/var/lib/libvirt/images/sources /srv/www/htdocs/sources fuse.sshfs  noauto,x-systemd.automount,_netdev,reconnect,identityfile=/root/.ssh/id_rsa,allow_other,default_permissions 0 0' >> /etc/fstab\""

        echo "upload ${_stage}/named.conf /etc/named.conf"
        echo "upload ${_stage}/${_mynetrev}.db /var/lib/named/${_mynetrev}.db"
        echo "upload ${_stage}/${_mydomain}.lan /var/lib/named/${_mydomain}.lan"
        echo "chmod 0644 /var/lib/named/${_mynetrev}.db"
        echo "chmod 0644 /var/lib/named/${_mydomain}.lan"
    } > "${_gf}"

    _msg="Mount image for configuration (guestfish, Leap 16 exception)" show_nicer_messages
    guestfish --rw -i -a /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 -f "${_gf}"
    local _rc=$?

    # id_rsa.pub only exists inside the guest now — pull it out the same
    # way configure_ssh() does (via /mnt) so the rest of this script's
    # own authorized_keys/pubkey-echo behavior is unaffected.
    guestfish --ro -i -a /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 \
        download /root/.ssh/id_rsa.pub "${_stage}/id_rsa.pub" 2>/dev/null
    if [[ -s "${_stage}/id_rsa.pub" ]]; then
        cat "${_stage}/id_rsa.pub" >> /root/.ssh/authorized_keys
        echo -e "\n# Automation VM public key:\n$(cat "${_stage}/id_rsa.pub")\n"
    fi

    rm -rf /tmp/lab-in-a-box "${_stage}"
    return "${_rc}"
}

# The rest of install_packages()/install_lab_scripts()'s original job
# (zypper install, service enable, running install_automation_node_
# scripts.sh — everything that needs real internet access or a fully
# booted OS) — run once wait_for_vm() confirms the automation VM is
# actually up, over plain SSH, exactly like provisioning any other
# already-running KVM guest. The lab-in-a-box tree is already at
# /var/tmp/lab-in-a-box (copied in offline, above).
function finish_automation_vm_setup_over_ssh() {
    _msg="Install required packages (post-boot, Leap 16 exception)" show_nicer_messages
    ssh -o StrictHostKeyChecking=accept-new "root@${_myip}" \
        "zypper --gpg-auto-import-keys install -y vim-small git rsync apache2 bind-utils bind docker podman libvirt-client jq virt-install salt-ssh ipcalc fuse3 sshfs netcat-openbsd python311 kubernetes1.35-client" \
        || { echo -e "\033[1;31mERROR\033[0m: package installation on the automation VM failed" >&2; exit 1; }
    ssh "root@${_myip}" "systemctl enable --now named apache2"

    _msg="Run install_automation_node_scripts.sh (post-boot, Leap 16 exception)" show_nicer_messages
    ssh "root@${_myip}" "cd /var/tmp/lab-in-a-box && bash install_automation_node_scripts.sh" \
        || { echo -e "\033[1;31mERROR\033[0m: install_automation_node_scripts.sh failed on the automation VM" >&2; exit 1; }
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

# Only relevant when _network_mode=nat: forward the automation VM's own
# ports in from the KVM host's real IP, reusing the exact same rule-building
# function libs/services.py's PortForwardService calls for lab nodes later
# (libs/portforward.py's apply_forwarded_ports()) — invoked here as a plain
# python3.11 call since this bash script has no importable module context
# of its own. _SLA_DIR resolves this script's own directory regardless of
# the caller's cwd (setup_kvm_node.py's do_it_all() runs this with cwd set
# to this same directory, but this stays correct even if invoked directly).
_SLA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

function configure_nat_port_forwarding() {
    [[ "${_network_mode:-bridge}" != "nat" ]] && return
    _msg="Forward automation VM ports from the KVM host" show_nicer_messages
    python3.11 -c "
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
    sed "s/NETCONFIG_DNS_STATIC_SERVERS=.*/NETCONFIG_DNS_STATIC_SERVERS=\"${_myip} ${_mydns}\"/;s/NETCONFIG_DNS_STATIC_SEARCHLIST=.*/NETCONFIG_DNS_STATIC_SEARCHLIST=\"${_mydomain}\"/" \
        -i /etc/sysconfig/network/config
    if grep -i "^search " /etc/resolv.conf &>/dev/null; then
        sed "/search.*/a nameserver ${_myip} " -i /etc/resolv.conf
    else
        sed "1s/^/search ${_mydomain}/" -i /etc/resolv.conf
        sed "1s/^/nameserver ${_myip}/" -i /etc/resolv.conf
    fi
}


# --- Main ---

if ! _host_can_prepare_image; then
    echo -e "\033[1;31mERROR\033[0m: the automation VM cannot be built on ${PRETTY_NAME:-this host}: libguestfs on the RHEL family and Fedora cannot read the btrfs filesystem of the openSUSE Leap automation image. The host itself is set up; create the automation node on another host for now." >&2
    exit 1
fi

_msg="Delete VM \"${AUTOMATION_HOSTNAME}\" if it exists" show_nicer_messages
if virsh desc "${AUTOMATION_HOSTNAME}" &>/dev/null; then
    virsh -c ${_qemu_addr} destroy  "${AUTOMATION_HOSTNAME}" 2>/dev/null
    virsh -c ${_qemu_addr} undefine "${AUTOMATION_HOSTNAME}" --remove-all-storage
fi

detect_bridge
configure_bridge
generate_mac
detect_vm_osinfo

if _lab_host_is_leap16; then
    configure_and_prepare_image_via_guestfish
else
    configure_image
    configure_os
    install_packages
    configure_ssh
    install_lab_scripts
    configure_helm
    configure_sshfs
    configure_dns_server
    unmount_image
fi
create_vm
wait_for_vm
_lab_host_is_leap16 && finish_automation_vm_setup_over_ssh
configure_nat_port_forwarding
configure_host_dns
