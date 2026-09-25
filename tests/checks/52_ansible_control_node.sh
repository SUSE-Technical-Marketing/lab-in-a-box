#!/bin/bash
# Unit tests for install_ansible_control_node.py. Independent container —
# see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/52_ansible_control_node_test.py
