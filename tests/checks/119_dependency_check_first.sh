#!/bin/bash
# Best-effort static lint: every caller of addon_common.die_not_ready() must make that call
# before any direct ssh_run() call in the same function (see die_not_ready()'s own docstring
# for why). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/119_dependency_check_first_test.py
