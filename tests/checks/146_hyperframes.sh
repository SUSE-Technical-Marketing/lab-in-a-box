#!/bin/bash
# Tests-paths: scripts/install_hyperframes libs/nodejs.py
# Unit tests for install_hyperframes: which agents it connects and by which of their own mechanisms, and that an
# agent whose CLI is absent is skipped rather than failing the node. Every remote call is mocked. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/146_hyperframes_test.py
