#!/bin/bash
# The shape of a BIND zone this project manages (libs/dns_zone.py) and the DNS master handling in
# libs/services.py: zone creation before a record is written, the once-per-process reachability gate, the
# additive sync, the deliberate prune, and the reported removal. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/108_dns_zone_sync_test.py
