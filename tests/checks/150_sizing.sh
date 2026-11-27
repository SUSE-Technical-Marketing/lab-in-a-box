#!/bin/bash
# Tests-paths: libs/sizing.py
# Unit tests for libs/sizing.py: that every product's entry is complete and carries the source its figures came
# from, and that a node running several of them is sized by the rule the module documents. Pure logic, no SSH.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/150_sizing_test.py
