#!/bin/bash
# LibvirtBackend picks virt-install --graphics from the hypervisor's
# domcapabilities (vnc where QEMU has no spice). virsh is mocked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/68_libvirt_graphics_test.py
