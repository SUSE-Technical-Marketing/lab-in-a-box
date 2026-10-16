#!/bin/bash
# Unit tests for install_client_registration's per-node
# client_registration_* override. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/48_client_registration_test.py
