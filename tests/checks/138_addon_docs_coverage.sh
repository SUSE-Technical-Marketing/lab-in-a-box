#!/bin/bash
# Every add-on is documented, in every language, and the documented add-on counter is the real one.
# Scans every add-on and every documentation page, so it declares no Tests-paths.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/138_addon_docs_coverage_test.py
