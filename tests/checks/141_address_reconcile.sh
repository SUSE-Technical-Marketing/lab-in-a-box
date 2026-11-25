#!/bin/bash
# Tests-paths: libs/reconcile.py libs/backends.py scripts/reconcile_lab.py
# Pure-logic unit tests for libs/reconcile.py (which cloud nodes' records no longer match their provider, and what
# is done about it) and for the static_ip flag it acts on. Every provider call is mocked. Independent container —
# see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/141_address_reconcile_test.py
