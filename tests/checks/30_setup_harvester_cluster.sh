#!/bin/bash
# Unit tests for scripts/setup_harvester_cluster.py — the new PXE-based
# Harvester HCI cluster bootstrap script. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# python3 has no PyYAML and python3.11 has it: each run covers one branch of the yaml-dependent checks.
_fail=0
python3 tests/checks/30_setup_harvester_cluster_test.py || _fail=1
python3.11 tests/checks/30_setup_harvester_cluster_test.py || _fail=1
exit ${_fail}
