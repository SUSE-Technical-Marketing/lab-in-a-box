#!/bin/bash
# The facets the web UI filters the catalogue by: that each one is read off an add-on's own capabilities, that
# every item the catalogue offers carries at least one, and that the browser is handed the same vocabulary the
# server derives. Scans every scripts/install_* file, so it carries no Tests-paths line and always runs.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/143_catalogue_facets_test.py
