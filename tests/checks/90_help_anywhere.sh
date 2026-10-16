#!/bin/bash
# Every deployed entry point prints its help and exits 0 for --help or -h in any argument position, before it reads
# any configuration or acts on the other arguments. Add-ons are the files install_automation_node_scripts.sh deploys
# (the last in sorted order per install_<name>), among the files git tracks. Runs with no lab_creation configuration present.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo="${PWD}"
export PATH="${_repo}/scripts:${PATH}"

_work="$(mktemp -d)"
trap 'rm -rf "${_work}"' EXIT
echo '{"nodes":{},"common":{}}' > "${_work}/lab.json"

declare -A _addons=()
while IFS= read -r f; do
    _name="${f##*/}"
    _addons["${_name%%.*}"]="${f}"
done < <(git -c safe.directory="${_repo}" ls-files 'scripts/install_*' | sort)

_entry_points=(setup_lab.py setup_vm.py destroy_vm.py destroy_lab.py lab_schema refresh_hypervisor_status.py
               setup_harvester_cluster.py build_lab_usb.py setup_credentials.py vm_power.py check_addon.py
               migrate_config.py schema_versions.py lab_builder_helper.py pushDockerImage.sh overlay_ctl.py)

failed=0
run_case() {
    local script="$1"; shift
    local out rc
    if [[ "${script}" == *.py && ! -x "${script}" ]]; then
        out="$(cd "${_work}" && timeout 30 python3.11 "${_repo}/${script}" "$@" 2>&1 </dev/null)"; rc=$?
    else
        out="$(cd "${_work}" && timeout 30 "${_repo}/${script}" "$@" 2>&1 </dev/null)"; rc=$?
    fi
    if [[ ${rc} -ne 0 || -z "${out}" || -e "${_work}/--help" || -e "${_work}/-h" ]]; then
        echo "FAIL: ${script} $*: rc=${rc}: $(head -c 160 <<<"${out}" | tr '\n' ' ')"
        rm -f "${_work}/--help" "${_work}/-h"
        failed=$((failed + 1))
    fi
}

for script in "${_entry_points[@]/#/scripts/}" "${_addons[@]}"; do
    run_case "${script}" --help
    run_case "${script}" lab.json --help
    run_case "${script}" lab.json -h
done
run_case scripts/lab-builder-passwd --help
run_case scripts/lab-builder-passwd someuser --help

if [[ ${failed} -ne 0 ]]; then
    echo "${failed} check(s) failed"
    exit 1
fi
echo "all help-anywhere checks passed"
