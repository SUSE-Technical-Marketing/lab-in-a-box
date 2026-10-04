#!/bin/bash
# Part of lab-in-a-box, this is a simple library that defines functions used by other shell scripts.
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


# set -x -v

# ─── Lab definition preflight validation ──────────────────────────────────────
#
# validate_lab_definition  — call before setup_lab.sh / setup_vm.sh begins work.
#
# Reads:  inputFile    (path to the lab JSON)
#         VIRT_SRV     (libvirt URI, from lab_creation.cfg)
#         REMOTE_HOST  (hypervisor SSH host, from lab_creation.cfg)
#         ISO_LOC      (source image directory on the hypervisor)
#         VM_IMG_LOC   (VM disk directory on the hypervisor)
#         LAB_SETUP_PATH (ignition/combustion tree on the automation VM)
#
# Collects ALL issues and prints a full report. Exits 1 if any ERRORs were
# found. Warnings do not block execution but are always shown.
#
# Checks performed:
#   1.  JSON syntax
#   2.  Required top-level sections (nodes, common)
#   3.  common: required fields (ISO_IMAGE, VM_MEM, VM_DSK, VM_CPU)
#   4.  Per-node: required fields (myip), IP uniqueness, kcluster ref validity
#   5.  kcluster: required fields (clu_type, clu_rel, mydomain), addon scripts present
#   6.  Addon mandatory JSON fields (per-addon table)
#   7.  Hypervisor: source image exists, VM name not already defined
#   8.  Local: ignition/combustion templates present
#   9.  IP address reachability warning (ping, non-fatal)

function validate_lab_definition() {
        local _f="${1:-${inputFile}}"
        # Optional: pass a single VM hostname to restrict per-node checks to that node only
        local _target_node="${2:-}"
        local _errors=0
        local _warnings=0
        local _issues=""       # accumulates all messages

        local _E="\e[1;91mERROR\e[0m"
        local _W="\e[1;33mWARN \e[0m"

        _vld_err()  { _issues+="  [ERROR] $*\n"; ((_errors++));   }
        _vld_warn() { _issues+="  [WARN]  $*\n"; ((_warnings++)); }

        if [[ -n "${_target_node}" ]]; then
                echo -e "\e[1;97m── Preflight: validating '${_f}' for node '${_target_node}' ──\e[0m"
        else
                echo -e "\e[1;97m── Preflight: validating '${_f}' ──\e[0m"
        fi

        # ── 1. JSON syntax ────────────────────────────────────────────────────
        if ! jq empty < "${_f}" 2>/dev/null; then
                _vld_err "JSON syntax error in '${_f}' — run: jq . '${_f}'"
                # Cannot continue if JSON is broken
                echo -e "${_issues}"
                echo -e "\e[1;91m✗ Preflight FAILED\e[0m — ${_errors} error(s), ${_warnings} warning(s)"
                return 1
        fi

        # ── 2. Required top-level sections ────────────────────────────────────
        jq -e '.nodes'  < "${_f}" &>/dev/null || _vld_err "Missing required section: 'nodes'"
        jq -e '.common' < "${_f}" &>/dev/null || _vld_err "Missing required section: 'common'"

        # ── 3. common: required fields ────────────────────────────────────────
        local _iso
        _iso=$(jq -r '.common.ISO_IMAGE // ""' < "${_f}")
        [[ -z "${_iso}" ]] && _vld_err "common.ISO_IMAGE is required"

        for _req in VM_MEM VM_DSK VM_CPU; do
                local _val
                _val=$(jq -r ".common.${_req} // \"\"" < "${_f}")
                [[ -z "${_val}" ]] && _vld_err "common.${_req} is required"
        done

        # Validate disk bus type if specified
        local _dsk_bus
        _dsk_bus=$(jq -r '.common.VM_DSK_BUS // ""' < "${_f}")
        if [[ -n "${_dsk_bus}" ]]; then
                case "${_dsk_bus}" in
                        virtio|scsi|sata|usb|ide) ;;
                        *) _vld_err "common.VM_DSK_BUS '${_dsk_bus}' is invalid — must be one of: virtio, scsi, sata, usb, ide" ;;
                esac
        fi

        # Validate NIC model if specified
        local _net_model
        _net_model=$(jq -r '.common.VM_NET_MODEL // ""' < "${_f}")
        if [[ -n "${_net_model}" ]]; then
                case "${_net_model}" in
                        virtio|e1000|e1000e|rtl8139|vmxnet3|ne2k_pci) ;;
                        *) _vld_err "common.VM_NET_MODEL '${_net_model}' is invalid — must be one of: virtio, e1000, e1000e, rtl8139, vmxnet3, ne2k_pci" ;;
                esac
        fi

        # ── 4. Per-node checks ────────────────────────────────────────────────
        local _seen_ips="" _seen_macs=""
        local _kclusters_defined
        _kclusters_defined=$(jq -r '.kclusters // {} | keys[]' < "${_f}" 2>/dev/null)

        # When a target node is specified, check only that node; otherwise check all
        local _nodes_to_check
        if [[ -n "${_target_node}" ]]; then
                _nodes_to_check="${_target_node}"
        else
                _nodes_to_check=$(jq -r '.nodes | keys[]' < "${_f}" 2>/dev/null)
        fi

        while IFS= read -r _node; do
                [[ -z "${_node}" ]] && continue
                local _myip _mymac _kcluster
                _myip=$(jq -r ".nodes[\"${_node}\"].myip // \"\"" < "${_f}")
                _mymac=$(jq -r ".nodes[\"${_node}\"].mymac // \"\"" < "${_f}")
                _kcluster=$(jq -r ".nodes[\"${_node}\"].kcluster // \"\"" < "${_f}")

                # Required fields
                [[ -z "${_myip}" ]] && _vld_err "nodes.${_node}: 'myip' is required"

                # IP uniqueness
                if [[ -n "${_myip}" ]]; then
                        if echo "${_seen_ips}" | grep -qF " ${_myip} "; then
                                _vld_err "nodes.${_node}: IP ${_myip} is already assigned to another node"
                        else
                                _seen_ips+=" ${_myip} "
                        fi
                fi

                # MAC uniqueness within the file and against the hypervisor
                if [[ -n "${_mymac}" ]]; then
                        local _mymac_l
                        _mymac_l=$(echo "${_mymac}" | tr '[:upper:]' '[:lower:]')

                        # Duplicate within the JSON
                        if echo "${_seen_macs}" | grep -qF " ${_mymac_l} "; then
                                _vld_err "nodes.${_node}: MAC ${_mymac} is duplicated within this JSON"
                        else
                                _seen_macs+=" ${_mymac_l} "
                        fi

                fi

                # kcluster reference
                if [[ -n "${_kcluster}" ]]; then
                        echo "${_kclusters_defined}" | grep -qxF "${_kcluster}" || \
                                _vld_err "nodes.${_node}: references kcluster '${_kcluster}' which is not defined in 'kclusters'"
                fi

                # Hypervisor: VM name must not already exist
                if virsh --connect "${VIRT_SRV}" dominfo "${_node}" &>/dev/null; then
                        _vld_warn "nodes.${_node}: a VM with this name already exists on the hypervisor (will be destroyed and recreated)"
                fi

                # IP reachability (informational)
                if [[ -n "${_myip}" ]] && ping -c1 -W1 "${_myip}" &>/dev/null; then
                        _vld_warn "nodes.${_node}: IP ${_myip} is currently responding to ping — something may already be using it"
                fi

        done <<< "${_nodes_to_check}"

        # ── 5. kcluster checks ────────────────────────────────────────────────
        local _known_addons
        _known_addons=$(ls "$(dirname "$(command -v install_rancher 2>/dev/null || echo /usr/local/bin/install_rancher)")/install_"* 2>/dev/null \
                        | sed 's|.*/install_||' | tr '\n' ' ')
        # Fall back to PATH search
        [[ -z "${_known_addons}" ]] && \
                _known_addons=$(compgen -c install_ 2>/dev/null | sed 's/^install_//' | tr '\n' ' ')

        # When scoped to a target node, only check the kcluster that node belongs to
        local _clusters_to_check
        if [[ -n "${_target_node}" ]]; then
                local _node_kcluster
                _node_kcluster=$(jq -r ".nodes[\"${_target_node}\"].kcluster // \"\"" < "${_f}" 2>/dev/null)
                _clusters_to_check="${_node_kcluster}"
        else
                _clusters_to_check=$(jq -r '.kclusters // {} | keys[]' < "${_f}" 2>/dev/null)
        fi

        while IFS= read -r _clu; do
                [[ -z "${_clu}" ]] && continue
                local _ctype _crel _cdomain
                _ctype=$(jq -r ".kclusters[\"${_clu}\"].clu_type // \"\"" < "${_f}")
                _crel=$(jq -r ".kclusters[\"${_clu}\"].clu_rel // \"\"" < "${_f}")
                _cdomain=$(jq -r ".kclusters[\"${_clu}\"].mydomain // \"\"" < "${_f}")

                [[ -z "${_ctype}" ]]   && _vld_err "kclusters.${_clu}: 'clu_type' is required (rke2 or k3s)"
                [[ -z "${_crel}" ]]    && _vld_err "kclusters.${_clu}: 'clu_rel' is required (e.g. stable)"
                [[ -z "${_cdomain}" ]] && _vld_err "kclusters.${_clu}: 'mydomain' is required"

                # Addon scripts must be installed
                while IFS= read -r _addon; do
                        command -v "install_${_addon}" &>/dev/null || \
                                _vld_err "kclusters.${_clu}: addon '${_addon}' — script 'install_${_addon}' not found in PATH"
                done < <(jq -r ".kclusters[\"${_clu}\"].addons // [] | .[]" < "${_f}" 2>/dev/null)

        done <<< "${_clusters_to_check}"

        # Per-VM addon scripts must also be present
        while IFS= read -r _node; do
                [[ -z "${_node}" ]] && continue
                while IFS= read -r _addon; do
                        command -v "install_${_addon}" &>/dev/null || \
                                _vld_err "nodes.${_node}: addon '${_addon}' — script 'install_${_addon}' not found in PATH"
                done < <(jq -r ".nodes[\"${_node}\"].addons // [] | .[]" < "${_f}" 2>/dev/null)
        done <<< "${_nodes_to_check}"

        # ── 6. Per-addon field validation — delegate to each install script ──────
        # Each install_* script supports --validate <json> and exits non-zero
        # with [ERROR] lines if its own fields are invalid or missing.
        local _all_addons
        if [[ -n "${_target_node}" ]]; then
                local _node_kcluster
                _node_kcluster=$(jq -r ".nodes[\"${_target_node}\"].kcluster // \"\"" < "${_f}" 2>/dev/null)
                _all_addons=$(jq -r --arg node "${_target_node}" --arg clu "${_node_kcluster}" '
                    ([.nodes[$node].addons // [] | .[]] +
                     (if $clu != "" then [.kclusters[$clu].addons // [] | .[]] else [] end)) |
                    unique | .[]' < "${_f}" 2>/dev/null)
        else
                _all_addons=$(jq -r '
                    [.kclusters // {} | to_entries[].value.addons // [] | .[]] +
                    [.nodes // {}     | to_entries[].value.addons // [] | .[]] |
                    unique | .[]' < "${_f}" 2>/dev/null)
        fi

        while IFS= read -r _addon; do
                [[ -z "${_addon}" ]] && continue
                if command -v "install_${_addon}" &>/dev/null; then
                        local _addon_out
                        _addon_out=$("install_${_addon}" --validate "${_f}" 2>&1)
                        local _addon_rc=$?
                        if [[ ${_addon_rc} -ne 0 ]]; then
                                while IFS= read -r _line; do
                                        _vld_err "addon '${_addon}': ${_line#\[ERROR\] }"
                                done <<< "${_addon_out}"
                        fi
                fi
        done <<< "${_all_addons}"

        # ── 7. Hypervisor: source image exists ────────────────────────────────
        local _hv_ssh_ok=0
        if [[ -z "${REMOTE_HOST}" ]]; then
                _vld_err "REMOTE_HOST is not set — check /etc/lab_creation.cfg"
        elif [[ -z "${ISO_LOC}" ]]; then
                _vld_err "ISO_LOC is not set — check /etc/lab_creation.defaults"
        else
                local _ssh_test
                _ssh_test=$(ssh -o BatchMode=yes \
                                -o StrictHostKeyChecking=accept-new \
                                -o ConnectTimeout=5 \
                                -q "root@${REMOTE_HOST}" \
                                "echo ok" 2>&1)
                if [[ "${_ssh_test}" != "ok" ]]; then
                        _vld_err "Cannot reach hypervisor '${REMOTE_HOST}' via SSH — image checks skipped (${_ssh_test})"
                else
                        _hv_ssh_ok=1
                fi
        fi

        _check_image_on_hv() {
                local _img="${1}" _label="${2}"
                if [[ -z "${_img}" ]]; then return; fi
                if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -q \
                        "root@${REMOTE_HOST}" "test -f '${ISO_LOC}/${_img}'" 2>/dev/null; then
                        local _avail
                        _avail=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -q "root@${REMOTE_HOST}" \
                                "ls '${ISO_LOC}/'*.qcow2 2>/dev/null | xargs -n1 basename" 2>/dev/null \
                                | head -10 | tr '\n' '  ')
                        _vld_err "${_label}: image '${_img}' not found at ${REMOTE_HOST}:${ISO_LOC}/${_img}${_avail:+ — available: ${_avail}}"
                fi
        }

        if [[ "${_hv_ssh_ok}" == "1" ]]; then
                _check_image_on_hv "${_iso}" "common.ISO_IMAGE"

                while IFS= read -r _node; do
                        local _node_iso
                        _node_iso=$(jq -r ".nodes[\"${_node}\"].ISO_IMAGE // \"\"" < "${_f}")
                        [[ -n "${_node_iso}" ]] && \
                                _check_image_on_hv "${_node_iso}" "nodes.${_node}.ISO_IMAGE"
                done <<< "${_nodes_to_check}"
        fi

        # ── 8. Local: provisioning templates ─────────────────────────────────
        # Only check the config methods actually used in this definition
        local _needs_ign_cmb=0 _needs_cloud_init=0 _needs_install_iso=0
        while IFS= read -r _node; do
                local _cm
                _cm=$(jq -r ".nodes[\"${_node}\"].config_method // \"\"" < "${_f}")
                case "${_cm}" in
                        cloud-init)   _needs_cloud_init=1 ;;
                        install_iso)  _needs_install_iso=1 ;;
                        virt_customize|iso-cloud-init) ;;
                        *)            _needs_ign_cmb=1 ;;
                esac
        done < <(jq -r '.nodes | keys[]' < "${_f}" 2>/dev/null)

        if [[ "${_needs_ign_cmb}" == "1" ]]; then
                [[ -f "${LAB_SETUP_PATH}/ignition/template" ]] || \
                        _vld_err "Ignition template not found: ${LAB_SETUP_PATH}/ignition/template"
                [[ -f "${LAB_SETUP_PATH}/combustion/template" ]] || \
                        _vld_err "Combustion template not found: ${LAB_SETUP_PATH}/combustion/template"
        fi
        if [[ "${_needs_cloud_init}" == "1" ]]; then
                for _tpl in user-data network-config meta-data; do
                        [[ -f "${LAB_SETUP_PATH}/cloud-init/template_${_tpl}" ]] || \
                                _vld_err "cloud-init template not found: ${LAB_SETUP_PATH}/cloud-init/template_${_tpl}"
                done
        fi
        if [[ "${_needs_install_iso}" == "1" ]]; then
                local _itype
                _itype=$(jq -r '.common.install_type // ""' < "${_f}")
                if [[ -n "${_itype}" ]]; then
                        case "${_itype}" in
                                autoyast|kickstart|preseed|autoinstall) ;;
                                *) _vld_err "common.install_type '${_itype}' is invalid — must be: autoyast, kickstart, preseed, or autoinstall" ;;
                        esac
                fi
                # Resolve effective install_type for template check (auto-detect from ISO name if blank)
                local _eff_itype
                _eff_itype=$(ISO_IMAGE=$(jq -r '.common.ISO_IMAGE // ""' < "${_f}") install_type="${_itype}" _resolve_install_type)
                [[ -f "${LAB_SETUP_PATH}/install_iso/template_${_eff_itype}" ]] || \
                        _vld_err "install_iso template not found: ${LAB_SETUP_PATH}/install_iso/template_${_eff_itype}"
        fi

        # ── Report ────────────────────────────────────────────────────────────
        if [[ $(( _errors + _warnings )) -gt 0 ]]; then
                echo -e "${_issues}"
        fi

        if [[ "${_errors}" -gt 0 ]]; then
                echo -e "\e[1;91m✗ Preflight FAILED\e[0m — ${_errors} error(s), ${_warnings} warning(s). Fix the above before proceeding."
                return 1
        else
                echo -e "\e[1;92m✓ Preflight passed\e[0m — 0 errors, ${_warnings} warning(s)."
                return 0
        fi
}

# Print _msg with indentation based on _lvl depth
function show_nicer_messages() {
  local _indent=""
  local _i=0
  while (( _i < ${_lvl:-0} )); do _indent+="  "; ((_i++)); done
  echo -e "${_indent}${_msg}"
}

# Print _msg as an error and exit
function fail_with_error() {
  echo -e "\e[1;91mERROR:\e[0m ${_msg}" >&2
  exit 1
}



# Creates a VM image from a separate image and resizes it to the desired size.
# Validate or generate the MAC address for a VM.
#
# Reads:  mymac       (from load_vm_vars; may be empty)
#         _vm_name    (current VM name)
#         VIRT_SRV    (libvirt URI, e.g. qemu+ssh://root@hypervisor/system)
#
# Writes: mymac       (may be updated to a freshly generated address)
#         NETWORK     (rebuilt to reflect the final mymac)
#
# Behaviour:
#   - mymac empty   → generate a random locally-administered unicast MAC
#   - mymac set, not in use on the hypervisor → use as-is
#   - mymac set, already used by a different VM → generate a new random one + warn
function check_or_generate_mac() {
        # Collect all MACs currently in use on the hypervisor (one per line, lower-case)
        _used_macs=$(virsh --connect "${VIRT_SRV}" list --all --name 2>/dev/null | \
                     grep -v '^$' | \
                     xargs -I{} virsh --connect "${VIRT_SRV}" domiflist {} 2>/dev/null | \
                     awk '/^[[:space:]]/ {print tolower($5)}' | \
                     grep -E '^([0-9a-f]{2}:){5}[0-9a-f]{2}$' | \
                     sort -u)

        if [[ -z "${mymac}" ]]
        then
                # No MAC defined — generate a random one that is not already in use
                mymac=$(_generate_unused_mac "${_used_macs}")
                _msg="- No MAC specified for \e[1;91m\"${_vm_name}\"\e[0m — generated ${mymac}" show_nicer_messages
        else
                _mymac_lower=$(echo "${mymac}" | tr '[:upper:]' '[:lower:]')
                # Check if this MAC is owned by a different VM
                _owner=$(virsh --connect "${VIRT_SRV}" list --all --name 2>/dev/null | \
                         grep -v '^$' | while read -r _dom; do
                             virsh --connect "${VIRT_SRV}" domiflist "${_dom}" 2>/dev/null | \
                                 awk -v dom="${_dom}" -v mac="${_mymac_lower}" \
                                     'tolower($5)==mac {print dom}'
                         done | head -1)

                if [[ -n "${_owner}" && "${_owner}" != "${_vm_name}" ]]
                then
                        _old_mac="${mymac}"
                        echo -e "\e[1;33mWARNING:\e[0m MAC ${_old_mac} is already used by VM '${_owner}'." >&2
                        read -rp "  Generate a new MAC and update ${inputFile}? [y/N] " _answer </dev/tty
                        if [[ "${_answer}" =~ ^[Yy]$ ]]
                        then
                                mymac=$(_generate_unused_mac "${_used_macs}")
                                jq ".nodes[\"${_vm_name}\"].mymac = \"${mymac}\"" < "${inputFile}" > "${inputFile}.tmp" \
                                        && mv "${inputFile}.tmp" "${inputFile}"
                                _msg="- MAC updated to ${mymac} for \e[1;91m\"${_vm_name}\"\e[0m in ${inputFile}" show_nicer_messages
                        else
                                _msg="MAC conflict on \e[1;91m\"${_vm_name}\"\e[0m (${_old_mac} owned by '${_owner}') — aborting" fail_with_error
                        fi
                else
                        _msg="- MAC ${mymac} is available for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
                fi
        fi

        # Rebuild NETWORK with the final MAC and optional NIC model (default virtio)
        NETWORK="bridge=${BRIDGE:-br0},mac.address=${mymac},model=${VM_NET_MODEL:-virtio}"
}

# Generate a random locally-administered unicast MAC not present in _used (newline-separated list).
function _generate_unused_mac() {
        local _used="$1"
        local _candidate
        while true
        do
                # Byte 0: set bit1 (locally administered), clear bit0 (unicast) → X2/X6/XA/XE
                _b0=$(printf "%02x" $(( (RANDOM & 0xFC) | 0x02 )))
                _candidate="${_b0}:$(printf "%02x" $((RANDOM & 0xFF))):$(printf "%02x" $((RANDOM & 0xFF))):$(printf "%02x" $((RANDOM & 0xFF))):$(printf "%02x" $((RANDOM & 0xFF))):$(printf "%02x" $((RANDOM & 0xFF)))"
                echo "${_used}" | grep -qF "${_candidate}" || break
        done
        echo "${_candidate}"
}


function vm_is_reusable() {
    # Returns 0 when the VM should be kept, 1 when it must be destroyed and recreated.
    # Checks: running on hypervisor, MAC matches, DNS resolves to expected IP, SSH accessible.
    # Any failed check or command error returns 1 (safe default = recreate).

    # 1. VM must exist and be running
    local _state
    _state=$(virsh -c ${VIRT_SRV} domstate "${_vm_name}" 2>/dev/null)
    if [[ "${_state}" != "running" ]]; then
        _msg="  \e[1;33mKEEP CHECK\e[0m \e[1;91m\"${_vm_name}\"\e[0m: not running on hypervisor (state: ${_state:-not found}) — will recreate" show_nicer_messages
        return 1
    fi

    # 2. MAC must match (only when specified in the JSON)
    if [[ -n "${mymac}" ]]; then
        local _actual_mac
        _actual_mac=$(virsh -c ${VIRT_SRV} domiflist "${_vm_name}" 2>/dev/null | grep -i vnet |  \
                      awk '/^[[:space:]]/ {print tolower($5)}' | head -1)
        if [[ "$(echo "${mymac:-NOT_FOUND}" | tr '[:upper:]' '[:lower:]')" != "${_actual_mac:-NOT_FOUND}" ]]; then
            _msg="  \e[1;33mKEEP CHECK\e[0m \e[1;91m\"${_vm_name}\"\e[0m: MAC mismatch (want \"${mymac}\", got \"${_actual_mac:-none}\") — will recreate" show_nicer_messages
            return 1
        fi
    fi

    # 3. Hostname must resolve to the expected IP
    local _resolved_ip
    _resolved_ip=$(getent hosts "${_vm_name}" 2>/dev/null | awk '{print $1}' | head -1)
    if [[ "${_resolved_ip}" != "${myip}" ]]; then
        _msg="  \e[1;33mKEEP CHECK\e[0m \e[1;91m\"${_vm_name}\"\e[0m: IP mismatch (want \"${myip}\", DNS gives \"${_resolved_ip:-none}\") — will recreate" show_nicer_messages
        return 1
    fi

    # 4. SSH must be accessible with default credentials
    if ! ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes \
             root@"${_vm_name}" 'exit 0' &>/dev/null; then
        _msg="  \e[1;33mKEEP CHECK\e[0m \e[1;91m\"${_vm_name}\"\e[0m: SSH not accessible — will recreate" show_nicer_messages
        return 1
    fi

    return 0
}

function copy_vm_img() {
        # install_iso: disk is created empty by virt-install; nothing to copy or resize
        if [[ "${config_method}" == "install_iso" ]]; then
                _msg="- install_iso: skipping base image copy (disk created by virt-install)" show_nicer_messages
                return 0
        fi
        _msg="- Copy the image for the new VM \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
	ssh root@${REMOTE_HOST} "cp ${ISO_LOC}/${ISO_IMAGE} ${VM_IMG_LOC}/${_vm_name}.qcow2" || _msg="Failed to copy image for vm  \"${_vm_name}\"" fail_with_error
        _msg="- Resize to ${VM_DSK}G" show_nicer_messages
        ssh root@${REMOTE_HOST} "qemu-img resize -f qcow2 ${VM_IMG_LOC}/${_vm_name}.qcow2 ${VM_DSK}G" || _msg="Failed to resize VM image \"${_vm_name}\" to \"${VM_DSK}G\"" fail_with_error
}


# Check when a host becomes available via ssh
function check_ssh_conn() {
  ((_lvl++))
  _msg="Waiting for \e[1;91m\"${_vm_name}\"\e[0m to come online" show_nicer_messages
  _count=0
  ((_lvl++))
  while true
  do
    ((_count+=1))
    sleep ${_retry_interval:-2}
    if nc -z -w 2 ${_vm_name} ${_tcp_port:-22} &>/dev/null
    then
      _msg="\e[1;91m\"${_vm_name}\"\e[0m is online" show_nicer_messages
      break
    elif [[ "$?" == "127" ]]
    then
      _msg="ERROR - Netcat(nc) not installed" fail_with_error
    fi
    if [[ $_count -gt ${_retry_limit:-100} ]]
    then
      _msg="retry limit ( ${_retry_limit:-100} ) exceeded waiting for \e[1;91m\"${_vm_name}\"\e[0m to boot." fail_with_error
    fi
  done
  ((_lvl--))
  ((_lvl--))
}


# Creates ignition and combustion files used to setup the VM
function prepare_ign_and_cmb() {
        _msg="- Create ignition and combustion files for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
	cp ${LAB_SETUP_PATH}/combustion/{template,$_vm_name}
	cp ${LAB_SETUP_PATH}/ignition/{template,$_vm_name.ign}
	sed "s/TEMPLATE_HN/$_vm_name/g;s#ROOT_PWD_HASH#${ROOT_PWD_HASH}#g;s#ROOT_SSH_KEY#$(cat /root/.ssh/id_rsa.pub)#g" -i ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign

	sed "/#local vars/a mysource=${mysource}\nsourcepath=${sourcepath}\nmydns=${mydns}\nmyip=${myip}\nmymask=${mymask}\nmygw=${mygw}\nSUSE_email=${SUSE_email}\nSUSE_regcode=${SUSE_regcode}\nSUSE_url=${SUSE_url}" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
        sed "s#ROOT_SSH_KEY#$ROOT_SSH_KEY#g" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
}


# Creates cloud-init files used to setup the VM
function prepare_cloud-init() {
        ((_lvl++))
        _msg="- Create cloud-init files for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        ROOT_SSH_KEY=$(cat /root/.ssh/id_rsa.pub)
        ((_lvl++))
        for _type in user-data network-config meta-data
	do
          template_file=${LAB_SETUP_PATH}/cloud-init/template_${_type}
          process_templates >${LAB_SETUP_PATH}/cloud-init/${_vm_name}_${_type}
        done
        ((_lvl--))
        ((_lvl--))
}



# check which OS are we in and populate information
function find_OS() {
  # check which OS are we in
  if [[ -f /etc/os-release ]]
  then
    _os="`cat /etc/os-release | sed -n -e 's/^ID="\([-a-zA-Z].*\)"/\1/p'`"
    _version_id="`cat /etc/os-release | sed -n -e 's/^VERSION_ID="\([-a-zA-Z].*\)"/\1/p'`"
    _arch="`arch`"
  fi
}

# Install packages
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
function install_packages() {

#               eval "cat <<EOF
#$(cat ${template_file} )
#EOF
#"
       _msg="Installing standard packages" show_nicer_messages
          if [[ "${_os}" == "sles" ]]
          then
            BOOT_PACKAGES="vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
          elif [[ "${_os}" == "sle-micro" ]]
          then
            BOOT_PACKAGES="vim-small iptables NetworkManager-cloud-setup wget git"
          elif [[ "${_os}" == "opensuse-leap" ]]
          then
            BOOT_PACKAGES="vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
          else
            _msg="ERROR - OS not supported yet" fail_with_error
          fi
}


# ─── virt-customize provisioning ──────────────────────────────────────────────
#
# prepare_virt_customize — configure a QCOW2 image directly on the hypervisor
# without booting it (uses libguestfs).  Called when config_method=virt_customize.
#
# Autodetects the guest network stack:
#   network-scripts  (RHEL/CentOS ≤ 7, older Fedora)
#   nm-keyfile       (RHEL/CentOS 8+, Fedora, modern Debian/Ubuntu with NM)
#   ifupdown         (Debian ≤ 9, Ubuntu ≤ 18.04)
#   systemd-networkd (some minimal Debian/Ubuntu, Container Linux descendants)
#   netplan          (Ubuntu ≥ 18.04)
#
# Reads from env:  _vm_name  myip  mymask  mygw  mydns  mydomain
#                  VM_ROOT_PASS  ROOT_SSH_KEY  VM_IMG_LOC  REMOTE_HOST
#
function prepare_virt_customize() {
        # Delegates to prepare_virt_customize() in libs/lab_creation.py.
        # The Python function embeds all values via repr(), pipes the script to the
        # hypervisor over SSH, and runs virt-customize there entirely on the remote side.
        # This function runs locally on the automation VM.
        local _img="${VM_IMG_LOC}/${_vm_name}.qcow2"
        local _prefix="${mymask:-24}"

        local _pass_type _pass_b64
        if [[ -n "${VM_ROOT_PASS:-}" ]]; then
                _pass_type="plain"
                _pass_b64=$(printf '%s' "${VM_ROOT_PASS}" | base64 -w0)
        elif [[ -n "${ROOT_PWD_HASH:-}" ]]; then
                _pass_type="crypted"
                _pass_b64=$(printf '%s' "${ROOT_PWD_HASH}" | base64 -w0)
        else
                _pass_type="plain"
                _pass_b64=$(printf '%s' "linux" | base64 -w0)
                _msg="WARNING: neither VM_ROOT_PASS nor ROOT_PWD_HASH is set — using default password 'linux'" show_nicer_messages
        fi

        local _pubkey_b64=""
        if [[ -n "${ROOT_SSH_KEY:-}" && -f "${ROOT_SSH_KEY}.pub" ]]; then
                _pubkey_b64=$(base64 -w0 < "${ROOT_SSH_KEY}.pub")
        elif [[ -f ~/.ssh/id_rsa.pub ]]; then
                _pubkey_b64=$(base64 -w0 < ~/.ssh/id_rsa.pub)
        fi

        _msg="- Customising '${_vm_name}' via virt-customize (${myip}/${_prefix})" show_nicer_messages

        # Run the Python library function locally — it handles SSH to the hypervisor.
        # Values are passed as env vars; Python reads and decodes them.
        VC_REMOTE_HOST="${REMOTE_HOST}" \
        VC_IMG="${_img}" \
        VC_NAME="${_vm_name}" \
        VC_IP="${myip}" \
        VC_PREFIX="${_prefix}" \
        VC_GW="${mygw:-}" \
        VC_DNS="${mydns:-}" \
        VC_DOMAIN="${mydomain:-}" \
        VC_MAC="${mymac:-}" \
        VC_PASS_TYPE="${_pass_type}" \
        VC_PASS_B64="${_pass_b64}" \
        VC_PUBKEY_B64="${_pubkey_b64}" \
        VC_LIB_PATH="${_lib_path}" \
        python3 -c '
import os, sys, base64
sys.path.insert(0, os.environ.get("VC_LIB_PATH", "/usr/local/lib/lab_creation"))
from lab_creation import prepare_virt_customize as _vc
_vc(
    remote_host=os.environ["VC_REMOTE_HOST"],
    img_path=os.environ["VC_IMG"],
    vm_name=os.environ["VC_NAME"],
    ip=os.environ["VC_IP"],
    prefix=os.environ["VC_PREFIX"],
    gw=os.environ.get("VC_GW", ""),
    dns=os.environ.get("VC_DNS", ""),
    domain=os.environ.get("VC_DOMAIN", ""),
    mac=os.environ.get("VC_MAC", ""),
    pass_type=os.environ["VC_PASS_TYPE"],
    pass_val=base64.b64decode(os.environ["VC_PASS_B64"]).decode(),
    pubkey=base64.b64decode(os.environ["VC_PUBKEY_B64"]).decode() if os.environ.get("VC_PUBKEY_B64") else None,
)
' || { _msg="virt-customize failed for '${_vm_name}'" fail_with_error; }
}


# prepare_install_iso — render the answer file (AutoYaST/Kickstart/Preseed) for a VM.
# The rendered file is written to LAB_SETUP_PATH/install_iso/ which is already served
# over HTTP by the automation VM's web server.  Nothing needs to be copied to the hypervisor.
# Reads: _vm_name, myip, mymask, mygw, mydns, mydomain, ROOT_PWD_HASH, ROOT_SSH_KEY,
#        install_type (optional; auto-detected from ISO_IMAGE name if absent)
function _resolve_install_type() {
        local _itype="${install_type:-}"
        if [[ -z "${_itype}" ]]; then
                local _iso_lower
                _iso_lower=$(echo "${ISO_IMAGE:-}" | tr '[:upper:]' '[:lower:]')
                if   [[ "${_iso_lower}" =~ sles|suse|opensuse|sl-micro|sle-micro ]]; then _itype="autoyast"
                elif [[ "${_iso_lower}" =~ rhel|centos|rocky|almalinux|fedora ]];    then _itype="kickstart"
                elif [[ "${_iso_lower}" =~ ubuntu ]];                                 then _itype="autoinstall"
                elif [[ "${_iso_lower}" =~ debian ]];                                 then _itype="preseed"
                else _itype="autoyast"
                fi
        fi
        echo "${_itype}"
}

function prepare_install_iso() {
        local _itype
        _itype=$(_resolve_install_type)

        # Resolve public key content so templates can use $ROOT_SSH_PUBKEY directly
        local ROOT_SSH_PUBKEY=""
        if [[ -n "${ROOT_SSH_KEY:-}" && -f "${ROOT_SSH_KEY}.pub" ]]; then
                ROOT_SSH_PUBKEY=$(cat "${ROOT_SSH_KEY}.pub")
        elif [[ -f ~/.ssh/id_rsa.pub ]]; then
                ROOT_SSH_PUBKEY=$(cat ~/.ssh/id_rsa.pub)
        fi

        _msg="- Rendering ${_itype} answer file for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages

        if [[ "${_itype}" == "autoinstall" ]]; then
                # Ubuntu 22+ subiquity autoinstall — written directly (not via process_templates)
                # to avoid eval mangling $6$... password hashes.
                local _dir="${LAB_SETUP_PATH}/install_iso/${_vm_name}"
                mkdir -p "${_dir}"
                # The raw hash goes into the heredoc unescaped. A backslash that arrives through
                # a substituted variable is not consumed by the heredoc, so escaping the $ would
                # leave stray backslashes in the output.
                cat > "${_dir}/user-data" <<EOF
#cloud-config
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  network:
    network:
      version: 2
      ethernets:
        id0:
          match:
            macaddress: ${mymac}
          set-name: eth0
          dhcp4: no
          addresses:
            - ${myip}/${mymask}
          gateway4: ${mygw}
          nameservers:
            addresses: [${mydns}]
            search: [${mydomain}]
  storage:
    layout:
      name: lvm
  user-data:
    disable_root: false
    ssh_pwauth: true
    users:
      - name: root
        lock_passwd: false
        hashed_passwd: "${ROOT_PWD_HASH}"
        ssh_authorized_keys:
          - "${ROOT_SSH_PUBKEY}"
  ssh:
    install-server: true
    allow-pw: true
  late-commands:
    - mkdir -p /target/etc/ssh/sshd_config.d
    - printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' > /target/etc/ssh/sshd_config.d/99-lab.conf
EOF
                printf 'instance-id: %s\nlocal-hostname: %s\n' "${_vm_name}" "${_vm_name}" > "${_dir}/meta-data"
        else
                # For autoyast/kickstart/preseed use process_templates (eval-based).
                # Templates receive the raw ROOT_PWD_HASH. Escaping the $ would leave stray
                # backslashes in the rendered file.
                local _ext
                case "${_itype}" in
                        autoyast)  _ext="xml"     ;;
                        kickstart) _ext="ks"      ;;
                        preseed)   _ext="preseed" ;;
                esac
                local _tpl="${LAB_SETUP_PATH}/install_iso/template_${_itype}"
                local _out="${LAB_SETUP_PATH}/install_iso/${_vm_name}.${_ext}"
                [[ -f "${_tpl}" ]] || { _msg="install_iso template not found: ${_tpl}" fail_with_error; return 1; }
                mkdir -p "${LAB_SETUP_PATH}/install_iso"
                template_file="${_tpl}" process_templates > "${_out}"
        fi
}

# Copy the lab materials needed for the install to the hypervisor
function copy_to_hypervisor() {
        _msg="- Copy accross the lab setup materials" show_nicer_messages
	ssh root@${REMOTE_HOST} "[[ -d ${LAB_SETUP_PATH}/ ]] || mkdir -p ${LAB_SETUP_PATH}/" || _msg="failed creating new folder ${LAB_SETUP_PATH}" fail_with_error

        if [[ "$config_method" == "virt_customize" || "$config_method" == "install_iso" ]]; then
          # virt-customize / install_iso: no files need copying to the hypervisor
          return 0
        elif [[ "$config_method" == "" ]]
        then
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/{combustion,ignition}" || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/combustion/${_vm_name} root@${REMOTE_HOST}:${LAB_SETUP_PATH}/combustion/ || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign root@${REMOTE_HOST}:${LAB_SETUP_PATH}/ignition/ || _msg="" fail_with_error
          ssh  -q root@${REMOTE_HOST} "chmod 0644 ${LAB_SETUP_PATH}/ignition/* ${LAB_SETUP_PATH}/combustion/*" || _msg="" fail_with_error
        else
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/${config_method}" || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/${config_method}/${_vm_name}* root@${REMOTE_HOST}:${LAB_SETUP_PATH}/${config_method}/ || _msg="" fail_with_error
          ssh  -o StrictHostKeyChecking=accept-new root@${REMOTE_HOST} "cd ${LAB_SETUP_PATH}/${config_method}/; for i in ${_vm_name}*; do cp \${i} /tmp/\${i/${_vm_name}_/}; done ; rm -f ${VM_IMG_LOC}/${_vm_name}_ci.iso; mkisofs -J -l -R -V cidata -iso-level 3 -o /tmp/ci_${_vm_name}.iso /tmp/user-data /tmp/meta-data /tmp/network-config && mv /tmp/ci_${_vm_name}.iso ${VM_IMG_LOC}/${_vm_name}_ci.iso" || _msg="" fail_with_error
          
        fi

}

# Add hostname entry to the DNS server as well as the API DNS entry, TBI
function add_to_dns() {
        ((_lvl++))
        _msg="Add hostname DNS entry \e[1;91m\"${_vm_name}\"\e[0m \e[1;91m\"${myip}\"\e[0m" show_nicer_messages

        # this needs to be properly done, useful when working with different networks
        if [[ ! -f /var/lib/named/${mynet_reverse}.db ]]
        then
          touch /var/lib/named/${mynet_reverse}.db
          _msg="WARNING: Reverse network file not setup, /var/lib/named/${mynet_reverse}.db, please review and make sure it's correct" show_nicer_messages
        fi

        if [[ "${REMOTE_DNS_SERVERS}" != "" ]]
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "grep -qiF \"PTR     ${_vm_name}.\" /var/lib/named/${mynet_reverse}.db || echo \"${myip//*.}      IN  PTR     ${_vm_name}.\" >>/var/lib/named/${mynet_reverse}.db"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "grep -qi \"^${_vm_name//.*} \" /var/lib/named/${mydomain}.lan || echo \"${_vm_name//.*}         IN  A       ${myip}\" >>/var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} systemctl restart named
          done
        fi
        # NOTE: was `grep -qi "'${_vm_name}."` — a stray leading quote character meant this
        # never matched any line this function actually writes (real lines have no leading
        # quote), so the dedup check always failed and PTR records piled up as duplicates on
        # every re-run. Fixed to match the literal text that gets appended below.
        grep -qiF "PTR     ${_vm_name}." /var/lib/named/${mynet_reverse}.db || echo "${myip//*.}      IN  PTR     ${_vm_name}." >>/var/lib/named/${mynet_reverse}.db
        grep -qi "^${_vm_name//.*} " /var/lib/named/${mydomain}.lan || echo "${_vm_name//.*}         IN  A       ${myip}" >>/var/lib/named/${mydomain}.lan
        systemctl restart named
        ((_lvl--))
}


# function to add a service DNS giving preference to agent nodes.
function add_service_dns() {
        ((_lvl++))
        _count=0
        ((_lvl++))
        for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
        do
                clu_type_u=` echo ${clu_type} | tr '[:lower:]' '[:upper:]'`
                if [[ $(jq -r ".nodes[\"${_dns}\"][\"INSTALL_${clu_type_u}_TYPE\"]" < ${inputFile} ) == "agent" ]] && [[ $(jq -r ".nodes.\"${_dns}\".kcluster" < "${inputFile}") == "${clu_name}" ]]
                then
                        add_dns_to_named_rr
                        _count=1
                fi
        done


        ((_lvl--))
        if [[ "${_count}" == "0" ]]
        then
        	for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
	        do
                    if [[ $(jq -r ".nodes.\"${_dns}\".kcluster" < "${inputFile}") == "${clu_name}" ]]
                    then
			add_dns_to_named_rr
                    fi
		done
		_msg="DNS ${_dns_entry} added to point to all nodes of the cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
	else
		_msg="DNS ${_dns_entry} added to point to agent nodes of the cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
        fi
        systemctl restart named
        ((_lvl--))
}


# Adds a DNS to Bind for round-robing balancing.
function add_dns_to_named_rr() {
        ((_lvl++))
	_myip=$(jq -r ".nodes[\"${_dns}\"][\"myip\"]" < ${inputFile} )

        if grep -qP "^${_dns_entry}\tIN A  ${_myip}$" /var/lib/named/${mydomain}.lan 2>/dev/null
        then
                _msg="- DNS entry \e[1;91m\"${_dns_entry} → ${_myip}\"\e[0m already correct, skipping" show_nicer_messages
                ((_lvl--))
                return 0
        fi

        _msg="- add DNS entry \e[1;91m\"${_dns_entry}.${mydomain}\"\e[0m" show_nicer_messages

        if [[ "${REMOTE_DNS_SERVERS}" != "" ]]
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- "grep -qP \"^${_dns_entry}\tIN A  ${_myip}$\" /var/lib/named/${mydomain}.lan" && continue
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- "sed \"/${_dns_entry}\tIN A  ${_myip}/d\" -i /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- "echo -e \"${_dns_entry}\tIN A  ${_myip}\" >> /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- systemctl restart named
          done
        fi
	sed "/${_dns_entry}\tIN A  ${_myip}/d" -i /var/lib/named/${mydomain}.lan
	echo -e "${_dns_entry}\tIN A  ${_myip}" >> /var/lib/named/${mydomain}.lan
        ((_lvl--))
}

# Deletes a DNS entry from Bind
function del_from_dns() {
        _msg="- Delete DNS entries for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        if [[ "${REMOTE_DNS_SERVERS}" != "" ]]        
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "sed \"/${myip//*.}      IN  PTR     ${_vm_name}./d\" -i /var/lib/named/${mynet_reverse}.db"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "sed \"/${_vm_name//.*}         IN  A       ${myip}/d\" -i /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} systemctl restart named
          done
        fi


	sed "/${myip//*.}      IN  PTR     ${_vm_name}./d" -i /var/lib/named/${mynet_reverse}.db
	sed "/${_vm_name//.*}         IN  A       ${myip}/d" -i /var/lib/named/${mydomain}.lan
	systemctl restart named
        ((_lvl--))
}


# Creates a VM on a KVM hypervisor
function create_vm() {
        ((_lvl++))
        _msg="Create virtual machine \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        ((_lvl++))
        if [[ "${extra_fs}" != "" ]]
        then
          for _fs in ${extra_fs}
          do
            _filesystems="${_filesystems} --filesystem ${fs}"
          done
        else
          _msg="${_vm_name} has no extra volumes" show_nicer_messages
        fi
        if [[ "${extra_dsk}" != "" ]]
        then
          for _dsk in ${extra_dsk}
          do
            # Per-disk bus override: "path,bus=scsi" or "UUID=xxx,bus=sata"
            _dsk_bus_override=""
            if [[ "${_dsk}" =~ ,bus=([a-z]+) ]]; then
              _dsk_bus_override="${BASH_REMATCH[1]}"
            fi
            _dsk_path="${_dsk%%,bus=*}"
            _dsk_path="${_dsk_path//,*/}"
            if [[ "${_dsk_path}" =~ "UUID" ]]
            then
              _dsk_path=`$ssh_command "lsblk -o UUID,PATH | grep ${_dsk_path//UUID=} | cut -d' ' -f2"`
            fi
            _extra_bus="${_dsk_bus_override:-${VM_DSK_BUS:-virtio}}"
            _disks="${_disks} --disk path=${_dsk_path},bus=${_extra_bus}"
          done
        else
          _msg="\e[1;91m\"${_vm_name}\"\e[0m has no extra disks" show_nicer_messages
        fi
        _msg="_disks:  ${_disks}" show_nicer_messages
        ((_lvl--))

        # Normalise VM_BOOT: "uefi=off" is not a valid virt-install value;
        # map it (and empty) to the correct flag form.
        case "${VM_BOOT:-uefi}" in
          uefi=off|bios|legacy) _boot_flag="firmware=bios" ;;
          uefi)                  _boot_flag="uefi"          ;;
          *)                     _boot_flag="${VM_BOOT}"    ;;
        esac

        # If not config method specified we use ignition+combustion files
        if [[ "$config_method" == "" ]]
        then
          virt-install --connect ${VIRT_SRV} \
	       --name  ${_vm_name} \
               --autostart \
               --boot ${_boot_flag} \
	       --vcpus ${VM_CPU}  \
	       --memory ${VM_MEM} \
	       --os-variant=${VM_OSVARIANT:-slem5.4} \
	       --import \
	       --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,bus=${VM_DSK_BUS:-virtio},boot.order=1 \
               --import ${_filesystems} ${_disks} \
	       --graphics spice,listen=0.0.0.0 \
	       --network "${NETWORK}" \
	       --noautoconsole \
	       --qemu-commandline="-fw_cfg name=opt/com.coreos/config,file=${LAB_SETUP_PATH}/ignition/${IGN_FILE} -fw_cfg name=opt/org.opensuse.combustion/script,file=${LAB_SETUP_PATH}/combustion/${COM_FILE}"
          [[ "$?" != "0" ]] && _msg="virt-install failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error
        elif [[ "$config_method" == "install_iso" ]]
        then
          # Full OS installation from installer ISO (AutoYaST / Kickstart / Preseed).
          # --location boots the installer kernel+initrd directly from the ISO.
          # --wait -1 blocks until the installer powers the VM off (install complete).
          # After that we start the VM ourselves so setup_lab.sh can continue normally.
          local _itype="${install_type:-}"
          _itype=$(_resolve_install_type)
          local _location_arg _extra_args
          case "${_itype}" in
                autoyast)
                        _location_arg="${ISO_LOC}/${ISO_IMAGE}"
                        _extra_args="autoyast=http://${mydns}/lab_creation/install_iso/${_vm_name}.xml"
                        ;;
                kickstart)
                        _location_arg="${ISO_LOC}/${ISO_IMAGE}"
                        _extra_args="inst.ks=http://${mydns}/lab_creation/install_iso/${_vm_name}.ks inst.sshd"
                        ;;
                preseed)
                        _location_arg="${ISO_LOC}/${ISO_IMAGE}"
                        _extra_args="auto=true priority=critical url=http://${mydns}/lab_creation/install_iso/${_vm_name}.preseed"
                        ;;
                autoinstall)
                        # Ubuntu 22+ subiquity: boot from --cdrom and provide a second
                        # "cidata" seed CDROM.  Ubuntu 23.10+ starts autoinstall
                        # automatically when the nocloud datasource is present — no kernel
                        # cmdline arg needed, so --location + ,kernel= is not required.
                        local _seed_local="/tmp/seed_${_vm_name}_$$.iso"
                        local _seed_remote="${VM_IMG_LOC}/seed_${_vm_name}.iso"
                        mkisofs -J -l -R -V cidata -iso-level 3 \
                            -o "${_seed_local}" \
                            "${LAB_SETUP_PATH}/install_iso/${_vm_name}/user-data" \
                            "${LAB_SETUP_PATH}/install_iso/${_vm_name}/meta-data" \
                        || { _msg="mkisofs seed failed for '${_vm_name}'" fail_with_error; return 1; }
                        scp -o StrictHostKeyChecking=accept-new "${_seed_local}" \
                            "root@${REMOTE_HOST}:${_seed_remote}" \
                        || { rm -f "${_seed_local}"; _msg="scp seed failed for '${_vm_name}'" fail_with_error; return 1; }
                        rm -f "${_seed_local}"
                        _msg="- Installing Ubuntu via autoinstall + seed CDROM (blocks until installer finishes)…" show_nicer_messages
                        virt-install --connect ${VIRT_SRV} \
                             --name  ${_vm_name} \
                             --vcpus ${VM_CPU} \
                             --memory ${VM_MEM} \
                             --os-variant=${VM_OSVARIANT:-ubuntu24.04} \
                             --cdrom "${ISO_LOC}/${ISO_IMAGE}" \
                             --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,bus=${VM_DSK_BUS:-virtio},boot.order=2 \
                             --disk path="${_seed_remote}",device=cdrom,readonly=on \
                             ${_disks} \
                             --graphics spice,listen=0.0.0.0 \
                             --network "${NETWORK}" \
                             --noautoconsole \
                             --wait -1 \
                        || { ssh "root@${REMOTE_HOST}" "rm -f '${_seed_remote}'" 2>/dev/null; _msg="virt-install (autoinstall) failed for '${_vm_name}'" fail_with_error; return 1; }
                        ssh "root@${REMOTE_HOST}" "rm -f '${_seed_remote}'" 2>/dev/null || true
                        virsh --connect ${VIRT_SRV} autostart ${_vm_name}
                        virsh --connect ${VIRT_SRV} start ${_vm_name}
                        return 0
                        ;;
          esac
          _msg="- Installing via ${_itype} (this will block until the installer finishes)…" show_nicer_messages
          virt-install --connect ${VIRT_SRV} \
               --name  ${_vm_name} \
               --vcpus ${VM_CPU} \
               --memory ${VM_MEM} \
               --os-variant=${VM_OSVARIANT:-slem5.4} \
               --location "${_location_arg}" \
               --extra-args "${_extra_args} console=ttyS0,115200n8" \
               --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,bus=${VM_DSK_BUS:-virtio},boot.order=1 \
               ${_disks} \
               --graphics spice,listen=0.0.0.0 \
               --network "${NETWORK}" \
               --noautoconsole \
               --wait -1 \
          || { _msg="virt-install (install_iso) failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error; return 1; }
          # Installer powered off the VM — bring it back up and mark autostart
          virsh --connect ${VIRT_SRV} autostart ${_vm_name}
          virsh --connect ${VIRT_SRV} start ${_vm_name}

        elif [[ "$config_method" == "iso-cloud-init" ]]
        then
          if [[ "$vcluster" == "harvester" ]]
          then
            _boot_params="harvester.install.config_url=http://10.100.0.10/harvester/config-create.yaml"
          fi

        elif [[ "$config_method" == "virt_customize" ]]
        then
          # Image already fully configured by prepare_virt_customize — boot it directly,
          # no provisioning kernel args, no extra cdrom.
          virt-install --connect ${VIRT_SRV} \
               --name  ${_vm_name} \
               --import \
               --autostart \
               --boot ${_boot_flag} \
               --vcpus ${VM_CPU}  \
               --memory ${VM_MEM} \
               --os-variant=${VM_OSVARIANT:-slem5.4} \
               --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,bus=${VM_DSK_BUS:-virtio},boot.order=1 \
               ${_filesystems} ${_disks} \
               --graphics spice,listen=0.0.0.0 \
               --network "${NETWORK}" \
               --noautoconsole
          [[ "$?" != "0" ]] && _msg="virt-install failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error

        elif [[ "$config_method" == "cloud-init" ]]
        then
          virt-install  --connect ${VIRT_SRV} \
               --name  ${_vm_name} \
               --import \
               --autostart \
               --boot ${_boot_flag} \
               --vcpus ${VM_CPU}  \
               --memory ${VM_MEM} \
               --os-variant=${VM_OSVARIANT:-slem5.4} \
               --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,bus=${VM_DSK_BUS:-virtio},boot.order=1 \
               --import ${_filesystems} ${_disks} \
               --graphics spice,listen=0.0.0.0 \
               --network "${NETWORK}" \
               --noautoconsole \
               --disk ${VM_IMG_LOC}/${_vm_name}_ci.iso,device=cdrom
          [[ "$?" != "0" ]] && _msg="virt-install for cloud-init failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error
          _msg="  - Waiting 3 minutes" show_nicer_messages
          sleep 180

          if [[ "$salt_states" != "" ]]
          then
            setup_salt
            _msg="  - applying salt states" show_nicer_messages
            for _salt_state in ${salt_states}
            do
              salt-ssh -i -v --update-roster  ${_vm_name} state.apply ${_salt_state}
            done
          fi

         _msg="  - eject media" show_nicer_messages
          # NOTE: was missing the "/" between VM_IMG_LOC and the filename, so this never
          # matched the disk's actual attached path (${VM_IMG_LOC}/${_vm_name}_ci.iso,
          # as used everywhere else this path is built) and would silently fail to eject.
          virsh --connect ${VIRT_SRV} change-media ${_vm_name} --eject ${VM_IMG_LOC}/${_vm_name}_ci.iso

         _msg="- reboot node" show_nicer_messages
          virsh --connect ${VIRT_SRV} reboot ${_vm_name}
        fi
        ((_lvl--))
}

# Deletes a VM from a KVM hypervisor
function delete_vm() {
        ((_lvl++))
        _msg="- Delete VM \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        virsh -c ${VIRT_SRV} undefine --nvram "${_vm_name}"
	virsh -c ${VIRT_SRV} destroy  "${_vm_name}" 2>/dev/null
	virsh -c ${VIRT_SRV} undefine "${_vm_name}" --nvram --remove-all-storage
        ((_lvl--))
}

# Removes the VM ssh key from the known hosts to avoid warnings.
function clean_ssh_keys() {
	# Cleaup SSH keys
	ssh-keygen -f "${HOME}/.ssh/known_hosts" -R "${myip}"
}


# creates a config directory for operating kubernetes, TBI
function prepare_local_as_kubeclient() {
	# setup as client
	[ -d ~/.kube ] || mkdir -p ~/.kube
}





# Load VM variables
function load_vm_vars() {
        # grab the local device
        _default_dev="`nmcli -t -f GENERAL.DEVICE device show |grep -m 1 -v ' lo \| br-.*\| docker[0-9]* '`"
        _default_dev=${_default_dev//*:}
        clu_name=""
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
              export ${_key}="$(jq -r .common[\"${_key}\"] < ${inputFile} )"
        done
        for _key in $(jq -r ".nodes[\"${_vm_name}\"] | to_entries[].key" < ${inputFile} )
        do
              # if kcluster is defined it means it's part of a kubernetes cluster
              if [[ "${_key}" == "kcluster" ]]
              then
                export clu_name=$(get_vm_kcluster)
              else
                export ${_key}="$(jq -r .nodes[\"${_vm_name}\"][\"${_key}\"] < ${inputFile} )"
              fi
        done
        # make live easier with some autogenerated defauls
        # many assumptions.
        if [[ "${mydns}" == "" ]]
        then
           mydns=`nmcli -t -f IP4.DNS device show ${_default_dev} |egrep -m 1 -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'`
        fi
        if [[ "${mygw}" == "" ]]
        then
           mygw=`ip route list to default |egrep -m 1 -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'`
	fi
	if [[ "${mynet_reverse}" == "" ]]
        then
           mynet_reverse=`echo "${myip}" |sed 's/\([0-9]*\)\.\([0-9]*\)\.\([0-9]*\)\.\([0-9]*\)/\3.\2.\1/'`
        fi
        if [[ "${mymask}" == "" ]]
        then
          mymask=$(ipcalc -p "`ip -o -f inet addr show ${_default_dev} | egrep -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\/[0-9]{1,2}'`"|cut -d= -f2)
        fi
        if [[ "${mydomain}" == "" ]]
        then
          mydomain="$(hostname | sed "s/`hostname -s`\.//")"
        fi
}



# Setup SALT
function setup_salt() {
   [ -d ${HOME}/salt-ssh/states ] || mkdir -p ${HOME}/salt-ssh/states
   cat >${HOME}/salt-ssh/roster <<-EOF
managed:
  host: ${_vm_name}
  user: root
  sudo: False
  priv: ${HOME}/.ssh/id_rsa
EOF

  for _state in ${salt_states}
  do
    template_file=${LAB_SETUP_PATH}/salt-ssh/${_state}
    process_templates >${HOME}/salt-ssh/states/${_state}
  done

}


# Setup Helm
function setup_helm() {
	# add helm
        _msg="Setup Helm on cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
        if [[ "$online" == "1" ]]
	then
	        $ssh_command "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash" || _msg="$ssh_command \"curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash\"" fail_with_error
	else
		$ssh_command 'curl http://automation/helm/install_helm.sh | bash -' || _msg="$ssh_command 'curl http://automation/helm/install_helm.sh | bash -'" fail_with_error
	fi
}


function helm_repo_add() {
        _msg="Adding helm repository \"${_repo_name}\"" show_nicer_messages
	$ssh_command "helm repo add ${_repo_name} ${_repo_url}" || _msg="Failed adding a helm repo ${_repo_name} ${_repo_url}" fail_with_error
        $ssh_command "helm repo update" || _msg="Failed updating helm repos" fail_with_error
}



# Generic load Vars function

function _load_vars() {
	if jq -r ".${_section} | to_entries[].key " < ${inputFile} &>/dev/null
	then
		for _key in $(jq -r ".${_section} | to_entries[].key " < ${inputFile} )
	        do  
	            value=$(jq -r ".${_section}[\"${_key}\"]" < ${inputFile} )
	            export ${_key}="${value}"
	        done
	else
		_msg="No variables defined for ${_section}" show_nicer_messages
	fi
}


function kk() { 
  if jq -r ".${1} | to_entries[].key " < ${inputFile} &>/dev/null
  then
    for _key in $(jq -r ".${1} | to_entries[].key " < ${inputFile} )
    do
      keys="${keys} \"${_key}\""
    done
  else
    _msg="No variables defined for ${_section}" show_nicer_messages
  fi
}



function ko() {
  _counter=$2
  _itemlist=$1
  _mypath=''
#  echo "D: START  -_itemlist=\"$_itemlist\" ; _counter=$_counter"
  for i in $1
  do
#    echo "D: i=$i"
    if [[ "$i" == "any" ]]; then
#      echo "D: i is any"
      _itemlist=${_itemlist/ $i}
      keys=''
#      echo "D1: _mypath=${_mypath}"
      kk ${_mypath}
      for e in $keys
      do
#        echo "D: e=$e ; _itemlist=\"${_itemlist}\" ; $_itemlist/ any/$e=\"${_itemlist/ any/$e}\"; _counter=${_counter}"
        ko "${_itemlist} ${e}" ${2}
        _itemlist=${_itemlist/ $e}
        ((_counter-=1))
      done
      break
    else
      if [[ "${_counter}" -eq 1  ]]
      then
#        echo "D: _counter is 1 ## _mypath: ${_mypath} ; e: ${e}"
        value=$(jq -r ".${_mypath}[\"${i}\"]" < ${inputFile} )
        echo "${_mypath}=$value"
      else
#        echo "D: _counter is $_counter"
        _mypath="${_mypath}[\"${i}\"]"
        ((_counter-=1))
      fi
    fi
  done
}


function load_iter_vars() {
        # We load the common settings
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
              export ${_key}="$(jq -r .common[\"${_key}\"] < ${inputFile} )"
        done
        # we interate according to the input
        # number of arguments passed $#
        
        for _i in $@
        do
           if [[ "${_i}" == "*" ]]
           then
             ddd
           else
             uuu
           fi
        done 
        for _key in $(jq -r ".kclusters[\"${clu_name}\"] | to_entries[].key" < ${inputFile} )
        do
              export ${_key}="$(jq -r .kclusters[\"${clu_name}\"][\"${_key}\"] < ${inputFile} )"
        done
}




# Load rancher related variables.
function load_rancher_vars() {
        _section="rancher"
        _load_vars
}


# Load Jenkins related variables.
function load_jenkins_vars() {
       _section="jenkins"
       _load_vars
}

# Load ArgoCD related variables.
function load_argocd_vars() {
       _section="argocd"
       _load_vars
}

 
  # Load Longhorn related variables.
function load_lh_vars() {
       _section="longhorn"
       _load_vars
}

# Load NeuVector related variables.
function load_nv_vars() {
       _section="neuvector"
       _load_vars
}

# Load nginx Ingress Controller related variables.
function load_nginx_vars() {
       _section="nginx"
       _load_vars
}

# Load Traefik related variables.
function load_traefik_vars() {
       _section="traefik"
       _load_vars
}

# Load Harbor container registry related variables.
function load_harbor_vars() {
       _section="harbor"
       _load_vars
}

# Load Keycloak related variables.
function load_keycloak_vars() {
       _section="keycloak"
       _load_vars
}

# Load Fluentd related variables.
function load_fluentd_vars() {
       _section="fluentd"
       _load_vars
}

# Load PostgreSQL related variables.
function load_postgresql_vars() {
       _section="postgresql"
       _load_vars
}

# Load CoreDNS related variables.
function load_coredns_vars() {
       _section="coredns"
       _load_vars
}

# Load Linkerd related variables.
function load_linkerd_vars() {
       _section="linkerd"
       _load_vars
}

# Load Istio related variables.
function load_istio_vars() {
       _section="istio"
       _load_vars
}

# Load Kubewarden related variables.
function load_kubewarden_vars() {
       _section="kubewarden"
       _load_vars
}

# Load Fluid related variables.
function load_fluid_vars() {
       _section="fluid"
       _load_vars
}

# Load Kucero related variables.
function load_kucero_vars() {
       _section="kucero"
       _load_vars
}

# Load Ollama related variables.
function load_ollama_vars() {
       _section="ollama"
       _load_vars
}

# Load DeepSeek related variables.
function load_deepseek_vars() {
       _section="deepseek"
       _load_vars
}

# Load kagent related variables.
function load_kagent_vars() {
       _section="kagent"
       _load_vars
}

# Load Trento related variables.
function load_trento_vars() {
       _section="trento"
       _load_vars
}

function load_smlm_vars() { _section="smlm"; _load_vars; }

# Load SUSE Multi-Linux Manager proxy related variables.
function load_smlm_proxy_vars() { _section="smlm_proxy"; _load_vars; }

# Load Uyuni related variables.
function load_uyuni_vars() {
       _section="uyuni"
       _load_vars
}

# Load Phoebe related variables.
function load_phoebe_vars() {
       _section="phoebe"
       _load_vars
}

# Load ComplianceAsCode related variables.
function load_complianceascode_vars() {
       _section="complianceascode"
       _load_vars
}

# Load KIWI related variables.
function load_kiwi_vars() {
       _section="kiwi"
       _load_vars
}

# Load StackState StackPack related variables.
function load_stackpack_vars() {
       _section="stackpack"
       _load_vars
}

# Load SUSE Application Collection related variables.
function load_appcollection_vars() {
       _section="appcollection"
       _load_vars
}

# Load Google Gemini proxy related variables.
function load_gemini_vars() {
       _section="gemini"
       _load_vars
}


# Inspired from https://stackoverflow.com/questions/2914220/bash-templating-how-to-build-configuration-files-from-templates-with-bash#11050943
function process_templates() {
       eval "cat <<EOF
$(cat ${template_file} )
EOF
"

}

# Check if $_needle exists in the space-separated list $_haystack
function check_exists() {
    [[ " ${_haystack} " == *" ${_needle} "* ]]
}

. ${_lib_path}/k8s_functions.bash

function reboot_vm() {
        virsh -c ${VIRT_SRV} reboot "${_vm_name}"
        if ! virsh -c ${VIRT_SRV} event "${_vm_name}" --event lifecycle --timeout 120 &>/dev/null; then
          _msg="- \e[1;91m\"${_vm_name}\"\e[0m did not reboot — forcing power cycle" show_nicer_messages
          virsh -c ${VIRT_SRV} reset "${_vm_name}"
        fi
}


