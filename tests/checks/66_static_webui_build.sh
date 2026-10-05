#!/bin/bash
# The static GitHub Pages lab-builder (scripts/build-cube-static.py), built
# from the git-tracked files only — what GitHub's runner checks out, with no
# lab_schema installed on PATH — then its embedded API data checked against
# the add-on scripts themselves (66_static_webui_build_test.py) and its
# static apiGet() exercised in node (66_static_webui_build_smoke.js).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_tmp=$(mktemp -d)
_err="$_tmp/err"
trap 'rm -rf "$_tmp"' EXIT

mkdir "$_tmp/src"
if ! git -c safe.directory="$PWD" ls-files -z >"$_tmp/files" 2>"$_err"; then
    echo "FAIL: git ls-files (needed to copy only the tracked files)"
    sed 's/^/    /' "$_err"
    exit 1
fi
while IFS= read -r -d '' f; do [[ -e "$f" ]] && printf '%s\0' "$f"; done <"$_tmp/files" \
    | tar --null -T - -cf - | tar -xf - -C "$_tmp/src"

_run() {
    local desc=$1; shift
    if "$@" 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: $desc"
        sed 's/^/    /' "$_err"
    fi
}

_run "build-cube-static.py builds from a clean checkout" \
    python3.11 "$_tmp/src/scripts/build-cube-static.py" --output "$_tmp/page.html"

if [[ -s "$_tmp/page.html" ]]; then
    _run "embedded API data matches the add-on scripts" \
        python3.11 tests/checks/66_static_webui_build_test.py "$_tmp/page.html" "$_tmp/src/scripts"
    _run "static apiGet() answers from the embedded data" \
        node tests/checks/66_static_webui_build_smoke.js "$_tmp/page.html"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
