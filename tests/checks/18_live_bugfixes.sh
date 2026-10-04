#!/bin/bash
# Regression tests for live-host bugs found during the python_migration cutover.
# Mocked-SSH/subprocess here so they're covered without needing real
# infrastructure going forward. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/18_live_bugfixes_test.py
