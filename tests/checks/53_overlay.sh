#!/bin/bash
# Tests-paths: libs/overlay.py scripts/overlay_ctl.py scripts/setup_vm.py install_automation_node_scripts.sh
# Unit tests for libs/overlay.py. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/53_overlay_test.py
