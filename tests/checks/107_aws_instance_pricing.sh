#!/bin/bash
# The live EC2 instance-type catalog: the specifications and on-demand prices libs/aws_catalog.py reads,
# the cheapest-sufficient x86_64 type AWSBackend sizes a VM with, and the sizing-table fallback for an
# account or host that cannot reach either API. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/107_aws_instance_pricing_test.py
