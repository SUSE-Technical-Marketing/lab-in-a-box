#!/bin/bash
# Tests-paths: scripts/lab_schema
# py_compile over every .py file in the repo, plus scripts/lab_schema (Python
# with no .py suffix — matches how it's actually deployed). Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

_pyc_compile() {
    # /repo is read-only (see run_tests.sh) — py_compile's default cfile
    # location is next to the source file, which would fail there. Point it
    # at a throwaway path instead; we only care about the syntax check.
    python3 -c "
import py_compile, sys
py_compile.compile(sys.argv[1], cfile='/tmp/_static_check.pyc', doraise=True)
" "$1"
}

while IFS= read -r -d '' f; do
    if _pyc_compile "$f" 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: $f"
        sed 's/^/    /' "$_err"
    fi
done < <(find . -name '*.py' -not -path './.git/*' -not -path '*/__pycache__/*' -print0)

if _pyc_compile scripts/lab_schema 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: scripts/lab_schema"
    sed 's/^/    /' "$_err"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
