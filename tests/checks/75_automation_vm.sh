#!/bin/bash
# setup_lab_automation.sh with _automation_node=vm: runs the real script with libvirt, libguestfs, chroot, ssh and the
# network commands replaced by stubs, for the guestmount + chroot path and for the guestfish + SSH path (Leap 16
# hosts), and checks that both put the same inputs into the VM and run automation-node-lib.sh configure there.
# Independent container (it replaces this container's /etc/os-release) — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo=$PWD
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

_t=$(mktemp -d)
_bin="${_t}/bin" _log="${_t}/calls.log"
mkdir -p "${_bin}" "${_t}/lab" "${_t}/node" /var/lib/libvirt/images
export STUB_LOG="${_log}" STUB_DIR="${_t}"

# chroot /mnt CMD and ssh root@IP "CMD" both run CMD "inside the VM", which is ${STUB_DIR}/node.
cat > "${_bin}/in-node" <<'EOF'
#!/bin/bash
case "$1 $2 $3" in
    "tar -C /") shift 3; tar -C "${STUB_DIR}/node" "$@" ;;
    "rm -rf "*) rm -rf "${STUB_DIR}/node$3" ;;
    "cat /root/.ssh/id_rsa.pub ") echo "ssh-rsa VMKEY root@automation" ;;
esac
exit 0
EOF
cat > "${_bin}/chroot" <<'EOF'
#!/bin/bash
echo "chroot $*" >> "${STUB_LOG}"
shift
exec in-node "$@"
EOF
cat > "${_bin}/ssh" <<'EOF'
#!/bin/bash
echo "ssh $*" >> "${STUB_LOG}"
eval "set -- ${*: -1}"
exec in-node "$@"
EOF
cat > "${_bin}/guestfish" <<'EOF'
#!/bin/bash
echo "guestfish $*" >> "${STUB_LOG}"
cat "${*: -1}" >> "${STUB_DIR}/guestfish.script"
exit 0
EOF
cat > "${_bin}/ip" <<'EOF'
#!/bin/bash
[[ "$*" == "-4 -br addr show br0" ]] && echo "br0              UP             192.0.2.2/24"
exit 0
EOF
for _c in systemctl virsh virt-install nc tput ssh-keygen hostname curl qemu-img guestmount guestunmount mount umount \
          mountpoint lsof; do
    printf '#!/bin/bash\necho "%s $*" >> "${STUB_LOG}"\n[[ "%s $*" == "virsh "*" desc "* || "%s" == mountpoint ]] && exit 1\nexit 0\n' \
        "${_c}" "${_c}" "${_c}" > "${_bin}/${_c}"
done
chmod 0755 "${_bin}"/*
mkdir -p /root/.ssh && : > /root/.ssh/id_rsa && echo "ssh-ed25519 HOSTKEY root@host" > /root/.ssh/id_rsa.pub
echo image > "${_t}/leap.qcow2"

cat > "${_t}/lab/lab.cfg" <<EOF
_automation_node="vm"
_network_mode="bridge"
_bridge_name="br0"
_bridge_nic=""
_myip="192.0.2.10"
_mynet="192.0.2.0/24"
_mygw="192.0.2.1"
_mydns="192.0.2.53"
_mynetrev="2.0.192"
_mydomain="mydemo.lab"
_QCOW_IMAGE="${_t}/leap.qcow2"
AUTOMATION_HOSTNAME="automation.mydemo.lab"
MYREG="registry.mydemo.lab"
ROOT_SSH_PUB_KEY="ssh-ed25519 OPERATORKEY op@laptop"
root_pwd="s3cret-pw"
_qemu_addr="qemu:///system"
_virt_srv="root@192.0.2.2"
EOF

run_setup() {
    : > "${_log}"; rm -rf "${_t}/node"; mkdir -p "${_t}/node"
    (cd "${_t}/lab" && PATH="${_bin}:${PATH}" LAB_PYTHON=python3.11 \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
        bash "${_repo}/setup_demo_server/setup_lab_automation.sh" > "${_t}/run.log" 2>&1)
    if grep -q "ERROR" "${_t}/run.log" || ! grep -q "Reconfigure host to use new VM as DNS server" "${_t}/run.log"; then
        tail -30 "${_t}/run.log"; fail "setup_lab_automation.sh failed in VM mode ($1)"
    fi
}

# Checks the inputs in the VM and the settings for network stack $1.
check_inputs() {
    local _node="${_t}/node/etc/lab_creation/node"
    [[ -x "${_node}/automation-node-lib.sh" && -f "${_node}/lab-in-a-box/install_automation_node_scripts.sh" ]] \
        || fail "$1: the library or the lab tree is not put into the VM"
    [[ -e "${_node}/lab-in-a-box/.git" ]] && fail "$1: the lab tree in the VM has .git"
    (
        # shellcheck source=/dev/null
        . "${_node}/settings"
        [[ "${_node_kind}" == vm && "${_node_netstack}" == "$1" && "${_python_bin}" == python3.11 ]]
    ) || fail "$1: the VM settings do not say vm, network stack $1 and python3.11"
    grep -q "s3cret-pw" "${_node}/settings" && fail "$1: the VM settings contain the plaintext root password"
}

# ── guestmount + chroot ───────────────────────────────────────────────────────
run_setup chroot
check_inputs nm
grep -q "^guestmount -i --rw -a /var/lib/libvirt/images/automation.mydemo.lab.qcow2 /mnt/$" "${_log}" || fail "the image is not mounted"
grep -q "^chroot /mnt tar -C / --no-overwrite-dir -xf -$" "${_log}" || fail "the inputs are not copied into the image"
grep -q "^chroot /mnt bash /etc/lab_creation/node/automation-node-lib.sh configure --offline$" "${_log}" \
    || fail "the image is not configured offline with automation-node-lib.sh"
grep -q "^guestunmount /mnt$" "${_log}" || fail "the image is not unmounted"
[[ "$(grep "^guestunmount\|^virt-install" "${_log}" | awk '{printf "%s ", $1}')" == "guestunmount virt-install " ]] \
    || fail "the VM is created before the image is unmounted"
grep -q "VMKEY" /root/.ssh/authorized_keys || fail "the VM's key is not authorized on the host"
grep -q "^guestfish\|^ssh " "${_log}" && fail "the chroot path used guestfish or SSH"

# ── guestfish + SSH (Leap 16 host) ────────────────────────────────────────────
cp /etc/os-release "${_t}/os-release"
printf 'ID="opensuse-leap"\nVERSION_ID="16.0"\n' > /etc/os-release
run_setup guestfish
cp "${_t}/os-release" /etc/os-release
check_inputs wicked
grep -q "^guestfish --rw -i -a /var/lib/libvirt/images/automation.mydemo.lab.qcow2 -f " "${_log}" || fail "the image is not prepared with guestfish"
grep -q "^copy-in .*/etc /$" "${_t}/guestfish.script" && grep -q "^copy-in .*/root /$" "${_t}/guestfish.script" \
    || fail "guestfish does not copy the staged tree into the image"
grep -q "^ssh -n .*root@192.0.2.10 bash /etc/lab_creation/node/automation-node-lib.sh configure $" "${_log}" \
    || fail "the running VM is not configured over SSH with automation-node-lib.sh"
grep -q "^guestmount\|^chroot " "${_log}" && fail "the guestfish path used guestmount or chroot"
[[ "$(grep -c VMKEY /root/.ssh/authorized_keys)" == 1 ]] || fail "the VM's key is authorized on the host twice"

exit "${_fail}"
