#!/bin/bash
# Every field the shipped example labs use is declared in the base lab schema
# (libs/lab_schema_base.py). Cross-cutting: it reads every examples/*.json*
# against every section of the schema, so it carries no Tests-paths line.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/126_example_schema_fields_test.py
