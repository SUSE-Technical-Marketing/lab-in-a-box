#!/bin/bash
# The cloud automation node: what setup_automation_node.py derives for automation-node-lib.sh, and the
# "cloud" node kind's BIND policy, omitted KVM-host records and skipped hypervisor mount. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/102_automation_node_cloud_test.py
