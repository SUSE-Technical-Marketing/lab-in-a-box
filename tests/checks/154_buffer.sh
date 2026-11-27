#!/bin/bash
# The buffer add-on: that it uses the endpoints Buffer publishes rather than invented ones, connects no agent
# unless the lab names one (its MCP tools can publish to real accounts), and never puts the API key on a command
# line. Nothing reaches buffer.com.
# Tests-paths: scripts/install_buffer libs/agents.py
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/154_buffer_test.py
