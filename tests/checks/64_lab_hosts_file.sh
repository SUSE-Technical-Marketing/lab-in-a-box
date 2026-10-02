#!/bin/bash
# LAB_HOSTS_FILE: DNSService keeps an /etc/hosts-style line per registered VM.
# Zone files and named are redirected/mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/64_lab_hosts_file_test.py
