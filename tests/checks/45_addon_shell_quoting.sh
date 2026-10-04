#!/bin/bash
# Targeted regression tests for shell quoting: addon-config
# passwords / db+user names must be shlex-quoted before reaching a remote shell.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/45_addon_shell_quoting_test.py
