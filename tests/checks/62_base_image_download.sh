#!/bin/bash
# Base image download (a URL in SOURCE_IMAGE + SOURCE_SHA256, or ISO_URL /
# ISO_SHA256 / ISO_SHA256_URL): field
# validation, node/common pairing, KVM host candidates, and the real fetch
# script run locally against file:// URLs (SSH is replaced by a local
# `bash -s`). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/62_base_image_download_test.py
