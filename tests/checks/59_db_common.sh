#!/bin/bash
# Unit tests for libs/db_common.py. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/59_db_common_test.py
