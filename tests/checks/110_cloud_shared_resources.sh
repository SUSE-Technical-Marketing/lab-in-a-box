#!/bin/bash
# The resources a parallel cloud run shares (libs/backends.py): the per-backend ensure lock, the cloud DNS VM's
# health check and provider resolver, and cloud-account resolution with --cloud-account. Independent container —
# see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/110_cloud_shared_resources_test.py
