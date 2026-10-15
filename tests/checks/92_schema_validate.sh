#!/bin/bash
# Schema-driven value validation: libs/schema_validate.py's checks, the preflight reporting them, every add-on's
# --validate running them, and every lab definition the repository ships staying valid.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/92_schema_validate_test.py
