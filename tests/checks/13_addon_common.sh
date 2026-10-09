#!/bin/bash
# Pure-logic unit tests for libs/addon_common.py — the
# shared --version/--validate/--help/--schema/--capabilities CLI scaffolding
# every install_<addon>.py script builds on. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/13_addon_common_test.py || _fail=1
python3.11 tests/checks/13_addon_common_test.py || _fail=1
exit ${_fail}
