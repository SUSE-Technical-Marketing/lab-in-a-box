#!/bin/bash
# install_suse_ai: component order, derived chart values, pull secrets, cert-manager and registry logins. SSH and the credential
# store are faked. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/84_suse_ai_test.py
