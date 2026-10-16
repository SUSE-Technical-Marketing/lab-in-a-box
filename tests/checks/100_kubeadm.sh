#!/bin/bash
# clu_type kubeadm: what it installs on a node, how a control-plane node and a worker join, the network plugin it
# applies, and that the schema's cluster-type list and k8s.py's registry still agree.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/100_kubeadm_test.py
