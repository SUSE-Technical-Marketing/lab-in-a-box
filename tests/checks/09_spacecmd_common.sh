#!/bin/bash
# Mocked-SSH unit tests for libs/spacecmd_common.py (the
# shared SMLM/Uyuni activation-key + channel-sync helpers). Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/09_spacecmd_common_test.py
