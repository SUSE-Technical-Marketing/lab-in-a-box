#!/bin/bash
# Installing a package on a node goes through lab_creation.install_packages(), never a per-file zypper/dnf/yum/
# apt-get/transactional-update command. This check fails on any file that writes its own install command and is not
# listed in NOT_APPLICABLE below, with its reason.
#
# NOT_APPLICABLE is only for code that CANNOT call a Python function at the point it installs: a command string
# embedded in a generated script, a Containerfile layer, the local host's own argv, or a command run inside a
# container. Ordinary SSH package installation is never an exception; a new entry here needs an explicit exception
# from the project owner, with its reason on the same line.
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

# "<path>|<why it cannot call the library>"
NOT_APPLICABLE=(
    "libs/backends.py|BIND_INSTALL is a command string inside a cloud-init bootstrap script that picks the manager in shell, on a VM there is no SSH to yet"
    "libs/kvm_host_profiles.py|argv for the local host's own package manager, not a node over SSH"
    "libs/services.py|a RUN layer inside a Containerfile, executed by the image build"
    "libs/spacecmd_common.py|runs inside the server container through podman exec, and bootstraps podman-or-docker with its own shim policy"
    "scripts/build_lab_usb.py|one kernel-default install needs zypper --force-resolution plus an answer on stdin"
)

_reason() {
    local entry
    for entry in "${NOT_APPLICABLE[@]}"; do
        [[ "${entry%%|*}" == "$1" ]] && return 0
    done
    return 1
}

# Tracked AND untracked-but-not-ignored: a brand-new add-on is checked before it is ever committed.
mapfile -t _tracked < <(git -c safe.directory="*" ls-files --cached --others --exclude-standard \
    'libs/*.py' 'scripts/install_*' 'scripts/*.py' | sort -u)
mapfile -t _offenders < <(printf '%s\n' "${_tracked[@]}" | grep -v "^${_LIBRARY}$" \
    | xargs grep -lE "${_PATTERN}" 2>/dev/null | sort)

for _f in "${_offenders[@]}"; do
    _reason "${_f}" && continue
    fail "${_f} installs packages with its own command — call lab_creation.install_packages() instead (see the
      comment at the top of this check)"
done

# The exception list stays accurate: an entry whose file is gone, or that no longer writes such a command, is deleted.
for _entry in "${NOT_APPLICABLE[@]}"; do
    _f="${_entry%%|*}"
    _why="${_entry#*|}"
    if [[ -z "${_why}" || "${_why}" == "${_f}" ]]; then
        fail "NOT_APPLICABLE entry for '${_f}' has no reason — add one after the '|'"
    elif [[ ! -f "${_f}" ]]; then
        fail "NOT_APPLICABLE names '${_f}', which does not exist — delete the line"
    elif ! grep -qE "${_PATTERN}" "${_f}" 2>/dev/null; then
        fail "${_f} no longer installs packages with its own command — delete its NOT_APPLICABLE line"
    fi
done

# The library really does offer the shared helpers the rest of the tree is told to use.
for _fn in "def package_manager(" "def package_available(" "def install_packages(" "def ensure_podman(" \
           "def open_guest_port("; do
    grep -q "${_fn}" "${_LIBRARY}" || fail "${_LIBRARY} has no '${_fn}' — this check points the whole tree at it"
done

[[ ${_fail} -eq 0 ]] && echo "all package-install reuse checks passed"
exit ${_fail}
