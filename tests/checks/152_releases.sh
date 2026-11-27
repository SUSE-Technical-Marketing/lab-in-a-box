#!/bin/bash
# Which releases of a Kubernetes distribution a lab can install: that the list is read from the distribution's own
# git repository rather than a rate-limited REST API, at most once a day, topped up with only what is newer than
# the list already held, kept when the repository cannot be read, filtered by channel, and never an allowlist a
# hand-typed version cannot get past.
# Tests-paths: libs/releases.py libs/harvester.py scripts/refresh_hypervisor_status.py webui/lib/discovery.py
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/152_releases_test.py
