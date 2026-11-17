#!/bin/bash
# Tests-paths: scripts/install_openshell
# install_openshell: the Agent Sandbox prerequisite, the chart's auth/TLS/telemetry values, the gateway ingress,
# host-mode installation and validation. SSH is faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/128_openshell_test.py
