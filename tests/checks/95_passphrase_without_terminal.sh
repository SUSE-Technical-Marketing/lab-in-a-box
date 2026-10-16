#!/bin/bash
# A lab whose credentials are encrypted, created without a terminal: the passphrase comes from
# LAB_CREDENTIALS_PASSPHRASE, the lab-builder hands it to that one job's environment and keeps it nowhere else.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/95_passphrase_without_terminal_test.py
