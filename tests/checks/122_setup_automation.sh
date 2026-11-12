#!/bin/bash
# Tests-paths: setup_demo_server/setup_automation.py setup_demo_server/setup_kvm_node.py
# Unit tests for setup_demo_server/setup_automation.py — the automation-system-first entrypoint
# (automation node mandatory, KVM-host setup is its optional add-on, the reverse priority from
# setup_kvm_node.py). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/122_setup_automation_test.py
