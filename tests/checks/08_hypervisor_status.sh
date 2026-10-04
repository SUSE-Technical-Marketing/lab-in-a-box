#!/bin/bash
# Pure-logic unit tests for the hypervisor status snapshot feature
# (scripts/refresh_hypervisor_status.py + webui/lib/discovery.py's
# status()/dynamic ISO_IMAGE enum injection) — SSH is mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/08_hypervisor_status_test.py
