#!/bin/bash
# Unit tests for install_run_command. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/115_run_command_test.py
