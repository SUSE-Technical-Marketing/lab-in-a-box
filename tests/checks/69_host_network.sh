#!/bin/bash
# Unit tests for libs/host_network.py (bridge renderers and rollback handling).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/69_host_network_test.py
