#!/bin/bash
# Real multi-OS verification of install_demo_server_scripts.sh: the curl-piped bootstrap that
# installs git+python and hands off to setup_demo_server/setup_kvm_node.py. Unlike tests/checks/
# (every check runs stubbed, inside one shared container), this pulls one real base image per
# distribution the script's own case statement claims to support and lets it run its actual
# package manager — that is the only way to confirm "zypper install python311" or "dnf install
# python3" still works against that distribution's real repositories. Runs on the host (or CI
# runner) directly, never nested inside another container, since each case needs its own base
# image and its own package manager.
#
# Intentionally NOT part of tests/run_tests.sh's per-commit loop: it pulls several external OS
# images over the network and runs real package installs, which is slow and network-dependent.
# Both the pre-commit hook and CI's "changes" job gate this on whether install-affecting files
# changed (see .githooks/pre-commit and .github/workflows/ci.yml) — add a new distro case here
# instead of hardcoding a test around one, and it is covered the same way automatically.
#
# Run manually: tests/install_test/run.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo=$PWD
_script=/repo/install_demo_server_scripts.sh

if ! command -v podman >/dev/null 2>&1; then
    echo "podman is required (one real OS image per supported distribution)" >&2
    exit 1
fi

_status=0
_failed=()

# image | label | python version-ish string expected to appear in the "could not install"-free run
_cases=(
    "registry.opensuse.org/opensuse/leap:15.6|opensuse-leap-15 (python311 branch)"
    "registry.opensuse.org/opensuse/leap:16.0|opensuse-leap-16 (default python3 branch)"
    "docker.io/library/debian:12|debian-12"
    "docker.io/library/ubuntu:24.04|ubuntu-24.04"
    "docker.io/library/fedora:41|fedora-41"
    "docker.io/library/rockylinux:9|rockylinux-9 (ID_LIKE=rhel centos fedora)"
)

_run_case() {
    local image=$1 label=$2 out rc
    out=$(podman run --rm -v "${_repo}":/repo:ro "${image}" \
        bash "${_script}" --source /repo --fetch-only 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "FAIL: $label — exit $rc"
        echo "${out}" | sed 's/^/    /'
        return 1
    fi
    if [[ "${out}" != *"Fetched into /repo."* ]]; then
        echo "FAIL: $label — ran but did not reach the fetch-only confirmation message"
        echo "${out}" | sed 's/^/    /'
        return 1
    fi
    echo "ok: $label"
    return 0
}

for _case in "${_cases[@]}"; do
    IFS='|' read -r _image _label <<<"${_case}"
    echo
    echo "############################################"
    echo "# ${_label} (${_image})"
    echo "############################################"
    if ! _run_case "${_image}" "${_label}"; then
        _status=1
        _failed+=("${_label}")
    fi
done

# ── negative cases: distro branches that must refuse, not crash ──────────────
echo
echo "############################################"
echo "# unsupported distribution dies with a clear message"
echo "############################################"
out=$(podman run --rm -v "${_repo}":/repo:ro docker.io/library/debian:12 \
    bash -c 'printf "ID=plan9\n" > /etc/os-release; bash '"${_script}"' --source /repo --fetch-only' 2>&1)
rc=$?
if [[ $rc -eq 0 ]] || [[ "${out}" != *"unsupported distribution"* ]]; then
    echo "FAIL: an unrecognised ID should die with 'unsupported distribution', got (exit $rc):"
    echo "${out}" | sed 's/^/    /'
    _status=1
    _failed+=("unsupported distribution")
else
    echo "ok: unsupported distribution"
fi

echo
echo "############################################"
echo "# non-root refuses to run"
echo "############################################"
out=$(podman run --rm --user 1000:1000 -v "${_repo}":/repo:ro docker.io/library/debian:12 \
    bash "${_script}" --source /repo --fetch-only 2>&1)
rc=$?
if [[ $rc -eq 0 ]] || [[ "${out}" != *"run this as root"* ]]; then
    echo "FAIL: a non-root run should die with 'run this as root', got (exit $rc):"
    echo "${out}" | sed 's/^/    /'
    _status=1
    _failed+=("non-root")
else
    echo "ok: non-root"
fi

echo
if [[ $_status -eq 0 ]]; then
    echo "ALL INSTALL TESTS PASSED"
else
    echo "SOME INSTALL TESTS FAILED: ${_failed[*]}"
fi
exit $_status
