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

# Every zone path depends on the target's os-release, which services.py reads over SSH. Nothing here has a host
# to ask, so the default answer is an unknown family (the default zone directory); the blocks that exercise the
# detection itself patch this with their own.
services.detect_os = lambda hostname: {"id": "", "ver_id": "", "like": "", "ver_major": ""}

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

check("a zone file is named by an absolute path, so a stanza loads whatever named's own directory is",
      dns_zone.zone_path("mydemo.lab.lan") == "/var/lib/named/mydemo.lab.lan")

# Debian and Ubuntu confine named with AppArmor to /etc/bind for a zone it is the origin of, so a file written
# anywhere else is denied and the zone never loads however correctly it is declared.
check("a Debian master serves its zone from /etc/bind",
      dns_zone.zone_dir("debian") == "/etc/bind" and dns_zone.zone_dir("ubuntu") == "/etc/bind")
check("...matched through ID_LIKE too", dns_zone.zone_dir("linuxmint", "debian") == "/etc/bind")
check("the SUSE and Fedora families keep it in /var/lib/named",
      dns_zone.zone_dir("opensuse-leap") == "/var/lib/named"
      and dns_zone.zone_dir("rhel", "fedora") == "/var/lib/named")
check("an undetected family gets the default rather than nothing", dns_zone.zone_dir() == "/var/lib/named")
check("a Debian master declares its zone where its own named.conf already includes it",
      dns_zone.remote_named_conf("debian") == "/etc/bind/named.conf.local")
check("...and elsewhere in this project's own include file",
      dns_zone.remote_named_conf("opensuse-leap") == "/etc/named.lab.conf"
      and dns_zone.main_named_conf("opensuse-leap") == "/etc/named.conf")
check("this host declares a zone in named.conf itself on SUSE, which is where an automation node's zones are",
      dns_zone.local_named_conf("opensuse-leap") == "/etc/named.conf")

stanza = dns_zone.stanza("mydemo.lab", dns_zone.zone_path("mydemo.lab.lan"))
check("the stanza declares the zone a master of its file",
      'zone "mydemo.lab"' in stanza and "type master" in stanza and 'file "/var/lib/named/mydemo.lab.lan"' in stanza)

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


def _probe(host, attempts=3, delay=2, user="root", probe=None):
    probes.append((host, attempts))
    return host == "up.lab"


with mock.patch.object(services, "clean_ssh_keys", lambda *a: None), \
        mock.patch.object(services, "ssh_reachable", side_effect=_probe):
    first = svc.masters(["up.lab", "down.lab"])
    second = svc.masters(["up.lab", "down.lab"])
check("an unreachable master is left out of the list", first == ["up.lab"] and second == ["up.lab"])
check("a reachable master is probed once per process, not once per write",
      [host for host, _ in probes].count("up.lab") == 1)
check("an unreachable one is retried, then probed once more with its host key dropped",
      [attempts for host, attempts in probes if host == "down.lab"]
      == [services.DNSService._REACHABLE_ATTEMPTS, 1])
check("the retry budget is more than one attempt, so a booting server is waited for",
      services.DNSService._REACHABLE_ATTEMPTS > 1)
check("no remote servers means no masters and no probes", svc.masters(None) == [])

services.DNSService._reachable_cache = {}
svc = services.DNSService()
through = []
with mock.patch.object(services, "clean_ssh_keys", lambda *a: None), \
        mock.patch.object(svc, "_remote",
                          side_effect=lambda h, c, check=True, capture=False: through.append(h) or FakeResult(0)):
    svc.masters(["via.lab"])
check("the gate probes through the service's own SSH wrapper, so a caller that fakes it is honoured",
      through == ["via.lab"])

# ── ssh_reachable: only a failure to connect is worth retrying ───────────────

import lab_creation  # noqa: E402

calls = []


def _ssh(host, cmd, check=True, capture=False, user="root"):
    calls.append(host)
    return FakeResult({"up": 0, "down": lab_creation.SSH_NO_CONNECTION, "refusing": 1}[host])


with mock.patch.object(lab_creation, "ssh_run", side_effect=_ssh), \
        mock.patch.object(lab_creation.time, "sleep", lambda _s: None):
    check("a host that answers is reported reachable on the first try",
          lab_creation.ssh_reachable("up", attempts=3) is True and calls.count("up") == 1)
    calls[:] = []
    check("a host that never connects is retried the full budget",
          lab_creation.ssh_reachable("down", attempts=3) is False and calls.count("down") == 3)
    calls[:] = []
    check("a host that answered and whose command failed is not retried, since it would simply run again",
          lab_creation.ssh_reachable("refusing", attempts=3) is False and calls.count("refusing") == 1)

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
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
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
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(same[server])), \
        mock.patch.object(svc, "_write_records", side_effect=lambda s, f, r: written.append(s)), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    check("masters that already agree produce no report and no write",
          svc.report_zone("mydemo.lab", remote_dns_servers=["dns2.lab"]) == []
          and svc.sync_zone("mydemo.lab", remote_dns_servers=["dns2.lab"]) == []
          and written == [])

reported = []
with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
        mock.patch.object(svc, "zone_records", side_effect=lambda server, f: set(held[server])), \
        mock.patch.object(svc, "_write_records", side_effect=lambda s, f, r: reported.append(s)):
    check("report_zone finds the same differences and writes nothing",
          len(svc.report_zone("mydemo.lab", remote_dns_servers=["dns2.lab"])) == 4 and reported == [])

# ── prune_zone: the deliberate opposite ─────────────────────────────────────

removed = []
with mock.patch.object(svc, "masters", lambda remote=None: ["dns2.lab"]), \
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
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
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
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
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
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

# ── _create_remote_zone: each family's own layout, over SSH ─────────────────

services.DNSService._reachable_cache = {}
svc = services.DNSService()
sent = []
svc._os_info = {"suse.lab": {"id": "opensuse-leap", "like": ""}, "deb.lab": {"id": "debian", "like": ""}}
with mock.patch.object(svc, "_remote",
                       side_effect=lambda h, c, check=True, capture=False: sent.append((h, c)) or FakeResult(0)):
    svc._create_remote_zone("suse.lab", "mydemo.lab", "mydemo.lab.lan", ["$TTL 86400"])
    svc._create_remote_zone("deb.lab", "mydemo.lab", "mydemo.lab.lan", ["$TTL 86400"])
suse_cmd = next(c for h, c in sent if h == "suse.lab")
deb_cmd = next(c for h, c in sent if h == "deb.lab")
check("a SUSE master gets this project's own include file, declared from its named.conf",
      "/etc/named.lab.conf" in suse_cmd and 'include "/etc/named.lab.conf";' in suse_cmd)
check("...and its zone written where its named reads one", "/var/lib/named/mydemo.lab.lan" in suse_cmd)
check("a Debian master gets named.conf.local, which its own named.conf already includes, so no include is added",
      "/etc/bind/named.conf.local" in deb_cmd and "include" not in deb_cmd)
check("...and its zone written under /etc/bind, the only place its confined named may read one",
      "/etc/bind/mydemo.lab.lan" in deb_cmd and "/var/lib/named" not in deb_cmd)
check("the detection is not re-implemented in shell on the server",
      "/etc/os-release" not in suse_cmd and "/etc/os-release" not in deb_cmd)

# ── zone validity: records agreeing says nothing about the zone loading ─────

services.DNSService._reachable_cache = {}
svc = services.DNSService()

# named-checkzone's exit code decides, and only a real failure is a fault: 127 is the binary not being installed
# and 126 the server holding no such zone file, neither of which says the zone is broken.
codes = {"broken.lab": FakeResult(1, "zone mydemo.lab/IN: has no SOA record"),
         "bare.lab": FakeResult(127),
         "empty.lab": FakeResult(126),
         "good.lab": FakeResult(0, "ns.mydemo.lab. admin.mydemo.lab. 1 3600 900 604800 300")}
with mock.patch.object(svc, "_remote", side_effect=lambda host, cmd, check=True, capture=False: codes[host]):
    check("a zone named itself refuses to load is a fault",
          svc.zone_loads("broken.lab", "mydemo.lab", "f")[0] is False)
    check("...and the reason it gave is carried",
          "no SOA" in svc.zone_loads("broken.lab", "mydemo.lab", "f")[1])
    check("a server without named-checkzone is not reported, since its absence says nothing",
          svc.zone_loads("bare.lab", "mydemo.lab", "f")[0] is True)
    check("a server holding no such zone file is left to the record comparison to report",
          svc.zone_loads("empty.lab", "mydemo.lab", "f")[0] is True)
    check("a zone that loads is no fault", svc.zone_loads("good.lab", "mydemo.lab", "f")[0] is True)

services.DNSService._reachable_cache = {}
svc = services.DNSService()
with mock.patch.object(svc, "masters", lambda remote=None: ["broken.lab"]), \
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "zone_loads",
                          side_effect=lambda server, zone, f: (server is None, "has no SOA record")):
    messages = svc.validate_zones("mydemo.lab", remote_dns_servers=["broken.lab"])
check("a master whose zone does not load is named", len(messages) == 1 and "broken.lab" in messages[0])
check("...and the message says it answers nothing however well its records agree",
      "answers nothing" in messages[0])

# A master serving nothing must not be hidden by a clean record comparison, so report_zone reports it and still
# writes nothing.
services.DNSService._reachable_cache = {}
svc = services.DNSService()
reported = []
with mock.patch.object(services, "warn", side_effect=lambda m: reported.append(m)), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: ["'down.lab' cannot load its copy"]), \
        mock.patch.object(svc, "_compare_zone", lambda *a, **kw: []), \
        mock.patch.object(svc, "_write_records", lambda *a: (_ for _ in ()).throw(AssertionError("wrote"))):
    returned = svc.report_zone("mydemo.lab")
check("an unloadable zone is reported even when every record agrees",
      len(returned) == 1 and "cannot load" in returned[0])
check("...and reporting it writes nothing", any("cannot load" in m for m in reported))

# ── a record is written where the master's own named can read it ────────────

services.DNSService._reachable_cache = {}
svc = services.DNSService()
svc._os_info = {"deb.lab": {"id": "debian", "like": ""}, "suse.lab": {"id": "opensuse-leap", "like": ""}}
check("a Debian master's zone file is named under /etc/bind",
      svc._zone_path("deb.lab", "mydemo.lab.lan") == "/etc/bind/mydemo.lab.lan")
check("a SUSE master's is named under /var/lib/named",
      svc._zone_path("suse.lab", "mydemo.lab.lan") == "/var/lib/named/mydemo.lab.lan")
check("this host's own path comes from its own zone directory, not a master's",
      svc._zone_path(None, "mydemo.lab.lan") == str(services.NAMED_ZONE_DIR / "mydemo.lab.lan"))

# A family this operation did not resolve is read then rather than assumed, since the wrong directory means a
# record that is written and never answered.
services.DNSService._reachable_cache = {}
svc = services.DNSService()
svc._os_info = {}
with mock.patch.object(services, "detect_os", side_effect=lambda h: {"id": "ubuntu", "like": "debian"}):
    check("an unresolved master's family is detected rather than defaulted",
          svc._zone_path("new.lab", "mydemo.lab.lan") == "/etc/bind/mydemo.lab.lan")
check("...and kept for the rest of the operation", "new.lab" in svc._os_info)

services.DNSService._reachable_cache = {}
svc = services.DNSService()
with mock.patch.object(services, "detect_os", side_effect=lambda h: {"id": "debian", "like": ""}):
    svc._resolve_families(["a.lab", "b.lab"])
check("each master's family is resolved for the operation", sorted(svc._os_info) == ["a.lab", "b.lab"])
check("...and this host is not one of them", None not in svc._os_info)

written = []
services.DNSService._reachable_cache = {}
svc = services.DNSService()
with mock.patch.object(services, "detect_os", side_effect=lambda h: {"id": "debian", "like": ""}), \
        mock.patch.object(svc, "masters", lambda remote=None: ["deb.lab"]), \
        mock.patch.object(svc, "_resolve_families", lambda servers: None), \
        mock.patch.object(svc, "validate_zones", lambda *a, **kw: []), \
        mock.patch.object(svc, "ensure_zone", lambda *a, **kw: False), \
        mock.patch.object(svc, "_remote", side_effect=lambda h, c, check=True, capture=False:
                          written.append(c) or FakeResult(0)), \
        mock.patch.object(svc, "_dns_add_line", lambda *a: None), \
        mock.patch.object(svc, "_dns_remove_ptr_for_octet", lambda *a: None), \
        mock.patch.object(svc, "_update_hosts_file", lambda *a, **kw: None), \
        mock.patch.object(svc, "restart_named", lambda **kw: None):
    svc.add_to_dns("node101.mydemo.lab", "192.168.88.101", "mydemo.lab", "88.168.192", ["deb.lab"])
check("add_to_dns writes to the Debian master's own directory",
      any("/etc/bind/mydemo.lab.lan" in c for c in written))
check("...and never to this host's path on the master",
      not any("/var/lib/named" in c for c in written))

# ── zone_loads asks whether the master answers, not whether a file parses ───

services.DNSService._reachable_cache = {}
svc = services.DNSService()
svc._os_info = {"s.lab": {"id": "debian", "like": ""}}
# named-checkzone reads the file as root and unconfined, so a zone AppArmor denies named still parses: the only
# question that settles it is whether the server's own named answers for the zone.
with mock.patch.object(svc, "_run_on", side_effect=lambda server, cmd: FakeResult(0, "")):
    loads, detail = svc.zone_loads("s.lab", "mydemo.lab", "mydemo.lab.lan")
check("a running named that answers no SOA for the zone is a fault", loads is False)
check("...and the message says it is written but not served", "not served" in detail)
with mock.patch.object(svc, "_run_on",
                       side_effect=lambda server, cmd: FakeResult(0, "ns.mydemo.lab. admin.mydemo.lab. 1")):
    check("a master that answers the SOA is serving the zone",
          svc.zone_loads("s.lab", "mydemo.lab", "mydemo.lab.lan")[0] is True)
with mock.patch.object(svc, "_run_on", side_effect=lambda server, cmd: FakeResult(126)):
    check("a master holding no such zone file is left to the record comparison",
          svc.zone_loads("s.lab", "mydemo.lab", "mydemo.lab.lan")[0] is True)

# With nothing listening to ask (dig's exit code 9), the file check is the fallback rather than a false verdict.
calls = []


def _fallback(server, cmd):
    calls.append(cmd)
    if "dig" in cmd:
        return FakeResult(9)
    return FakeResult(1, "zone mydemo.lab/IN: has no SOA record")


with mock.patch.object(svc, "_run_on", side_effect=_fallback):
    loads, detail = svc.zone_loads("s.lab", "mydemo.lab", "mydemo.lab.lan")
check("with nothing listening, named-checkzone is the fallback", loads is False and "SOA" in detail)
check("...and it was asked only after dig could not be", len(calls) == 2 and "dig" in calls[0])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all dns_zone_sync checks passed")
