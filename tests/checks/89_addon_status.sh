#!/bin/bash
# Add-on status ("under construction"): the warning text, --help, a run that warns and continues, --schema/--capabilities,
# check_addon validation, the setup_lab preflight warning and the web UI catalogue fields.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/89_addon_status_test.py
