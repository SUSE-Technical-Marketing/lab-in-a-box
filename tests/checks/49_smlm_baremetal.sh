#!/bin/bash
# Unit tests for install_smlm.py's traditional mgradm/podman (bare-metal)
# deployment mode. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/49_smlm_baremetal_test.py
