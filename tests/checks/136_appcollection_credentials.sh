#!/bin/bash
# Resolving a SUSE Application Collection account: the shared library function every add-on uses, and that it
# really reads a credential file. Scans every addon, so it declares no Tests-paths.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/136_appcollection_credentials_test.py
