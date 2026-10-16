#!/bin/bash
# The fleet add-on: the Fleet resources it applies in Rancher (cluster labels, cluster groups, GitRepos), that it
# refuses a cluster without Fleet, and what its --check reports. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/99_fleet_test.py
