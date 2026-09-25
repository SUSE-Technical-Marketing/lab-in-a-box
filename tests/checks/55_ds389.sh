#!/bin/bash
# Unit tests for install_ds389.py. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/55_ds389_test.py
