#!/bin/bash
# client_registration_profile_name: PROFILENAME passed to bootstrap, salt-key
# checks keyed on it, SSH still to the node. Mocked SSH/spacecmd.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/63_client_registration_profile_name_test.py
