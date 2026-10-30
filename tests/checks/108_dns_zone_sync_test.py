#!/usr/bin/env python3
# Unit tests for libs/dns_zone.py's zone shape and libs/services.py's DNS master handling: the records two
# masters are compared on, zone creation before any record is written, the reachability gate, the additive
# sync, the deliberate prune, and the reported removal that could not reach a master. Every SSH call and
# every zone file is faked, so nothing here touches a real BIND, a network or /etc.
# Run from 108_dns_zone_sync.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import dns_zone  # noqa: E402
import services  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult(object):
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── dns_zone: what a zone looks like and what is compared ────────────────────

check("the forward zone file is named after the domain", dns_zone.forward_file("mydemo.lab") == "mydemo.lab.lan")
check("the reverse zone file is named after the network", dns_zone.reverse_file("88.168.192") == "88.168.192.db")
check("the reverse zone name carries the in-addr.arpa suffix",
      dns_zone.reverse_zone("88.168.192") == "88.168.192.in-addr.arpa")

header = dns_zone.forward_header("mydemo.lab")
check("a new zone starts with a TTL", header[0].startswith("$TTL "))
check("...an SOA", any("IN SOA" in line for line in header))
check("...an NS", any("IN NS" in line for line in header))
check("...and an address for that NS, which BIND needs to resolve inside the zone",
      any(line.startswith("ns IN A") for line in header))
check("a cloud zone's TTL is the one it was asked for",
      dns_zone.forward_header("mydemo.lab", ttl=300)[0] == "$TTL 300")

stanza = dns_zone.stanza("mydemo.lab", "mydemo.lab.lan")
check("the stanza declares the zone a master of its file",
      'zone "mydemo.lab"' in stanza and "type master" in stanza and 'file "mydemo.lab.lan"' in stanza)

ZONE = "\n".join(dns_zone.forward_header("mydemo.lab")) + """
        IN  MX 10   automation.mydemo.lab.
node101         IN  A       192.168.88.101
rancher\tIN A  192.168.88.102
; a comment
"""
records = dns_zone.records(ZONE)
check("a record is compared with its whitespace collapsed",
      "node101 IN A 192.168.88.101" in records and "rancher IN A 192.168.88.102" in records)
check("the header is not compared: it is per server",
      not any(r.startswith("ns ") or "SOA" in r or r.startswith("IN") for r in records))
check("a comment is not a record", len(records) == 2)

REVERSE = "\n".join(dns_zone.reverse_header("mydemo.lab", "88.168.192")) + """
        IN  PTR     mydemo.lab.
101      IN  PTR     node101.mydemo.lab.
"""
check("a PTR record is compared although it starts with a digit",
      dns_zone.records(REVERSE) == {"101 IN PTR node101.mydemo.lab."})

check("an empty zone file has no records", dns_zone.records("") == set())

pattern = dns_zone.pattern("node101 IN A 192.168.88.101").replace("[[:space:]]", r"\s")
import re  # noqa: E402
check("the pattern matches the line a record was read from",
      re.match(pattern, "node101         IN  A       192.168.88.101") is not None)
check("...and not a longer name", re.match(pattern, "node1010 IN A 192.168.88.101") is None)
check("...and not a different address", re.match(pattern, "node101 IN A 192.168.88.1016") is None)

# ── masters: an unreachable one is left out, and probed once ─────────────────

svc = services.DNSService()
services.DNSService._reachable_cache = {}
probes = []


def _probe(host, cmd, check=True, capture=False, **kw):
    probes.append(host)
    return FakeResult(0 if host == "up.lab" else 255)


with mock.patch.object(services, "clean_ssh_keys", lambda *a: None), \
        mock.patch.object(svc, "_remote", side_effect=_probe):
    first = svc.masters(["up.lab", "down.lab"])
    second = svc.masters(["up.lab", "down.lab"])
check("an unreachable master is left out of the list", first == ["up.lab"] and second == ["up.lab"])
check("a reachable master is probed once per process, not once per write", probes.count("up.lab") == 1)
check("an unreachable one is retried once with its host key dropped, then not probed again",
      probes.count("down.lab") == 2)
check("no remote servers means no masters and no probes", svc.masters(None) == [])

# ── ensure_zone: a master holds the zone before a record is written ──────────

services.DNSService._reachable_cache = {}
svc = services.DNSService()
declared = {"mydemo.lab"}
created = []

with mock.patch.object(svc, "_zone_declared", side_effect=lambda server, zone: zone in declared), \
        mock.patch.object(svc, "_create_local_zone", side_effect=lambda *a: created.append(a)), \
        mock.patch.object(svc, "_create_remote_zone", side_effect=lambda *a: created.append(a)), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    already = svc.ensure_zone(None, "mydemo.lab")
    check("a zone the server already declares is left alone", already is False and created == [])
    fresh = svc.ensure_zone(None, "other.lab")
    check("a zone it does not declare is created, and the caller is told", fresh is True and len(created) == 1)
    created[:] = []
    svc.ensure_zone(None, "other.lab", "88.168.192")
    check("the reverse zone is created too when the lab states one", len(created) == 2)

# ── sync_zone: records are added everywhere, never removed ───────────────────

services.DNSService._reachable_cache = {}
svc = services.DNSService()
held = {
    None: {"node101 IN A 192.168.88.101", "node102 IN A 192.168.88.102"},
    "dns2.lab": {"node101 IN A 192.168.88.101", "stale IN A 192.168.88.9"},
}
written = []

with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(held[server])), \
        mock.patch.object(svc, "_write_records", side_effect=lambda s, f, r: written.append((s, tuple(r)))), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    messages = svc.sync_zone("mydemo.lab", remote_dns_servers=["dns2.lab"])

check("the record only this host had is written to the master that lacked it",
      ("dns2.lab", ("node102 IN A 192.168.88.102",)) in written)
check("the record only the other master had is written here",
      (None, ("stale IN A 192.168.88.9",)) in written)
check("nothing is removed by a sync", all("removed" not in m for m in messages))
check("every difference is reported", len(messages) == 4)

written[:] = []
same = {None: {"a IN A 1.1.1.1"}, "dns2.lab": {"a IN A 1.1.1.1"}}
with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(same[server])), \
        mock.patch.object(svc, "_write_records", side_effect=lambda s, f, r: written.append(s)), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    check("masters that already agree produce no report and no write",
          svc.report_zone("mydemo.lab", remote_dns_servers=["dns2.lab"]) == []
          and svc.sync_zone("mydemo.lab", remote_dns_servers=["dns2.lab"]) == []
          and written == [])

reported = []
with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(held[server])), \
        mock.patch.object(svc, "_write_records", side_effect=lambda s, f, r: reported.append(s)):
    check("report_zone finds the same differences and writes nothing",
          len(svc.report_zone("mydemo.lab", remote_dns_servers=["dns2.lab"])) == 4 and reported == [])

# ── prune_zone: the deliberate opposite ─────────────────────────────────────

removed = []
with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(held[server])), \
        mock.patch.object(svc, "_dns_remove_record", side_effect=lambda f, r: removed.append((None, r))), \
        mock.patch.object(svc, "_remote_dns_remove_record",
                          side_effect=lambda s, f, r: removed.append((s, r))), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    pruned = svc.prune_zone("mydemo.lab", remote_dns_servers=["dns2.lab"])
check("prune removes the record a single master holds alone",
      ("dns2.lab", "stale IN A 192.168.88.9") in removed)
check("...on either side", (None, "node102 IN A 192.168.88.102") in removed)
check("...and says so", len(pruned) == 2)

with mock.patch.object(svc, "masters", lambda remote=None: []), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: {"a IN A 1.1.1.1"}), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    check("with one master there is nothing to prune against, so nothing is removed",
          svc.prune_zone("mydemo.lab", remote_dns_servers=None) == [])

# ── del_from_dns: a removal that could not reach a master is reported ───────

services.DNSService._reachable_cache = {}
svc = services.DNSService()
warnings = []
with mock.patch.object(services, "warn", side_effect=lambda m: warnings.append(m)), \
        mock.patch.object(services, "log", lambda m: None), \
        mock.patch.object(svc, "masters", lambda remote=None: ["up.lab"]), \
        mock.patch.object(svc, "_remote", side_effect=lambda *a, **kw: FakeResult()), \
        mock.patch.object(svc, "_dns_remove_line", lambda *a: None), \
        mock.patch.object(svc, "_dns_remove_service_lines", lambda *a: None), \
        mock.patch.object(svc, "_update_hosts_file", lambda *a: None), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    svc.del_from_dns("node101.mydemo.lab", "192.168.88.101", "mydemo.lab", "", ["up.lab", "down.lab"])
check("the master a removal never reached is named", any("down.lab" in w for w in warnings))
check("...and the reachable one is not reported as a problem", not any("'up.lab'" in w for w in warnings))
check("...and the report says no later run will fix it, because a sync only adds",
      any("no later run" in w for w in warnings))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all dns_zone_sync checks passed")
