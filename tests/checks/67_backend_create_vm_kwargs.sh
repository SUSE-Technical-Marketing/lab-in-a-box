#!/bin/bash
# Every VMBackend.create_vm() accepts every keyword setup_vm.py passes it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/67_backend_create_vm_kwargs_test.py
