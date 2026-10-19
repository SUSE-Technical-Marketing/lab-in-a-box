#!/bin/bash
# pushDockerImage.sh: it runs at all (it used to exit "Library not found" every time), pushes to MYREG with the
# container engine the host has, and refuses clearly when something is missing.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

SCRIPT="${PWD}/scripts/pushDockerImage.sh"
_fail=0
check() { if [[ "$2" != "ok" ]]; then echo "FAIL: $1"; _fail=$((_fail+1)); fi; }

_tmp=$(mktemp -d)
trap 'rm -rf "${_tmp}"' EXIT
cd "${_tmp}" || exit 1

# Config in the working directory, which the script accepts when /etc has none. In this container it does not.
printf '_lib_path=/usr/local/lib/lab_creation\n' > lab_creation.defaults
printf "MYREG='registry.example.lab'\n" > lab_creation.cfg
mkdir -p bin
printf '#!/bin/sh\necho "podman $*"\n' > bin/podman
printf '#!/bin/sh\necho "docker $*"\n' > bin/docker
chmod +x bin/podman bin/docker

out=$(PATH="${_tmp}/bin:${PATH}" bash "${SCRIPT}" nginx:latest 2>&1); rc=$?
check "it runs instead of dying on the library check" "$([[ ${rc} -eq 0 ]] && echo ok)"
check "the library is not sourced at all" "$(grep -q '\. ${_lib_path}' "${SCRIPT}" && echo no || echo ok)"
check "the image is pulled, tagged for MYREG and pushed" \
    "$(grep -q 'pull nginx:latest' <<<"${out}" && grep -q 'tag nginx:latest registry.example.lab/nginx:latest' <<<"${out}" \
       && grep -q 'push registry.example.lab/nginx:latest' <<<"${out}" && echo ok)"
check "podman is preferred, since that is what this project installs" \
    "$(grep -q '^podman pull' <<<"${out}" && echo ok)"

# A host with only docker still works.
rm bin/podman
out=$(PATH="${_tmp}/bin:${PATH}" bash "${SCRIPT}" nginx:latest 2>&1)
check "docker is used when podman is not there" "$(grep -q '^docker pull' <<<"${out}" && echo ok)"

# Neither engine: a clear refusal, not a command-not-found.
rm bin/docker
out=$(PATH="/nonexistent" /bin/bash "${SCRIPT}" nginx:latest 2>&1); rc=$?
check "a host with no container engine is told so" \
    "$([[ ${rc} -ne 0 ]] && grep -qi "neither podman nor docker" <<<"${out}" && echo ok)"

# No registry configured: the push would land nowhere.
printf '#!/bin/sh\necho "podman $*"\n' > bin/podman; chmod +x bin/podman
printf '\n' > lab_creation.cfg
out=$(PATH="${_tmp}/bin:${PATH}" bash "${SCRIPT}" nginx:latest 2>&1); rc=$?
check "an unset MYREG is refused, with the setting named" \
    "$([[ ${rc} -ne 0 ]] && grep -q "MYREG is not set" <<<"${out}" && echo ok)"

# No image: usage, and a non-zero exit, instead of a silent success.
printf "MYREG='registry.example.lab'\n" > lab_creation.cfg
out=$(PATH="${_tmp}/bin:${PATH}" bash "${SCRIPT}" 2>&1); rc=$?
check "no image name is a usage error" "$([[ ${rc} -ne 0 ]] && grep -q "Usage:" <<<"${out}" && echo ok)"

out=$(PATH="${_tmp}/bin:${PATH}" bash "${SCRIPT}" --help 2>&1)
check "--help still works and needs no configuration" "$(grep -q "Usage:" <<<"${out}" && echo ok)"

if [[ ${_fail} -gt 0 ]]; then echo "${_fail} check(s) failed"; exit 1; fi
echo "all push-image checks passed"
