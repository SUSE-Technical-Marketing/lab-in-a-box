#!/bin/bash
# Unit tests for install_rodeo_cli. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/120_rodeo_cli_test.py
