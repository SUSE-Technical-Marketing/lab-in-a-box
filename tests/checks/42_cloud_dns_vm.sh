#!/bin/bash
# Pure-logic unit tests for libs/backends.py's ensure_cloud_dns_vm() and
# _cloud_dns_vm_user_data(). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/42_cloud_dns_vm_test.py || _fail=1
python3.11 tests/checks/42_cloud_dns_vm_test.py || _fail=1
exit ${_fail}
