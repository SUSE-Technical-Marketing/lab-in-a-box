#!/bin/bash
# The four per-backend overlay primitives in libs/backends.py (private IP, subnet CIDR, VM-less port opening,
# source/dest filter) across all ten backends, and the shared guest-side address helper they fall back to.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/139_overlay_primitives_test.py
