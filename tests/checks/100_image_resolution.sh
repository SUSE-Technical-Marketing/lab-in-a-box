#!/bin/bash
# Product-name image resolution: SUSE's own image pickers, the AWS product table for the operating
# systems SUSE does not publish, architecture detection from the instance type, and the VPC DHCP
# option set that points cloud nodes at their lab's DNS server. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/100_image_resolution_test.py
