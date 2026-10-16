#!/bin/bash
# pre_commands / post_commands: a VM's own shell commands, run on it before any other work and after all of it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/93_node_commands_test.py
