#!/bin/bash
# cloud-init user-data template: scc_regcode/scc_email must NEVER reach this template — SCC registration is
# deliberately handled post-boot over SSH instead (see the Python test's own header comment for why), so a node
# with or without them renders identically. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/111_cloud_init_scc_regcode_test.py
