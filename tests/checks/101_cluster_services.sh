#!/bin/bash
# What a kcluster brings itself and what it is given: clu_ingress and clu_storage, the add-ons that supply them,
# an add-on's requires_cluster, and Longhorn sized to the cluster it is installed on.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/101_cluster_services_test.py
