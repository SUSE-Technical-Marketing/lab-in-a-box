#!/bin/bash
# The installer deploys what the repository tracks: a leftover file in a checkout is not installed as if it were part
# of the project, a packaged tree (no git) still deploys everything, and LAB_DEPLOY_UNTRACKED=1 overrides it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_fail=0
check() { if [[ "$2" != "ok" ]]; then echo "FAIL: $1"; _fail=$((_fail+1)); fi; }

INSTALLER=install_automation_node_scripts.sh

# Every loop that deploys a glob has to ask first, or a leftover rides along.
for _glob in 'libs/\*' 'scripts/install_\*' 'templates/salt/\*' 'examples/\*.json'
do
    _line=$(grep -n "for i in ${_glob}" "${INSTALLER}" | head -1 | cut -d: -f1)
    if [[ -z "${_line}" ]]; then
        echo "FAIL: ${INSTALLER} no longer has a loop over ${_glob}"; _fail=$((_fail+1)); continue
    fi
    _body=$(sed -n "${_line},$((_line+8))p" "${INSTALLER}")
    grep -q "deploy_this" <<<"${_body}" && check "the ${_glob} loop asks deploy_this" ok \
        || check "the ${_glob} loop asks deploy_this" no
done

check "what was skipped is reported, not silently dropped" \
    "$(grep -q "Not deployed, because this checkout does not track them" "${INSTALLER}" && echo ok)"
check "the way to deploy it anyway is named" \
    "$(grep -q "LAB_DEPLOY_UNTRACKED=1 to deploy them as well" "${INSTALLER}" && echo ok)"

# The function itself, taken from the installer rather than restated here, against a throwaway repository.
_tmp=$(mktemp -d)
trap 'rm -rf "${_tmp}"' EXIT
sed -n '/^deploy_this() {/,/^}/p' "${INSTALLER}" > "${_tmp}/fn.sh"
check "deploy_this is where the test expects it" "$([[ -s ${_tmp}/fn.sh ]] && echo ok)"

_repo="${_tmp}/repo"
mkdir -p "${_repo}"
(
  cd "${_repo}" || exit 1
  git init -q . 2>/dev/null
  git config user.email t@t; git config user.name t
  echo tracked > tracked.py
  echo leftover > leftover.py
  git add tracked.py
  git commit -qm init
) >/dev/null 2>&1

_probe() {   # $1: dir to run in, $2: LAB_DEPLOY_UNTRACKED, prints what was skipped
  (
    cd "$1" || exit 1
    export LAB_DEPLOY_UNTRACKED="$2"
    _in_checkout=$(git rev-parse --is-inside-work-tree 2>/dev/null)
    _skipped_untracked=()
    # shellcheck source=/dev/null
    source "${_tmp}/fn.sh"
    for f in tracked.py leftover.py; do deploy_this "${f}" || true; done
    printf '%s\n' "${_skipped_untracked[@]:-}"
  )
}

_out=$(_probe "${_repo}" "")
check "a tracked file is deployed" "$(grep -q tracked.py <<<"${_out}" && echo no || echo ok)"
check "a leftover in a checkout is not" "$(grep -q leftover.py <<<"${_out}" && echo ok)"

_out=$(_probe "${_repo}" "1")
check "LAB_DEPLOY_UNTRACKED=1 deploys it anyway" "$([[ -z ${_out//[[:space:]]/} ]] && echo ok)"

# A tree packaged without .git deploys everything, as it always did: setup_lab_automation.sh puts there what it means
# to put there, and lab_version() already falls back to .lab-versions for exactly that case.
_plain="${_tmp}/packaged"
mkdir -p "${_plain}"
cp "${_repo}/tracked.py" "${_repo}/leftover.py" "${_plain}/"
_out=$(_probe "${_plain}" "")
check "a packaged tree with no git deploys everything" "$([[ -z ${_out//[[:space:]]/} ]] && echo ok)"

if [[ ${_fail} -gt 0 ]]; then echo "${_fail} check(s) failed"; exit 1; fi
echo "all deploy-tracked-only checks passed"
