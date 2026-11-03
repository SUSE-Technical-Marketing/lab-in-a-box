#!/usr/bin/env python3
# Tests libs/dns_zone.py and libs/services.py's zone creation against a real, running BIND rather than a fake:
# named-checkconf and named-checkzone accept the generated named.conf stanza and zone header, named loads the
# zones, and dig answers the records add_to_dns writes and stops answering those del_from_dns removes.
#
# named's own "directory" is deliberately set to a path that is NOT where the zone files are written, which is the
# Debian and Ubuntu layout (/var/cache/bind): a stanza naming its file relatively loads on SUSE and fails there.
#
# Run from 109_dns_real_named.sh, in its own container — see tests/run_tests.sh.
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import dns_zone  # noqa: E402
import services  # noqa: E402

DOMAIN = "mydemo.lab"
REVERSE = "88.168.192"
NODE = "node1." + DOMAIN
ADDRESS = "192.168.88.11"
PORT = 5354

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)
    else:
        print("ok:", desc)


def run(*cmd):
    return subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)


tmp = Path(tempfile.mkdtemp(prefix="dns-real-"))
named_conf = tmp / "named.conf"
# "directory" points somewhere the zone files are NOT, so a stanza that named its file relatively would fail to
# load exactly as it does on Debian and Ubuntu.
elsewhere = tmp / "named-directory"
elsewhere.mkdir()
named_conf.write_text(
    'options {{\n'
    '    directory "{d}";\n'
    '    pid-file "{t}/named.pid";\n'
    '    listen-on port {p} {{ 127.0.0.1; }};\n'
    '    listen-on-v6 {{ none; }};\n'
    '    recursion no;\n'
    '    dnssec-validation no;\n'
    '}};\n'.format(d=elsewhere, t=tmp, p=PORT))

zone_dir = Path(dns_zone.zone_dir())   # this container is openSUSE, so the default /var/lib/named
zone_dir.mkdir(parents=True, exist_ok=True)
for stale in (dns_zone.forward_file(DOMAIN), dns_zone.reverse_file(REVERSE)):
    if (zone_dir / stale).exists():
        (zone_dir / stale).unlink()

services.NAMED_CONF = named_conf
services.NAMED_ZONE_DIR = zone_dir

_named = {"proc": None}


def reload_named(remote_servers=None):
    """Stand in for restart_named(): the container has no systemd, so named is stopped and started directly."""
    if _named["proc"] is not None:
        _named["proc"].terminate()
        _named["proc"].wait()
    _named["proc"] = subprocess.Popen(["named", "-f", "-g", "-c", str(named_conf)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if run("dig", "+short", "+time=1", "+tries=1", "@127.0.0.1", "-p", str(PORT),
               "SOA", DOMAIN).returncode == 0:
            return
        time.sleep(0.2)


def dig(*args):
    return run("dig", "+time=2", "+tries=1", "@127.0.0.1", "-p", str(PORT), *args).stdout


# ── the zone files and the stanza, as BIND itself judges them ────────────────

svc = services.DNSService()
svc.restart_named = reload_named
services.DNSService._reachable_cache = {}

created = svc.ensure_zone(None, DOMAIN, REVERSE)
check("ensure_zone reports that it created the zones", created is True)
check("the forward zone file exists", (zone_dir / dns_zone.forward_file(DOMAIN)).is_file())
check("the reverse zone file exists", (zone_dir / dns_zone.reverse_file(REVERSE)).is_file())

conf_text = named_conf.read_text()
check("the stanza names the zone file by an absolute path, which is what makes it load whatever named's "
      "own directory is", '"{}"'.format(dns_zone.zone_path(dns_zone.forward_file(DOMAIN))) in conf_text)

checkconf = run("named-checkconf", str(named_conf))
check("named-checkconf accepts the generated named.conf: " + checkconf.stdout.strip(), checkconf.returncode == 0)

fwd = run("named-checkzone", DOMAIN, str(zone_dir / dns_zone.forward_file(DOMAIN)))
check("named-checkzone accepts the generated forward zone: " + fwd.stdout.strip(), fwd.returncode == 0)
rev = run("named-checkzone", dns_zone.reverse_zone(REVERSE), str(zone_dir / dns_zone.reverse_file(REVERSE)))
check("named-checkzone accepts the generated reverse zone: " + rev.stdout.strip(), rev.returncode == 0)

check("ensure_zone is idempotent against a real named.conf", svc.ensure_zone(None, DOMAIN, REVERSE) is False)
check("the zone is declared exactly once after a second ensure_zone",
      conf_text.count('zone "{}"'.format(DOMAIN)) == 1
      and named_conf.read_text().count('zone "{}"'.format(DOMAIN)) == 1)

# ── a running named answers what the record paths write ──────────────────────

reload_named()
soa = dig("SOA", DOMAIN)
check("named loads the created zone and answers its SOA from the zone itself",
      "status: NOERROR" in soa and "flags:" in soa and " aa" in soa.split("flags:")[1].split(";")[0]
      and dig("+short", "SOA", DOMAIN).strip() != "")
check("the zone is served, not refused", "status: REFUSED" not in soa and "status: SERVFAIL" not in soa)

svc._update_hosts_file = lambda *a, **k: None
svc.add_to_dns(NODE, ADDRESS, DOMAIN, REVERSE)
reload_named()

answer = dig("+short", "A", NODE)
check("named answers the A record add_to_dns wrote: " + answer.strip(), ADDRESS in answer)
ptr = dig("+short", "-x", ADDRESS)
check("named answers the PTR record add_to_dns wrote: " + ptr.strip(), NODE in ptr)

svc.del_from_dns(NODE, ADDRESS, DOMAIN, REVERSE)
reload_named()
check("named no longer answers the A record after del_from_dns", ADDRESS not in dig("+short", "A", NODE))
check("named no longer answers the PTR record after del_from_dns", NODE not in dig("+short", "-x", ADDRESS))
check("the zone still loads after a removal",
      "status: NOERROR" in dig("SOA", DOMAIN))

# ── replace: a recreated cloud node's name points at its new address alone ────

# A record of an earlier instance of the same name, which nothing could remove because that instance's address
# was no longer known, plus neighbours that must survive: a name node1 is a prefix of, and a service record.
OLD, NEW = "203.0.113.7", "203.0.113.8"
forward = zone_dir / dns_zone.forward_file(DOMAIN)
with forward.open("a") as zone:
    zone.write("node1         IN  A       {}\n".format(OLD))
    zone.write("node10         IN  A       {}\n".format(OLD))
    zone.write("svc1\tIN A  {}\n".format(OLD))

svc.add_to_dns(NODE, NEW, DOMAIN, "")
reload_named()
both = dig("+short", "A", NODE).split()
check("without replace an earlier record of the name stays beside the new one, as before: {}".format(both),
      sorted(both) == sorted([OLD, NEW]))

svc.add_to_dns(NODE, NEW, DOMAIN, "", replace=True)
reload_named()
check("with replace the name resolves to its new address alone: " + dig("+short", "A", NODE).strip(),
      dig("+short", "A", NODE).split() == [NEW])
check("...a name it is only a prefix of keeps its record",
      dig("+short", "A", "node10." + DOMAIN).split() == [OLD])
check("...and so does a service record at the old address",
      dig("+short", "A", "svc1." + DOMAIN).split() == [OLD])
check("the zone still loads after a replace", "status: NOERROR" in dig("SOA", DOMAIN))

# The same removal on a remote master runs as one sed command over SSH. It is run here, for real, on a copy of a
# zone file holding the same records, so the command itself is what is checked rather than its text.
remote_copy = tmp / "remote.lan"
remote_copy.write_text("node1         IN  A       {o}\nnode1 IN A {n}\nnode10   IN  A   {o}\nsvc1\tIN A  {o}\n".format(
    o=OLD, n=NEW))
sent = []
svc._remote = lambda server, cmd, check=True, capture=False: sent.append(cmd) or run("bash", "-c", cmd)
svc._remote_dns_remove_other_a("dns2.lab", str(remote_copy), "node1", NEW)
check("the remote removal runs one command on the master", len(sent) == 1)
remote_records = dns_zone.records(remote_copy.read_text())
check("the remote removal leaves exactly the new record of the name and its neighbours: {}".format(
      sorted(remote_records)),
      remote_records == {"node1 IN A " + NEW, "node10 IN A " + OLD, "svc1 IN A " + OLD})

# ── zone validity, as named-checkzone itself judges it ──────────────────────

# A damaged header is the case record comparison cannot see: records() drops the header on purpose, because the
# SOA and the NS address are per server, so a master with no SOA agrees perfectly with the others and serves
# nothing at all. Only asking named tells them apart.
forward = zone_dir / dns_zone.forward_file(DOMAIN)
intact = forward.read_text()

loads, detail = svc.zone_loads(None, DOMAIN, dns_zone.forward_file(DOMAIN))
check("an intact zone loads: " + detail, loads is True)

forward.write_text("\n".join(line for line in intact.splitlines() if "SOA" not in line) + "\n")
loads, detail = svc.zone_loads(None, DOMAIN, dns_zone.forward_file(DOMAIN))
check("a zone whose SOA is gone is reported as not loading: " + detail, loads is False)
check("...and named's own reason is carried, not invented", "SOA" in detail)
check("record comparison cannot see it at all, which is why this check exists",
      dns_zone.records(intact) == dns_zone.records(forward.read_text()))

messages = svc.validate_zones(DOMAIN, REVERSE)
check("validate_zones names this host when its own zone does not load",
      len(messages) == 1 and "this host" in messages[0])

forward.write_text(intact)
check("the restored zone loads again", svc.zone_loads(None, DOMAIN, dns_zone.forward_file(DOMAIN))[0] is True)
check("...and validate_zones then reports nothing", svc.validate_zones(DOMAIN, REVERSE) == [])

if _named["proc"] is not None:
    _named["proc"].terminate()
    _named["proc"].wait()
shutil.rmtree(str(tmp), ignore_errors=True)

print()
if failures:
    print("{} FAILED".format(len(failures)))
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("all real-named DNS checks passed")
