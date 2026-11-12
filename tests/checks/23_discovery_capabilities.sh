#!/bin/bash
# Tests-paths: webui/lib/discovery.py
# Unit tests for webui/lib/discovery.py's schema()/discover() attaching
# PLUGIN capabilities (targets/layers), and the _def_path() fix that makes
# schema() actually find a ported addon in a repo checkout (not just
# scripts_dir() alone). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/23_discovery_capabilities_test.py
