#!/bin/bash
# Tests-paths: scripts/setup_lab.py scripts/setup_vm.py
# setup_lab.py's cloud-DNS-VM precondition: a cloud node's site DNS VM is
# checked before that node is destroyed, so a run that cannot provision it
# leaves the existing VM alone. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/125_cloud_dns_precondition_test.py
