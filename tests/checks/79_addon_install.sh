#!/bin/bash
# Every Python add-on's install path runs to the end with external commands faked, sends commands, and never runs
# `helm install`. See 79_addon_install_test.py. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/79_addon_install_test.py
