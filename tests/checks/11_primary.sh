#!/bin/bash
# Pure-logic unit tests for libs/primary.py — lab
# definition loading and simple-shell-variable config parsing. No mocking
# needed (pure file/string parsing, no subprocess/SSH). Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/11_primary_test.py || _fail=1
python3.11 tests/checks/11_primary_test.py || _fail=1
exit ${_fail}
