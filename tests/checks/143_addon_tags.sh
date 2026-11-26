#!/bin/bash
# Every add-on's category tags: that each one resolves to at least one tag, that every tag is in the vocabulary,
# that the central table names no add-on that is gone, and that the web UI's filter is wired to all of it. Scans
# every scripts/install_* file, so it carries no Tests-paths line and always runs. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/143_addon_tags_test.py
