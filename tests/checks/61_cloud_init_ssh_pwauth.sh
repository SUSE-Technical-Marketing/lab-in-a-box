#!/bin/bash
# cloud-init user-data template: the per-node ssh_pwauth switch. Renders the
# real template through lab_creation.process_template() (bash heredoc eval,
# the same path setup_vm.py uses). Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/61_cloud_init_ssh_pwauth_test.py
