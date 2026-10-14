#!/bin/bash
# install_kagent.py: OCI charts in order, provider values, the API-key secret, the UI ingress and validation. SSH is faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/86_kagent_test.py
