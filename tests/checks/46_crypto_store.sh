#!/bin/bash
# Tests-paths: libs/crypto_store.py
# Real round-trip tests for libs/crypto_store.py against the actual
# `cryptography` package (python311-cryptography, see tests/Containerfile) —
# this is the live check of the Argon2id/HKDF/AEAD API usage, nothing
# mocked. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/46_crypto_store_test.py
