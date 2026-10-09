#!/bin/bash
# Regression tests for live-host bugs found during the python_migration cutover.
# Mocked-SSH/subprocess here so they're covered without needing real
# infrastructure going forward. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/18_live_bugfixes_test.py || _fail=1
python3.11 tests/checks/18_live_bugfixes_test.py || _fail=1
exit ${_fail}
