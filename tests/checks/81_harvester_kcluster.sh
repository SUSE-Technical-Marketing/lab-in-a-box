#!/bin/bash
# Unit tests for libs/harvester.py: Harvester kclusters (clu_type "harvester") on the aws backend — node configuration,
# preflight problems, and the HTTPS, kubeconfig, Ready-node and Settings steps with urllib, SSH and kubectl mocked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/81_harvester_kcluster_test.py
