#!/bin/bash
# Tests-paths: libs/desktop_common.py scripts/install_xfce
# desktop_common and install_xfce: the per-distribution package sets, the "does not distribute" failures, the console/
# xrdp/vnc access methods and lab_creation's own package helpers. SSH is faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/129_desktop_test.py
