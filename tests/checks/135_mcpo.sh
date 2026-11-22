#!/bin/bash
# Tests-paths: scripts/install_mcpo templates/addons/mcpo
# Unit tests for install_mcpo, including rendering its real manifest template.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/135_mcpo_test.py
