#!/bin/bash
# status.html's per-component (node/cluster/addon) green/red parsing (parseComponents/parseIssues):
# node --check equivalent isn't meaningful for an inline <script> in an .html file, so this extracts
# just the pure parsing functions and exercises them DOM-free. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

node tests/checks/114_status_page_components_test.js
