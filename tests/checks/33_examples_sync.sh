#!/bin/bash
# Confirms tests/examples/*.json stays in sync with the examples in docs/src/index.html. Pure text/JSON comparison.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/33_examples_sync_test.py
