#!/bin/bash
# Add-on version matrix: libs/versions.py, its use by the preflight, add-on
# --schema/--validate, and the helm --version flags of rancher and longhorn.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/77_version_matrix_test.py
