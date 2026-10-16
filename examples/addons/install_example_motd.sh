#!/bin/bash
# Part of lab-in-a-box: example add-on written in bash. It sets the message of the day (/etc/motd) on a VM.
# See docs/addons.html (Add-on developers' guide). Copy it to scripts/install_<name>.sh to start a new add-on.
# License: GPLv3
#
# Contract: --schema [json|yaml], --capabilities, --validate <lab>, --check <lab>, --version, --help, or <lab> to
# install. The install run gets the lab definition as $1 and the VM in $_vm_name. Needs jq and root SSH to the VM.
# --check compares what is deployed with the lab: exit 0 it matches, 1 it does not, 2 no check implemented.
set -euo pipefail

_version="__LABVERSION__"
_section="example_motd"

_capabilities='{"targets": ["vm", "baremetal"], "layers": ["os-native"], "requires_kubernetes": null, "aux_services": [], "versions": {}, "check": true}'

_schema() {
    cat <<JSON
{"schema_version": "1.0", "addon": "${_section}", "section": "${_section}",
 "description": "Message of the day shown at login on a VM",
 "fields": [
  {"name": "example_motd_message", "type": "string", "required": true, "default": "",
   "description": "Text written to /etc/motd"},
  {"name": "example_motd_append", "type": "boolean", "required": false, "default": "false",
   "description": "Append to /etc/motd instead of replacing it"}
 ],
 "capabilities": ${_capabilities}}
JSON
}

# The add-on's config on VM $2 of lab $1: its section, with the VM's own {"example_motd": {...}} addons entry on top.
_config() {
    jq --arg vm "$2" --arg s "${_section}" \
        '(.[$s] // {}) + ([.nodes[$vm].addons[]? | objects | .[$s] // empty] | add // {})' "$1"
}

case "${1:-}" in
    # JSON is valid YAML, so "--schema yaml" prints the same text.
    --schema|--input-definition) _schema ;;
    --capabilities) echo "${_capabilities}" ;;
    --validate)
        [[ -n "${2:-}" ]] || { echo "[ERROR] --validate requires a lab definition file"; exit 1; }
        # Only labs that list the add-on carry its section; there the message is required.
        if jq -e --arg s "${_section}" 'has($s)' "$2" >/dev/null && \
           ! jq -e --arg s "${_section}" '.[$s].example_motd_message // "" | length > 0' "$2" >/dev/null; then
            echo "[ERROR] ${_section}.example_motd_message is required"
            exit 1
        fi
        ;;
    --check)
        [[ -n "${2:-}" ]] || { echo "[FAIL] --check requires a lab definition file"; exit 1; }
        if ! jq -e --arg s "${_section}" 'has($s)' "$2" >/dev/null; then
            echo "[OK] this lab has no ${_section} section, so there is nothing to check"
            exit 0
        fi
        _vm="${_vm_name:?_vm_name is not set (setup_lab.py sets it)}"
        _want=$(_config "$2" "${_vm}" | jq -r '.example_motd_message // ""')
        _have=$(ssh -o StrictHostKeyChecking=accept-new -q "root@${_vm}" "cat /etc/motd" 2>/dev/null || true)
        if [[ "${_have}" == *"${_want}"* ]]; then
            echo "[OK] /etc/motd on ${_vm} holds the configured message"
            exit 0
        fi
        echo "[FAIL] /etc/motd on ${_vm}: expected it to hold '${_want}'"
        exit 1
        ;;
    --version|-v) echo "install_${_section} ${_version}" ;;
    --help)
        echo "Usage: install_${_section} <lab.json>   (the VM is taken from \$_vm_name)"
        sed -n '2,8p' "$0"
        ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)
        _lab="${1:?usage: install_${_section} <lab.json>}"
        _vm="${_vm_name:?_vm_name is not set (setup_lab.py sets it)}"
        _cfg=$(_config "${_lab}" "${_vm}")
        _message=$(jq -r '.example_motd_message // ""' <<<"${_cfg}")
        [[ -n "${_message}" ]] || { echo "${_section}.example_motd_message is required" >&2; exit 1; }
        _redirect=">"
        [[ "$(jq -r '.example_motd_append // "false"' <<<"${_cfg}")" == "true" ]] && _redirect=">>"
        printf '%s\n' "${_message}" | ssh -o StrictHostKeyChecking=accept-new -q "root@${_vm}" "cat ${_redirect} /etc/motd"
        echo "/etc/motd set on ${_vm}"
        ;;
esac
