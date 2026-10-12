#!/bin/bash
# Fails when a module-level function of libs/*.py is referenced by no code outside tests/. Static AST scan, no
# imports. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/83_dead_functions_test.py
