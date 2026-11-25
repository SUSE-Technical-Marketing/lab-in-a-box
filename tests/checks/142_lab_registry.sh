#!/bin/bash
# Tests-paths: libs/lab_registry.py scripts/lab_watch.py scripts/setup_lab.py scripts/destroy_lab.py
# Pure-logic unit tests for the registry of deployed labs (libs/lab_registry.py), its event log, and the scheduled
# check that keeps it true to what each provider reports (scripts/lab_watch.py). Every provider call is mocked and
# the registry lives in a temporary directory. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/142_lab_registry_test.py
