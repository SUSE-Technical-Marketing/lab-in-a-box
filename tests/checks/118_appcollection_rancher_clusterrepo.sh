#!/bin/bash
# Unit tests for install_appcollection and appco_common.register_rancher_cluster_repo.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/118_appcollection_rancher_clusterrepo_test.py
