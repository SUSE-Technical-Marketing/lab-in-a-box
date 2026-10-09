#!/bin/bash
# Unit tests for the multiple-cloud-accounts feature: primary.*_cloud_account,
# backends.resolve_cloud_account / effective_backend_name / get_backend, and
# ensure_cloud_dns_vm's per-account DNS VM name. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/44_cloud_accounts_test.py || _fail=1
python3.11 tests/checks/44_cloud_accounts_test.py || _fail=1
exit ${_fail}
