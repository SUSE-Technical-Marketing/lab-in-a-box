#!/bin/bash
# Tests-paths: libs/portforward.py libs/services.py
# Unit tests for libs/portforward.py and libs/services.py's PortForwardService
# — the DNAT port-forwarding mechanism for VMs on a NAT'd libvirt network
# (setup_demo_server/lab.cfg.template's _network_mode=nat). Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/31_portforward_service_test.py
