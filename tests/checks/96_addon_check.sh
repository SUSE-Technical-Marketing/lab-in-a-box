#!/bin/bash
# An add-on's --check: comparing what is deployed with the lab definition (libs/addon_common.py's Checker), its three
# exit codes, the capability that advertises it, and the contract checker enforcing both.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/96_addon_check_test.py
