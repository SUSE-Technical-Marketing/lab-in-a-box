#!/bin/bash
# Tests-paths: scripts/install_libretranslate libs/podman_service.py
# Unit tests for install_libretranslate: which language models it loads for a lab's own selection and for "all",
# the LT_* environment that configures the container, and what --check compares. Every remote call is mocked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/148_libretranslate_test.py
