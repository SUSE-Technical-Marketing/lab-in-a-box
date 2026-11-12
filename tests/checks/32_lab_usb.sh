#!/bin/bash
# Tests-paths: libs/lab_usb.py scripts/build_lab_usb.py
# Unit tests for libs/lab_usb.py — the USB-delivery lab appliance pipeline.
# Pure functions only here (no real VM/SSH involved) — see
# scripts/build_lab_usb.py for the actual orchestration this backs.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/32_lab_usb_test.py
