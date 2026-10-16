#!/bin/bash
# The example labs the lab-builder offers as starting points: what counts as one, the two API endpoints (a name is
# never a path), and the UI and the static build offering them. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/94_lab_examples_test.py
