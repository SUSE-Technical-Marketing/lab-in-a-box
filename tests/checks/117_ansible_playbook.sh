#!/bin/bash
# Unit tests for install_ansible_playbook. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/117_ansible_playbook_test.py
