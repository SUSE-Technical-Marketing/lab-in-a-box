#!/bin/bash
# Tests-paths: scripts/install_vllm
# Unit tests for install_vllm. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/134_vllm_test.py
