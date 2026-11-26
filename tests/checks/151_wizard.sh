#!/bin/bash
# Tests-paths: webui/lib/wizard.py webui/lib/discovery.py
# The step-by-step lab builder: every path through all four workflows is generated and put through the same
# preflight a hand-written lab goes through, so a question whose answer produces an invalid lab fails here rather
# than in front of an operator. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/151_wizard_test.py
