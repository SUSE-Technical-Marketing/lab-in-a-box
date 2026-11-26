#!/bin/bash
# Tests-paths: scripts/install_voicestudio libs/podman_service.py
# Unit tests for install_voicestudio and the libs/podman_service.py plumbing it runs on: the container it would
# run, that the API key never reaches a command line, and what --check compares. Every remote call is mocked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/147_voicestudio_test.py
