#!/bin/bash
# Mocked unit tests for scripts/destroy_lab.py — whole-lab
# teardown orchestration. destroy_vm() and
# subprocess.run (ssh-keygen) are both monkeypatched. Independent container
# — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/16_destroy_lab_test.py
