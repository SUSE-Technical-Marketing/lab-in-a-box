#!/bin/bash
# install_complianceascode: Kubernetes version ranges and the chart choice between rancher-compliance and
# rancher-cis-benchmark. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/88_complianceascode_test.py
