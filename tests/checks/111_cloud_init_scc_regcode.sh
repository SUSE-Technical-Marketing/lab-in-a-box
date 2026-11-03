#!/bin/bash
# cloud-init user-data template: scc_regcode/scc_email register a bring-your-own-subscription SUSE image with
# SUSEConnect at first boot, and a node without them renders exactly as before. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/111_cloud_init_scc_regcode_test.py
