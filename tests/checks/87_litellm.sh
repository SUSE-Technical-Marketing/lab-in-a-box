#!/bin/bash
# libs/litellm_common.py and the anthropic/gemini/kimi/openai add-ons built on it: chart values, secrets, ingress. SSH is
# faked. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/87_litellm_test.py
