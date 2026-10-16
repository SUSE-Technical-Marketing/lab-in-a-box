#!/bin/bash
# Installing an add-on's product from the SUSE Application Collection instead of its upstream chart: the shared
# mechanism in libs/appco_common.py and longhorn (SUSE Storage), the first add-on to offer it.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/98_appcollection_source_test.py
