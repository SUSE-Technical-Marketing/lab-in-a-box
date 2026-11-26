#!/bin/bash
# Tests-paths: scripts/install_gitleaks libs/secret_scan.py templates/addons/secret_scan/cronjob.yml.tmpl
# Unit tests for install_gitleaks' two modes, the podman container and the Kubernetes CronJob, and for the
# libs/secret_scan.py plumbing they run on. Every remote call is mocked; the CronJob template is rendered for
# real. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/144_gitleaks_test.py
