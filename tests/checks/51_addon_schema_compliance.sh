#!/bin/bash
# Every tracked scripts/install_<addon> executable must handle --schema/--version, run as its own executable (its shebang
# picks the language), exactly as setup_lab.py and the web UI run it. --schema must print a JSON object with
# "capabilities". PATH is left alone: an add-on finds lab_schema next to itself.
# 22_addon_common_schema_test.py exercises addon_common.print_schema() against a synthetic fixture only.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0

while IFS= read -r f; do
    name=$(basename "$f")

    out=$("./$f" --schema json 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]] || ! printf '%s' "$out" | python3.11 -c 'import json,sys; assert json.load(sys.stdin)["capabilities"]' 2>/dev/null; then
        _fail=$((_fail + 1))
        echo "FAIL: $name --schema json did not print a JSON object with capabilities (rc=$rc): $(printf '%s\n' "$out" | head -1)"
        continue
    fi

    out=$("./$f" --version 2>&1)
    rc=$?
    if [[ $rc -ne 0 || "$out" != "$name "* ]]; then
        _fail=$((_fail + 1))
        echo "FAIL: $name --version did not print '$name <version>' (rc=$rc): $out"
        continue
    fi

    _pass=$((_pass + 1))
done < <(git -c safe.directory="$PWD" ls-files 'scripts/install_*')

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
