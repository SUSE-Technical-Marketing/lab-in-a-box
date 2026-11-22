#!/bin/bash
# Tests-paths: scripts/install_anthropic_tools
# Unit tests for install_anthropic_tools. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/133_anthropic_tools_test.py
