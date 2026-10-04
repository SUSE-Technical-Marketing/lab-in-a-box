#!/bin/bash
# Mocked-subprocess unit tests for libs/lab_creation.py —
# the shared VM-provisioning/DNS/preflight helpers used by setup_vm.py,
# destroy_vm.py, setup_lab.py, and k8s.py. These verify command shape and control flow
# with subprocess.run/ssh_run mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/10_lab_creation_core_test.py
