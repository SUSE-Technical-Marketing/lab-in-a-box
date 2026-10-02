#!/bin/bash
# vm_power.py + VMBackend.vm_state/start_vm/stop_vm (AWS implemented, others
# "unsupported"). The aws CLI is mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/65_vm_power_test.py
