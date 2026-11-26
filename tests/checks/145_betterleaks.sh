#!/bin/bash
# Tests-paths: scripts/install_betterleaks libs/secret_scan.py templates/addons/secret_scan/cronjob.yml.tmpl
# Unit tests for install_betterleaks: the command line it writes for each of its two major versions, and that it
# runs on the same libs/secret_scan.py plumbing as install_gitleaks. The shared plumbing itself is covered by
# 144_gitleaks.sh. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/145_betterleaks_test.py
