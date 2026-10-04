#!/bin/bash
# Mocked unit tests for scripts/destroy_vm.py — single-VM
# teardown orchestration. Every lab_creation
# call is monkeypatched. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/15_destroy_vm_test.py
