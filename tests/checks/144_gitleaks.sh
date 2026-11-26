#!/bin/bash
# Tests-paths: scripts/install_gitleaks templates/addons/gitleaks/cronjob.yml.tmpl
# Unit tests for install_gitleaks' four modes: the release binary, the podman container, the Kubernetes CronJob
# and the git pre-commit hook. Every remote call is mocked; the CronJob template is rendered for real.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/144_gitleaks_test.py
