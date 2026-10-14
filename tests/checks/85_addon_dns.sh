#!/bin/bash
# Every add-on that exposes an ingress hostname registers its DNS record (register_service_dns / add_service_dns /
# add_dns_to_named_rr); register_service_dns() and del_from_dns()'s service-record cleanup. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/85_addon_dns_test.py
