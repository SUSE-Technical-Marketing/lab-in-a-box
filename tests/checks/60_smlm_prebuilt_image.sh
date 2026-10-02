#!/bin/bash
# Unit tests for install_smlm.py's pre-built server image support
# (smlm_preinstalled, smlm_image_admin_pass) and the server-side lab
# conveniences (smlm_salt_auto_accept, smlm_bootstrap_scripts, smlm_ssl_*).
# SSH and the XML-RPC API are mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/60_smlm_prebuilt_image_test.py
