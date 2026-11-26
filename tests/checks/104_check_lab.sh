#!/bin/bash
# check_lab.py: it runs every add-on's own --check on the target that add-on was installed on, and a lab where
# nothing implements a check cannot come out looking verified.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/104_check_lab_test.py
