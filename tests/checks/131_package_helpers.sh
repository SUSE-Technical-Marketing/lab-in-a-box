#!/bin/bash
# Tests-paths: libs/lab_creation.py
# lab_creation's package helpers: which manager is picked, availability, the transactional snapshot reboot,
# required=False, extra_args, ensure_podman and open_guest_port. SSH is faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/131_package_helpers_test.py
