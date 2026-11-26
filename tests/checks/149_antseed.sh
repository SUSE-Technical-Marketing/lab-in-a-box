#!/bin/bash
# Tests-paths: scripts/install_antseed libs/nodejs.py
# Unit tests for install_antseed: the CLI install, the role services, the desktop asset chosen per architecture
# and package manager, and that secrets go to a root-only file rather than a command line. Every remote call is
# mocked. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/149_antseed_test.py
