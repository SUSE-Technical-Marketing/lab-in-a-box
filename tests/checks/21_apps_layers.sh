#!/bin/bash
# Tests-paths: libs/layers.py
# Pure-logic unit tests for libs/layers.py and the
# layers-related additions to apps.py. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/21_apps_layers_test.py
