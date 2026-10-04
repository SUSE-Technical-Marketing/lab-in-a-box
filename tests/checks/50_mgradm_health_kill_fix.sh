#!/bin/bash
# Regression test for the uyuni-server crash-loop fix:
# mgradm bakes --health-on-failure=stop with a zero start-period into the
# generated systemd unit, which can kill the container mid-warm-up before it
# ever reaches "healthy".
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/50_mgradm_health_kill_fix_test.py
