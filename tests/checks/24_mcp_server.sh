#!/bin/bash
# Tests-paths: mcp/mcp_server.py mcp/lab-mcp-issue-client-cert webui/lib/api.py
# Mocked unit tests for mcp/mcp_server.py: tool registration gating, the confirm/audit logic, per-client
# authentication and authorization (the client registry, the token verifier, the write scope and the client named
# in the audit log), the arguments the read-only tools pass to webui/lib/api.py's dispatch() (mocked), and
# _cfg()/_load_lab(). A minimal fake MCP server and auth surface stand in for the `mcp` package, which the test
# image does not install. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/24_mcp_server_test.py
