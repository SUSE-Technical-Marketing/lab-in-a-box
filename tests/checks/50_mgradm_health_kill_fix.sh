#!/bin/bash
# Targeted regression test for the 2026-09-13 uyuni-server crash-loop fix:
# mgradm bakes --health-on-failure=stop with a zero start-period into the
# generated systemd unit, which can kill the container mid-warm-up before it
# ever reaches "healthy" (confirmed live, real AWS SMLM 5.2.0 install).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/50_mgradm_health_kill_fix_test.py
