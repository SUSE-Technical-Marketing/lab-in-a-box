#!/bin/bash
# Custom Salt states and formulas installed on an SMLM/Uyuni server: where they land, that an RPM is unpacked and
# never installed, that a re-run with the same source changes nothing, and both add-ons' field and validation.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/97_salt_states_test.py
