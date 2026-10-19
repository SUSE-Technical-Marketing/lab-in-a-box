#!/bin/bash
# Allocating a node's IP address when the lab definition gives none: the network it comes from, the determinism of
# the choice, what rules an address out, and the web UI's preview of it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/102_ip_allocation_test.py
