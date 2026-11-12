#!/bin/bash
# Tests-paths: scripts/lab_schema
# scripts/lab_schema's `enum` lists vs. the real Python registries they
# mirror (see tests/schema_consistency_check.py for what's checked and why).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/schema_consistency_check.py
