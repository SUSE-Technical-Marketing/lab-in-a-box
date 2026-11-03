#!/bin/bash
# libs/dns_zone.py's generated zones and libs/services.py's zone creation against a REAL BIND: named-checkconf and
# named-checkzone accept what they produce, and a running named answers the records add_to_dns writes and stops
# answering those del_from_dns removes. Every other DNS check fakes the zone files, so nothing else proves that
# BIND itself loads them. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

if ! command -v named >/dev/null 2>&1 || ! command -v dig >/dev/null 2>&1; then
    echo "SKIP: named/dig are not installed in this image (bind, bind-utils)"
    exit 0
fi

python3.11 tests/checks/109_dns_real_named_test.py
