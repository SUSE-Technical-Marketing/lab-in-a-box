#!/bin/bash
# install_rancher finishes Rancher's bootstrap: without that first login its server URL stays empty and no cluster
# can ever be imported, which is what it printed a password and walked away from.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/106_rancher_bootstrap_test.py
