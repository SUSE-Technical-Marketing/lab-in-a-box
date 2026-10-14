#!/bin/bash
# Every VMBackend's vm_is_reusable() (setup_lab.py --keep) and reboot_vm(), the shared DNS/SSH keep check, AWS's
# secondary private IPs and LibvirtBackend's MAC and free-resource probes. Provider calls are mocked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/82_backend_keep_reboot_test.py
