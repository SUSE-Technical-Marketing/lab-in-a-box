#!/bin/bash
# Configuration steps of a lab-in-a-box automation node: a VM, a podman container or a Kubernetes pod.
#
# Run inside the node as "automation-node-lib.sh configure [--offline|--boot]" it configures the node from
# NODE_DIR (settings, this file and the lab-in-a-box tree), which setup_lab_automation.sh puts there:
#   --offline  in a chroot of the VM image before its first boot: services are enabled, not restarted
#   --boot     from lab-node-setup.service at every start of a container or pod: nothing is downloaded
# Sourced by setup_lab_automation.sh on the KVM host for write_node_settings and the write_* steps it needs
# before the node runs.
#
# write_* ROOT functions write files under the directory ROOT ("/" inside the node). Settings come from
# NODE_DIR/settings, see NODE_SETTINGS.

NODE_DIR=/etc/lab_creation/node

# Shell variables the node needs, written to NODE_DIR/settings by write_node_settings.
NODE_SETTINGS="AUTOMATION_HOSTNAME MYREG _myip _mymask_cidr _mygw _mydns _mydomain _mynetrev _timezone _virt_srv
ROOT_SSH_PUB_KEY HOST_SSH_PUB_KEY ROOT_PWD_HASH _host_ip _host_name _host_fqdn myarch _node_kind _node_netstack
_python_bin"

# sshfs mount options for the hypervisor's source images. accept-new: the node has never connected to the hypervisor
# before, so its host key is not known yet.
_SSHFS_OPTS="_netdev,reconnect,identityfile=/root/.ssh/id_rsa,allow_other,default_permissions,StrictHostKeyChecking=accept-new"

# Packages a VM gets on top of the openSUSE Leap image; the container image has them already. kubernetes1.35-client:
# same kubectl as the hypervisors (libs/kvm_host_profiles.py's KUBECTL_VERSION). Nothing that pulls in a kernel:
# installing one in the chroot rebuilds the initrd there, and the VM then cannot find its root disk.
_VM_PACKAGES="vim-small git rsync apache2 bind-utils bind docker podman libvirt-client jq virt-install salt-ssh ipcalc
fuse3 sshfs netcat-openbsd python311 kubernetes1.35-client openssl"

# Writes NODE_DIR/settings (mode 0600) under ROOT from the current values of NODE_SETTINGS.
function write_node_settings() {
    local _root="$1" _v
    mkdir -p "${_root}${NODE_DIR}"
    for _v in ${NODE_SETTINGS}; do
        printf '%s=%q\n' "${_v}" "${!_v}"
    done > "${_root}${NODE_DIR}/settings"
    chmod 0600 "${_root}${NODE_DIR}/settings"
}

# Writes the BIND configuration and, unless they exist, the lab zones (labs add records to them) under ROOT.
function write_dns_files() {
    local _root="$1"
    mkdir -p "${_root}/etc" "${_root}/var/lib/named"
    cat > "${_root}/etc/named.conf" <<EOF
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
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
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
    chmod 0644 "${_root}/etc/named.conf"

    [[ -f "${_root}/var/lib/named/${_mynetrev}.db" ]] || cat > "${_root}/var/lib/named/${_mynetrev}.db" <<EOF
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
${_host_ip##*.}      IN  PTR     ${_host_fqdn}.

EOF

    [[ -f "${_root}/var/lib/named/${_mydomain}.lan" ]] || cat > "${_root}/var/lib/named/${_mydomain}.lan" <<EOF
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
${_host_name}         IN  A       ${_host_ip}

EOF
    chmod 0644 "${_root}/var/lib/named/${_mydomain}.lan" "${_root}/var/lib/named/${_mynetrev}.db"
}

# Writes download_latest_helm.sh (run on the automation node) and install_helm.sh (served to lab nodes) under ROOT.
function write_helm_scripts() {
    local _root="$1"
    mkdir -p "${_root}/usr/local/bin" "${_root}/srv/www/htdocs/helm"
    cat > "${_root}/usr/local/bin/download_latest_helm.sh" << 'HELMSCRIPT'
#!/bin/bash
curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail 'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-MYARCH.tar.gz" \
    --output /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output /srv/www/htdocs/helm/KEYS
chmod 0644 /srv/www/htdocs/helm/KEYS /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
HELMSCRIPT
    sed -i "s/MYARCH/${myarch:-amd64}/g" "${_root}/usr/local/bin/download_latest_helm.sh"

    # install_helm.sh: served by the automation node, run on client nodes
    cat > "${_root}/srv/www/htdocs/helm/install_helm.sh" << HELMSCRIPT
#!/bin/bash
[ -d /tmp/helm ] || mkdir /tmp/helm
curl -SsL \${LAB_CURL_TLS-} "\${LAB_PROVISIONING_URL:-http://${AUTOMATION_HOSTNAME}}/helm/helm-latest-linux-${myarch:-amd64}.tar.gz" -o /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz
tar xf /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz -C /tmp/helm
cp /tmp/helm/linux-${myarch:-amd64}/helm /usr/local/bin
HELMSCRIPT

    chmod 0755 "${_root}/usr/local/bin/download_latest_helm.sh" "${_root}/srv/www/htdocs/helm/install_helm.sh"
}

# Downloads the latest helm tarball and the helm release KEYS into directory $1 (mode 0644), unless both are there.
function download_helm_files() {
    local _dir="$1" _tgz="helm-latest-linux-${myarch:-amd64}.tar.gz"
    [[ -s "${_dir}/KEYS" && -s "${_dir}/${_tgz}" ]] && return
    mkdir -p "${_dir}"
    curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output "${_dir}/KEYS"
    curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail \
        'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-${myarch:-amd64}.tar.gz" \
        --output "${_dir}/${_tgz}"
    chmod 0644 "${_dir}/KEYS" "${_dir}/${_tgz}"
}

# Writes the sshfs automount of the hypervisor's source images on /srv/www/htdocs/sources under ROOT: an fstab line on
# a VM, systemd units in a container or pod (systemd's fstab generator does not run there).
function write_sshfs_mount() {
    local _root="$1" _what="${_virt_srv:-root@hypervisor}:/var/lib/libvirt/images/sources"
    if [[ "${_node_kind}" == vm ]]; then
        grep -qs " /srv/www/htdocs/sources fuse.sshfs " "${_root}/etc/fstab" \
            || echo "${_what} /srv/www/htdocs/sources fuse.sshfs  noauto,x-systemd.automount,${_SSHFS_OPTS} 0 0" >> "${_root}/etc/fstab"
        return
    fi
    mkdir -p "${_root}/etc/systemd/system"
    cat > "${_root}/etc/systemd/system/srv-www-htdocs-sources.mount" <<EOF
[Unit]
Description=Hypervisor source images (sshfs)
After=network-online.target

[Mount]
What=${_what}
Where=/srv/www/htdocs/sources
Type=fuse.sshfs
Options=${_SSHFS_OPTS}
EOF
    cat > "${_root}/etc/systemd/system/srv-www-htdocs-sources.automount" <<EOF
[Unit]
Description=Hypervisor source images (sshfs), mounted on first access

[Automount]
Where=/srv/www/htdocs/sources

[Install]
WantedBy=multi-user.target
EOF
}

# Writes /etc/resolv.conf under ROOT: the node's own named first in a container or pod; on a VM only _mydns, since
# named is not running yet while packages are installed (NetworkManager adds _myip once the VM runs).
function write_resolv_conf() {
    local _root="$1"
    mkdir -p "${_root}/etc"
    [[ -L "${_root}/etc/resolv.conf" ]] && rm -f "${_root}/etc/resolv.conf"
    {
        echo "search ${_mydomain}"
        [[ "${_node_kind}" == vm ]] || echo "nameserver 127.0.0.1"
        echo "nameserver ${_mydns}"
    } > "${_root}/etc/resolv.conf"
}

# Writes a VM's hostname, keymap, static network configuration (_node_netstack: nm or wicked) under ROOT.
function write_vm_system() {
    local _root="$1"
    mkdir -p "${_root}/etc"
    echo "${AUTOMATION_HOSTNAME}" > "${_root}/etc/hostname"
    grep -qs "^KEYMAP=" "${_root}/etc/vconsole.conf" || echo "KEYMAP=us" >> "${_root}/etc/vconsole.conf"
    if [[ "${_node_netstack}" == wicked ]]; then
        mkdir -p "${_root}/etc/sysconfig/network"
        printf "BOOTPROTO='static'\nIPADDR='%s'\nPREFIXLEN='%s'\nSTARTMODE='auto'\n" "${_myip}" "${_mymask_cidr}" \
            > "${_root}/etc/sysconfig/network/ifcfg-eth0"
        echo "default ${_mygw} - -" > "${_root}/etc/sysconfig/network/routes"
    else
        mkdir -p "${_root}/etc/NetworkManager/system-connections"
        cat > "${_root}/etc/NetworkManager/system-connections/static.nmconnection" <<EOF
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
        chmod 0600 "${_root}/etc/NetworkManager/system-connections/static.nmconnection"
    fi
}

# Appends each key given as an argument to ROOT/root/.ssh/authorized_keys unless it is there already.
function write_authorized_keys() {
    local _root="$1" _f="$1/root/.ssh/authorized_keys" _k
    shift
    mkdir -p "${_root}/root/.ssh"
    chmod 0700 "${_root}/root/.ssh"
    touch "${_f}"
    for _k in "$@"; do
        [[ -z "${_k}" ]] || grep -qxF "${_k}" "${_f}" || echo "${_k}" >> "${_f}"
    done
    chmod 0600 "${_f}"
}

# Installs the VM's packages and sets which services start at boot.
function node_install_packages() {
    local _pkgs="${_VM_PACKAGES}"
    [[ "${_node_netstack}" == wicked ]] || _pkgs+=" NetworkManager"
    # shellcheck disable=SC2086
    zypper --non-interactive --gpg-auto-import-keys install -y ${_pkgs} || return 1
    systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null
    systemctl disable --now firewalld.service 2>/dev/null
    if [[ "${_node_netstack}" != wicked ]]; then
        systemctl disable wicked.service 2>/dev/null
        systemctl enable NetworkManager.service
    fi
    systemctl enable sshd.service named.service apache2.service
}

# Creates the node's own SSH key once and publishes its public half for the lab VMs.
function node_ssh_key() {
    [[ -f /root/.ssh/id_rsa ]] || ssh-keygen -q -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    mkdir -p /srv/www/htdocs
    cp /root/.ssh/id_rsa.pub /srv/www/htdocs/id_rsa.pub && chmod 0644 /srv/www/htdocs/id_rsa.pub
}

# Installs the lab scripts from NODE_DIR/lab-in-a-box.
function node_install_lab_scripts() {
    (cd "${NODE_DIR}/lab-in-a-box" \
        && _python_bin="${_python_bin}" _tls_ips="${_myip}" _backup=off bash install_automation_node_scripts.sh)
}

# Configures the node from NODE_DIR. $1: --offline or --boot (see the header), or nothing.
function configure_node() {
    local _mode="${1:-}"
    # shellcheck source=/dev/null
    . "${NODE_DIR}/settings" || return 1
    if [[ "${_node_kind}" == vm ]]; then
        write_resolv_conf /
        write_vm_system /
        node_install_packages || { echo "ERROR: installing the automation node's packages failed" >&2; return 1; }
    else
        write_resolv_conf /
    fi
    ln -sf "/usr/share/zoneinfo/${_timezone:-Europe/Zurich}" /etc/localtime
    write_dns_files /
    write_helm_scripts /
    [[ "${_mode}" == --boot ]] || download_helm_files /srv/www/htdocs/helm
    write_sshfs_mount /
    write_authorized_keys / "${ROOT_SSH_PUB_KEY}" "${HOST_SSH_PUB_KEY}"
    echo "root:${ROOT_PWD_HASH}" | chpasswd -e
    node_ssh_key
    node_install_lab_scripts || { echo "ERROR: install_automation_node_scripts.sh failed" >&2; return 1; }
    if [[ "${_mode}" == --offline ]]; then
        return 0
    fi
    systemctl daemon-reload
    if [[ "${_node_kind}" == vm ]]; then
        systemctl start srv-www-htdocs-sources.automount
    else
        systemctl enable --now srv-www-htdocs-sources.automount
    fi
    if [[ "${_mode}" == --boot ]]; then
        systemctl restart --no-block named apache2 sshd
    else
        systemctl restart named apache2 sshd
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    case "${1:-}" in
        configure) shift; configure_node "$@" ;;
        *) echo "Usage: $0 configure [--offline|--boot]" >&2; exit 2 ;;
    esac
fi
