#!/bin/bash
# Tests-paths: setup_demo_server/setup_kvm_node.py libs/kvm_host_profiles.py scripts/setup_credentials.py setup_demo_server/automation.cfg.template setup_demo_server/kvm_host.cfg.template
# Pure-logic unit tests for setup_demo_server/setup_kvm_node.py
# — image-URL construction and bridge-configuration wiring. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/20_setup_kvm_node_test.py
