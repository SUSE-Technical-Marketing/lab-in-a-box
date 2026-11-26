#!/bin/bash
# lab-builder's authorization layer: who may use the admin actions, as opposed to who can log in
# (webui/lib/admin.py's authorized(), enforced by webui/lib/api.py's dispatch()), the audit record, and the
# shipped single-sign-on example. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/140_labbuilder_authorization_test.py
