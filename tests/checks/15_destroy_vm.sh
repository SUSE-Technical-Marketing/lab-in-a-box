#!/bin/bash
# Tests-paths: scripts/destroy_vm.py libs/overlay.py
# Mocked unit tests for scripts/destroy_vm.py — single-VM
# teardown orchestration: the DNS removal, its ordering against the delete, and
# the node's removal from the overlay hub's node registry. Every lab_creation
# and overlay call is monkeypatched. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/15_destroy_vm_test.py
