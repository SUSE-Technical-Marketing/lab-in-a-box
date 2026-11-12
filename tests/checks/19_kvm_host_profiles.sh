#!/bin/bash
# Tests-paths: libs/kvm_host_profiles.py
# Pure-logic unit tests for libs/kvm_host_profiles.py — the
# per-(OS,version) hypervisor-host package/repo/bridge profiles. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/19_kvm_host_profiles_test.py
