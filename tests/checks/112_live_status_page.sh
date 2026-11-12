#!/bin/bash
# Tests-paths: libs/qr_util.py
# QR code + public live-status page for a running/finished lab-create job: libs/qr_util.py,
# lab_builder_helper.py's token/pub-status, webui's api.py pub-status route, setup_lab.py's own
# link+QR line. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/112_live_status_page_test.py
