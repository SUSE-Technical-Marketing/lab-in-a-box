#!/bin/bash
# Tests-paths: webui/htdocs/ansi.js
# webui/htdocs/ansi.js: node --check syntax, plus a DOM-free functional smoke test of ansiToHtml()
# against the real escape sequences lab_creation.py emits (_RED/_GREEN/_ORANGE/_WHITE/_RESET).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

_js="webui/htdocs/ansi.js"

if node --check "$_js" 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: node --check $_js"
    sed 's/^/    /' "$_err"
fi

if node tests/checks/113_ansi_to_html_test.js 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: ansiToHtml functional smoke test"
    sed 's/^/    /' "$_err"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
