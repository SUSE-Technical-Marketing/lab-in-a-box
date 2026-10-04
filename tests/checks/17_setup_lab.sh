#!/bin/bash
# Mocked unit tests for scripts/setup_lab.py — the full
# lab orchestrator (VM creation, Kubernetes install, cluster/VM addon
# dispatch). Every
# lab_creation/setup_vm/destroy_vm call and the addon-dispatch
# shutil.which+subprocess.run pair are monkeypatched. Independent container
# — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/17_setup_lab_test.py
