#!/bin/bash
# Schema versions: schemas/compatibility.json matches every schema (lab definition, add-ons, configuration files),
# libs/schema_versions.py catches unrecorded and misclassified changes, libs/migrations.py and migrate_config.py
# migrate lab definitions. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

export PATH="${PWD}/scripts:${PATH}"
python3.11 tests/checks/80_schema_versions_test.py
