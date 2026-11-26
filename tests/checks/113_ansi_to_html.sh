#!/bin/bash
# Tests-paths: webui/htdocs/ansi.js libs/lab_creation.py
# webui/htdocs/ansi.js: node --check syntax, plus a DOM-free functional smoke test of ansiToHtml().
# The escape sequences it is tested against are read out of libs/lab_creation.py and handed to the
# test in LAB_ANSI, so the test renders what the project actually emits and holds no copy of its own.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

_js="webui/htdocs/ansi.js"

# name -> escape sequence, straight out of lab_creation.py's own constants. An empty result is a
# failure, not an empty test run: it means the constants moved and nothing here covers them.
if _codes=$(python3.11 - <<'PY' 2>"$_err"
import io
import json
import re

src = io.open("libs/lab_creation.py", encoding="utf-8").read()
found = dict(re.findall(r'^_([A-Z]+)\s*=\s*"((?:\\033|\\x1b|\\e)\[[0-9;]*m)"', src, re.M))
if len(found) < 2:
    raise SystemExit("found {} colour constant(s) in libs/lab_creation.py, expected the full set — "
                     "the declarations moved or changed shape".format(len(found)))
print(json.dumps(found))
PY
); then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: reading the colour constants out of libs/lab_creation.py"
    sed 's/^/    /' "$_err"
fi

if node --check "$_js" 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: node --check $_js"
    sed 's/^/    /' "$_err"
fi

if LAB_ANSI="${_codes}" node tests/checks/113_ansi_to_html_test.js 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: ansiToHtml functional smoke test"
    sed 's/^/    /' "$_err"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
