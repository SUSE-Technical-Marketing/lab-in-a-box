#!/bin/bash
# setup_demo_server/automation-node-lib.sh: runs "configure" for real inside this test container (as the automation
# node), with systemctl, zypper, ssh-keygen and curl replaced by stubs, for a container node and for a VM node (both
# network stacks), and checks the files it writes, that re-runs keep zones and keys, and the settings round trip.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo=$PWD
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

_t=$(mktemp -d)
_bin="${_t}/bin" _log="${_t}/calls.log"
mkdir -p "${_bin}"
export STUB_LOG="${_log}"
for _c in systemctl zypper; do
    printf '#!/bin/bash\necho "%s $*" >> "${STUB_LOG}"\nexit 0\n' "${_c}" > "${_bin}/${_c}"
done
cat > "${_bin}/ssh-keygen" <<'EOF'
#!/bin/bash
echo "ssh-keygen $*" >> "${STUB_LOG}"
f=${*: -1}
echo PRIVATE > "$f"; echo "ssh-rsa NODEKEY root@automation" > "$f.pub"
EOF
cat > "${_bin}/curl" <<'EOF'
#!/bin/bash
echo "curl $*" >> "${STUB_LOG}"
while [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && { echo data > "$2"; shift; }; shift; done
[[ -t 1 ]] || echo v3.99.0
EOF
chmod 0755 "${_bin}"/*
export PATH="${_bin}:${PATH}"

# shellcheck source=setup_demo_server/automation-node-lib.sh
. setup_demo_server/automation-node-lib.sh

# ── settings round trip ───────────────────────────────────────────────────────
AUTOMATION_HOSTNAME=automation.mydemo.lab MYREG=registry.mydemo.lab _myip=192.0.2.10 _mymask_cidr=24 _mygw=192.0.2.1
_mydns=192.0.2.53 _mydomain=mydemo.lab _mynetrev=2.0.192 _timezone=UTC _virt_srv=root@192.0.2.2
ROOT_SSH_PUB_KEY="ssh-ed25519 OPERATORKEY op@laptop" HOST_SSH_PUB_KEY="ssh-rsa HOSTKEY root@host"
ROOT_PWD_HASH='$6$salt$abc/def.ghi' _host_ip=192.0.2.2 _host_name=kvmhost _host_fqdn=kvmhost.example.com myarch=amd64
_node_kind=container _node_netstack=none _python_bin=python3.11
write_node_settings "${_t}/stage"
[[ "$(stat -c %a "${_t}/stage${NODE_DIR}/settings")" == 600 ]] || fail "the settings file is not mode 0600"
(
    unset ROOT_PWD_HASH ROOT_SSH_PUB_KEY
    # shellcheck source=/dev/null
    . "${_t}/stage${NODE_DIR}/settings"
    [[ "${ROOT_PWD_HASH}" == '$6$salt$abc/def.ghi' && "${ROOT_SSH_PUB_KEY}" == "ssh-ed25519 OPERATORKEY op@laptop" ]]
) || fail "the settings file does not restore values with \$ and spaces"
grep -q "s3cret" "${_t}/stage${NODE_DIR}/settings" && fail "the settings file contains a plaintext password"

# ── the node: inputs in NODE_DIR, configure ───────────────────────────────────
mkdir -p "${NODE_DIR}/lab-in-a-box"
cp "${_t}/stage${NODE_DIR}/settings" "${NODE_DIR}/settings"
cp setup_demo_server/automation-node-lib.sh "${NODE_DIR}/"
git -c safe.directory="${_repo}" ls-files -z | while IFS= read -r -d '' f; do [[ -e "$f" ]] && printf '%s\0' "$f"; done \
    | tar --null -T - -cf - | tar -xf - -C "${NODE_DIR}/lab-in-a-box"
echo "abc1234 scripts/setup_lab.py" > "${NODE_DIR}/lab-in-a-box/.lab-versions"
# Committed files carry their stamped version; put the placeholder back so the install stamps it again.
sed -i 's/^__version__ = .*/__version__ = "__LABVERSION__"/' "${NODE_DIR}/lab-in-a-box/scripts/setup_lab.py"

run_configure() {
    : > "${_log}"
    bash "${NODE_DIR}/automation-node-lib.sh" configure "$@" > "${_t}/configure.log" 2>&1 \
        || { tail -20 "${_t}/configure.log"; fail "configure $* failed"; }
}

run_configure
grep -q "forwarders {" /etc/named.conf && grep -q "192.0.2.53;" /etc/named.conf || fail "named.conf does not forward to _mydns"
_zone=/var/lib/named/mydemo.lab.lan
grep -Eq "^automation +IN  A +192\.0\.2\.10$" "${_zone}" || fail "the zone lacks the automation node's A record"
grep -Eq "^kvmhost +IN  A +192\.0\.2\.2$" "${_zone}" || fail "the zone lacks the hypervisor's A record"
grep -Eq "^2 +IN  PTR +kvmhost\.example\.com\.$" /var/lib/named/2.0.192.db || fail "the reverse zone lacks the hypervisor's PTR record"
[[ -x /usr/local/bin/download_latest_helm.sh && -x /srv/www/htdocs/helm/install_helm.sh ]] || fail "the helm scripts are not written"
[[ -s /srv/www/htdocs/helm/KEYS ]] || fail "the helm files are not downloaded"
grep -q "^What=root@192.0.2.2:/var/lib/libvirt/images/sources$" /etc/systemd/system/srv-www-htdocs-sources.mount \
    && grep -q "StrictHostKeyChecking=accept-new" /etc/systemd/system/srv-www-htdocs-sources.mount \
    || fail "the sshfs mount unit is not written for a container node"
grep -q "/srv/www/htdocs/sources" /etc/fstab 2>/dev/null && fail "a container node got an fstab entry"
grep -q "^systemctl enable --now srv-www-htdocs-sources.automount$" "${_log}" || fail "the sshfs automount is not enabled"
grep -qx "nameserver 127.0.0.1" /etc/resolv.conf || fail "a container node does not resolve through its own named"
grep -qxF "ssh-ed25519 OPERATORKEY op@laptop" /root/.ssh/authorized_keys && grep -qxF "ssh-rsa HOSTKEY root@host" /root/.ssh/authorized_keys \
    || fail "the operator and hypervisor keys are not authorized"
grep -q '^root:\$6\$salt\$abc/def.ghi:' /etc/shadow || fail "the root password hash is not set"
[[ "$(cat /srv/www/htdocs/id_rsa.pub)" == "ssh-rsa NODEKEY root@automation" ]] || fail "the node's public key is not published"
[[ -x /usr/local/bin/setup_lab.py ]] || fail "the lab scripts are not installed"
grep -q "abc1234" /usr/local/bin/setup_lab.py || fail "setup_lab.py is not stamped with its version from .lab-versions"
grep -q "^systemctl restart named apache2 sshd$" "${_log}" || fail "the services are not restarted"
grep -q "^zypper" "${_log}" && fail "a container node installed packages"

# A re-run keeps the zones and the key, and adds nothing twice.
echo "custom IN A 192.0.2.99" >> "${_zone}"
run_configure
grep -q "^custom IN A 192.0.2.99$" "${_zone}" || fail "a re-run overwrote an existing zone"
[[ "$(grep -c OPERATORKEY /root/.ssh/authorized_keys)" == 1 ]] || fail "a re-run authorized a key twice"
grep -q "^ssh-keygen" "${_log}" && fail "a re-run created a new node key"

# At boot: nothing is downloaded and the restart does not wait.
rm -f /srv/www/htdocs/helm/KEYS
run_configure --boot
grep -q "^curl" "${_log}" && fail "configure --boot downloaded files"
grep -q "^systemctl restart --no-block named apache2 sshd$" "${_log}" || fail "configure --boot restarts the services blocking"

# ── a VM node ─────────────────────────────────────────────────────────────────
vm_settings() {
    _node_kind=vm _node_netstack="$1"
    write_node_settings /
}
vm_settings nm
run_configure --offline
grep -q "^zypper .* install -y .*python311.* NetworkManager$" "${_log}" || fail "a VM node does not install its packages with NetworkManager"
grep -q "^systemctl enable NetworkManager.service$" "${_log}" && grep -q "^systemctl disable wicked.service$" "${_log}" \
    || fail "a VM node with NetworkManager does not switch from wicked"
grep -q "^systemctl disable --now firewalld.service$" "${_log}" || fail "a VM node keeps firewalld"
grep -q "^systemctl enable sshd.service named.service apache2.service$" "${_log}" || fail "a VM node does not enable its services"
grep -q "^systemctl restart named apache2 sshd\|^systemctl restart --no-block" "${_log}" && fail "configure --offline restarted services"
grep -q "^address1=192.0.2.10/24$" /etc/NetworkManager/system-connections/static.nmconnection || fail "the VM's NetworkManager connection is not written"
[[ "$(cat /etc/hostname)" == automation.mydemo.lab ]] || fail "the VM's hostname is not set"
grep -qx "nameserver 127.0.0.1" /etc/resolv.conf && fail "a VM node resolves through its not yet running named"
grep -q " /srv/www/htdocs/sources fuse.sshfs .*StrictHostKeyChecking=accept-new" /etc/fstab || fail "a VM node does not get the sshfs fstab entry"

vm_settings wicked
run_configure
grep -q "NetworkManager" "${_log}" && fail "a VM node with wicked installs or enables NetworkManager"
grep -q "^systemctl disable --now firewalld.service$" "${_log}" || fail "a VM node with wicked keeps firewalld"
grep -q "^systemctl start srv-www-htdocs-sources.automount$" "${_log}" || fail "a running VM node does not start the sshfs automount"
grep -q "^IPADDR='192.0.2.10'$" /etc/sysconfig/network/ifcfg-eth0 && grep -q "^default 192.0.2.1 - -$" /etc/sysconfig/network/routes \
    || fail "the VM's wicked configuration is not written"
[[ "$(grep -c "/srv/www/htdocs/sources fuse.sshfs" /etc/fstab)" == 1 ]] || fail "a re-run added the fstab entry twice"

exit "${_fail}"
