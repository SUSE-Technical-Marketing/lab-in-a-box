#!/bin/bash
# Installing a package on a node goes through lab_creation.install_packages(), never a per-file zypper/dnf/yum/
# apt-get/transactional-update command. This check fails on any file that writes its own install command and is not
# in KNOWN_DEBT below.
#
# KNOWN_DEBT is the duplication that predates install_packages(). It is a shrinking list, never a growing one: when a
# file there is migrated onto the library, its line is deleted. Adding a line to it needs an explicit exception from
# the project owner.
#
# Deliberately NOT annotated with Tests-paths: it scans every add-on and every library, so narrowing it to the files
# it happens to name today would stop it running on the change that breaks it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

# A raw package-manager install command, in any of the families lab_creation.install_packages() handles.
_PATTERN='(zypper[^|;&]*install|dnf install|yum install|apt-get install|transactional-update[^|;&]*pkg install)'

# The package helpers themselves: this is where those commands belong.
_LIBRARY='libs/lab_creation.py'

KNOWN_DEBT=(
    libs/backends.py
    libs/db_common.py
    libs/k8s.py
    libs/kvm_host_profiles.py
    libs/overlay.py
    libs/services.py
    libs/spacecmd_common.py
    scripts/build_lab_usb.py
    scripts/install_ansible_control_node
    scripts/install_gitlab
    scripts/install_grafana
    scripts/install_longhorn
    scripts/install_nextcloud
    scripts/install_prometheus
    scripts/install_seafile
    scripts/install_smlm
    scripts/install_uyuni
)

_known() {
    local f
    for f in "${KNOWN_DEBT[@]}"; do
        [[ "$1" == "$f" ]] && return 0
    done
    return 1
}

# Tracked AND untracked-but-not-ignored: a brand-new add-on is checked before it is ever committed.
mapfile -t _tracked < <(git -c safe.directory="*" ls-files --cached --others --exclude-standard \
    'libs/*.py' 'scripts/install_*' 'scripts/*.py' | sort -u)
mapfile -t _offenders < <(printf '%s\n' "${_tracked[@]}" | grep -v "^${_LIBRARY}$" \
    | xargs grep -lE "${_PATTERN}" 2>/dev/null | sort)

for _f in "${_offenders[@]}"; do
    _known "${_f}" && continue
    fail "${_f} installs packages with its own command — call lab_creation.install_packages() instead (see the
      comment at the top of this check)"
done

# The debt list only shrinks: a file in it that no longer has such a command must be removed from it, so the list
# stays an accurate statement of what is left.
for _f in "${KNOWN_DEBT[@]}"; do
    if [[ ! -f "${_f}" ]]; then
        fail "KNOWN_DEBT names '${_f}', which does not exist — delete the line"
    elif ! grep -qE "${_PATTERN}" "${_f}" 2>/dev/null; then
        fail "${_f} no longer installs packages with its own command — delete its KNOWN_DEBT line"
    fi
done

# The library really does offer the shared helpers the rest of the tree is told to use.
for _fn in "def package_manager(" "def package_available(" "def install_packages("; do
    grep -q "${_fn}" "${_LIBRARY}" || fail "${_LIBRARY} has no '${_fn}' — this check points the whole tree at it"
done

[[ ${_fail} -eq 0 ]] && echo "all package-install reuse checks passed"
exit ${_fail}
