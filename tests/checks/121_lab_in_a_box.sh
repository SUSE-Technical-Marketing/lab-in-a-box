#!/bin/bash
# Tests-paths: scripts/install_lab_in_a_box libs/lab_in_a_box_common.py
# Unit tests for install_lab_in_a_box. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/121_lab_in_a_box_test.py
