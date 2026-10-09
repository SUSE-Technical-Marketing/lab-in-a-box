#!/bin/bash
# Unit tests for addon_common.py's --schema dispatch merging in an addon's
# PLUGIN capabilities (targets/layers/...) via apps.attach_capabilities().
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/22_addon_common_schema_test.py || _fail=1
python3.11 tests/checks/22_addon_common_schema_test.py || _fail=1
exit ${_fail}
