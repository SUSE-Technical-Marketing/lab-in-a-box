#!/bin/bash
# Mocked-SSH unit tests for libs/spacecmd_common.py (the
# shared SMLM/Uyuni activation-key + channel-sync helpers). Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_fail=0
python3 tests/checks/09_spacecmd_common_test.py || _fail=1
python3 tests/checks/09_spacecmd_orchestration_test.py || _fail=1
exit ${_fail}
