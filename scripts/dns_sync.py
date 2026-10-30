#!/usr/bin/env python3
# Part of lab-in-a-box — compare and reconcile the DNS masters of a lab's zone
# Author/s: Raul Mahiques
# License: GPLv3
"""
dns_sync.py: what each DNS server of a lab's zone answers, and bringing them back into step.

Every DNS server this project writes to is a master of the same zone: records reach each one by being written to
its own zone file, with no transfer between them. A server that was unreachable while a record was added or
removed therefore stays behind, and nothing notices.

    dns_sync.py <lab.json>              report the differences and write the missing records to each master
    dns_sync.py --check <lab.json>      report only, write nothing
    dns_sync.py --prune <lab.json>      also remove every record a single master holds alone
    dns_sync.py --server <address> ...  add a DNS master to the ones the lab already names, repeatable
    dns_sync.py --help                  print this help

Records are only added by default. A record one master holds alone is either one the others have not been written
yet or one deleted from them, and the zones cannot tell those apart, so adding it everywhere is right in the first
case and harmless in the second, while removing it would silently undo a deletion. --prune takes the other
decision, and is for a record you have read in the report and know to be stale.

The masters are this automation node itself, REMOTE_DNS_SERVERS from /etc/lab_creation.cfg, and any --server given.
A lab's own cloud DNS VM is created per run and reached at whatever address it currently has, so pass it with
--server when reconciling a cloud lab outside a run.

The exit code is 0 when every master already agreed, 1 when any difference was found, whether or not it was
reconciled, and 2 on a usage or input error.

overlay_ctl.py's "dns-sync" is the blunter instrument for a WireGuard overlay: it replaces every site's copy of the
zone with this automation node's, which is right when one automation node drives every site and so holds every
record, and wrong when each site has its own, because this node's copy then never held the other sites' records.
This command compares the masters instead of assuming one of them is correct, so it is the one to use when more
than one automation node writes the zone.
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
from lab_creation import log, die  # noqa: E402
from services import DNSService  # noqa: E402

USAGE = "Usage: dns_sync.py [--check|--prune] [--server <address>]... <lab.json>\n"


def zone_of(definition):
    """The lab's domain and reverse-zone prefix, from `common`, which is where a lab states them."""
    common = definition.get("common") or {}
    mydomain = common.get("mydomain") or ""
    mynet_reverse = common.get("mynet_reverse") or ""
    if not mydomain:
        die("this lab definition states no 'mydomain', so it names no zone to reconcile")
    return mydomain, mynet_reverse


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return 0
    check_only = "--check" in argv
    prune = "--prune" in argv
    if check_only and prune:
        sys.stderr.write("--check and --prune ask for opposite things\n" + USAGE)
        return 2
    extra_servers = []
    rest = []
    expect_server = False
    for arg in argv:
        if expect_server:
            extra_servers.append(arg)
            expect_server = False
        elif arg == "--server":
            expect_server = True
        elif arg not in ("--check", "--prune"):
            rest.append(arg)
    if expect_server or len(rest) != 1:
        sys.stderr.write(USAGE)
        return 2

    definition = primary.load_definition(rest[0])
    mydomain, mynet_reverse = zone_of(definition)
    config = primary.load_config() or {}
    masters = (config.get("REMOTE_DNS_SERVERS", "") or "").split() + extra_servers

    service = DNSService()
    log("Zone '{}' across this host and {} other master(s)".format(mydomain, len(masters)))
    if check_only:
        differences = service.report_zone(mydomain, mynet_reverse, remote_dns_servers=masters)
    else:
        differences = service.sync_zone(mydomain, mynet_reverse, remote_dns_servers=masters)
        if prune:
            differences += service.prune_zone(mydomain, mynet_reverse, remote_dns_servers=masters)
    if not differences:
        log("Every DNS master of '{}' holds the same records".format(mydomain))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
