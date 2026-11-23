#!/bin/bash
# The hosted-provider add-ons (groq, cohere, xai, …): one provider table and one install path in
# libs/litellm_common.py, with each add-on only naming its provider. Scans every such add-on, so it declares no
# Tests-paths. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/137_litellm_providers_test.py
