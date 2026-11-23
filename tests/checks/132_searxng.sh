#!/bin/bash
# Tests-paths: scripts/install_searxng scripts/install_open_webui templates/addons/searxng
# install_searxng: the settings.yml it writes, the Kubernetes manifests it renders and applies, the companion Valkey
# the limiter needs, podman mode, secret-key reuse, mode dispatch and validation; and install_open_webui's own
# SearXNG web-search wiring. SSH is faked; the manifests are rendered from the real templates and parsed.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/132_searxng_test.py
