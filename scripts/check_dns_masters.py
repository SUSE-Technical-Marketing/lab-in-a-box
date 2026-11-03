#!/usr/bin/env python3
# Part of lab-in-a-box — opt-in live check of the DNS remote-master path
# Author/s: Raul Mahiques
# License: GPLv3
"""
check_dns_masters.py: drive libs/services.py's DNS remote-master path at a real BIND server and ask that server,
with dig, whether the result is actually answered.

    check_dns_masters.py <server> [<ssh key>]

Not part of tests/run_tests.sh, and never run by the pre-commit hook: it needs a disposable server to write to.
Without arguments it prints this help and exits 0, the same opt-in shape as check_cloud_images.py.

Everything in DNSService that takes a `server` argument is faked in the test suite: _create_remote_zone's per-family
layout, _remote_dns_add, _remote_dns_remove_record's sed, zone_records read back over SSH, zone_loads and the
reachability gate's retry. A container with a real named proves the local path and the generated zone, but not that
a DIFFERENT distribution's named will read it — which is exactly where /var/lib/named was denied by AppArmor on
Debian and Ubuntu while named-checkzone called the zone fine.

THIS HOST'S OWN BIND IS NEVER TOUCHED. NAMED_ZONE_DIR and NAMED_CONF are redirected to a temporary directory and
the local named is never restarted, so the automation node's real zones are neither read nor written. That matters
more than it looks: run against this host's real zones instead, a sync would push every record it holds to the
server under test, and --prune would delete from the real zones every record that server does not have.

`server` must be a disposable VM running BIND, not a DNS server anything depends on: the zone "dnstest.lab" is
created, written, broken on purpose and pruned on it. Build one with:

    virt-customize -a <copy of a cloud image> --hostname dnsm-test \
        --mkdir /root/.ssh --write "/root/.ssh/authorized_keys:<pubkey>" \
        --chmod '0700:/root/.ssh' --chmod '0600:/root/.ssh/authorized_keys' \
        --install bind9   # or "bind" outside the Debian family
    virt-install --name dnsm-test --memory 1024 --vcpus 1 --disk <that image>,bus=virtio \
        --import --network bridge=br0,model=virtio --boot uefi --graphics none --noautoconsole

Use a throwaway key, not this machine's own. A UEFI domain will not boot a legacy BIOS image (the kvm-and-xen
variants), and a JeOS image without cloud-init stops at its firstboot wizard, so prefer a cloud image prepared
offline as above over a cloud-init seed.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEY = sys.argv[2] if len(sys.argv) > 2 else ""
SERVER = sys.argv[1] if len(sys.argv) > 1 else ""
DOMAIN = "dnstest.lab"
REVERSE = "88.168.192"
NODE = "rnode1." + DOMAIN
ADDRESS = "192.168.88.241"
NODE2 = "rnode2." + DOMAIN
ADDRESS2 = "192.168.88.242"

if not SERVER:
    print(__doc__.strip())
    sys.exit(0)

for _candidate in ("/usr/local/lib/lab_creation", str(REPO / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
import lab_creation  # noqa: E402
import dns_zone      # noqa: E402
import services      # noqa: E402

# Reach the disposable VM with the throwaway key only, and keep its host key out of this machine's known_hosts.
# A key given here is used instead of this host's own, and its host-key file is kept beside it rather than in
# this machine's known_hosts, since the server is disposable and its key is of no interest afterwards.
if KEY:
    lab_creation._SSH_BASE = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o",
                              "UserKnownHostsFile=" + str(Path(KEY).parent / "known_hosts"),
                              "-o", "ConnectTimeout=5", "-q"]

tmp = Path(tempfile.mkdtemp(prefix="dns-remote-"))
services.NAMED_ZONE_DIR = tmp
services.NAMED_CONF = tmp / "named.conf"
services.NAMED_CONF.write_text("")

failures = []


def check(desc, cond):
    print(("ok:   " if cond else "FAIL: ") + desc)
    if not cond:
        failures.append(desc)


def dig(*args):
    out = subprocess.run(["dig", "+time=3", "+tries=1", "@" + SERVER] + list(args),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return out.stdout


def remote(cmd):
    return lab_creation.ssh_run(SERVER, cmd, check=False, capture=True).stdout or ""


svc = services.DNSService()
svc._resolve_families([SERVER])
R_FWD = svc._zone_path(SERVER, dns_zone.forward_file(DOMAIN))
R_REV = svc._zone_path(SERVER, dns_zone.reverse_file(REVERSE))
print("remote zone dir resolved to: {}".format(R_FWD))
# The local named is this automation node's own; only the remote one is restarted.
svc.restart_named = lambda remote_servers=None: [
    svc._remote(s, "systemctl restart named", check=False) for s in (remote_servers or [])]
svc._update_hosts_file = lambda *a, **k: None
svc._REACHABLE_DELAY = 0

print("== the master gate ==")
check("the live master is reachable and listed", svc.masters([SERVER]) == [SERVER])

print("\n== ensure_zone over SSH ==")
remote("rm -f /etc/named.lab.conf; sed -i '/dnstest.lab/d;/in-addr.arpa/d' /etc/bind/named.conf.local 2>/dev/null; "
       "rm -f {} {} /var/lib/named/*.lan /var/lib/named/*.db; systemctl restart named".format(R_FWD, R_REV))
services.DNSService._reachable_cache = {}
created = svc.ensure_zone(SERVER, DOMAIN, REVERSE)
check("ensure_zone reports it created the zones on the remote master", created is True)
conf = remote("cat /etc/bind/named.conf.local /etc/named.lab.conf 2>/dev/null")
check("the zone is declared on the remote master", 'zone "{}"'.format(DOMAIN) in conf)
check("...by the absolute path in the directory this master's own named may read: " + R_FWD, R_FWD in conf)
check("the reverse zone is declared too", dns_zone.reverse_zone(REVERSE) in conf)
check("the zone file exists on the remote master", "No such" not in remote("ls {}".format(R_FWD)))
check("named-checkzone on the master says its new zone loads", svc.zone_loads(SERVER, DOMAIN,
      dns_zone.forward_file(DOMAIN))[0] is True)
check("validate_zones reports nothing against the live master",
      svc.validate_zones(DOMAIN, REVERSE, remote_dns_servers=[SERVER]) == [])

time.sleep(2)
soa = dig("SOA", DOMAIN)
check("the remote named actually serves the created zone", "status: NOERROR" in soa)

print("\n== add_to_dns and del_from_dns over SSH ==")
svc.add_to_dns(NODE, ADDRESS, DOMAIN, REVERSE, remote_dns_servers=[SERVER])
time.sleep(2)
check("the master answers the A record written over SSH: " + dig("+short", "A", NODE).strip(),
      ADDRESS in dig("+short", "A", NODE))
check("the master answers the PTR record written over SSH: " + dig("+short", "-x", ADDRESS).strip(),
      NODE in dig("+short", "-x", ADDRESS))
check("zone_records reads the record back over SSH",
      any(ADDRESS in r for r in svc.zone_records(SERVER, dns_zone.forward_file(DOMAIN))))

svc.del_from_dns(NODE, ADDRESS, DOMAIN, REVERSE, remote_dns_servers=[SERVER])
time.sleep(2)
check("the master stops answering the A record after del_from_dns (remote sed)",
      ADDRESS not in dig("+short", "A", NODE))
check("the master stops answering the PTR record", NODE not in dig("+short", "-x", ADDRESS))
check("the zone still loads after the remote removal", "status: NOERROR" in dig("SOA", DOMAIN))

print("\n== sync_zone brings a master that fell behind back into step ==")
# This host holds a record the master never got, which is the drift a per-record write leaves behind.
svc._dns_add_line(services.NAMED_ZONE_DIR / dns_zone.forward_file(DOMAIN),
                  "{}         IN  A       {}".format(NODE2.split(".")[0], ADDRESS2))
reported = svc.report_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
check("report_zone names the record the master is missing",
      any(ADDRESS2 in m and "missing" in m for m in reported))
check("...and reporting changed nothing on the master", ADDRESS2 not in dig("+short", "A", NODE2))
svc.sync_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
time.sleep(2)
check("sync_zone writes it to the master, which then answers it: " + dig("+short", "A", NODE2).strip(),
      ADDRESS2 in dig("+short", "A", NODE2))
# This host's "zone" here is a temporary directory no named serves, so validate_zones rightly says this host
# answers nothing for it. What matters is that nothing is left to report against the live master.
remaining = svc.report_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
check("nothing is left to report against the master once it is in step",
      not any(SERVER in message for message in remaining))
check("...while this host's own unserved copy is still reported, which is the check doing its job",
      any("this host" in message and "answers nothing" in message for message in remaining))

print("\n== a broken zone on the master is reported, which no record comparison can see ==")
path = R_FWD
before = set(svc.zone_records(SERVER, dns_zone.forward_file(DOMAIN)))
remote("sed -i '/SOA/d' {}".format(path))
after = set(svc.zone_records(SERVER, dns_zone.forward_file(DOMAIN)))
check("stripping the master's SOA leaves its comparable records identical", before == after)
check("...so report_zone's record comparison alone would have said nothing",
      not any("missing" in m for m in svc._difference_messages(
          svc._compare_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER]))))
messages = svc.validate_zones(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
check("validate_zones names the master whose zone no longer loads",
      any(SERVER in m for m in messages))
check("...and carries named's own reason", any("SOA" in m for m in messages))

print("\n== prune_zone removes a record only the master holds ==")
remote("printf '%s\\n' 'stale         IN  A       192.168.88.249' >> {}; systemctl restart named".format(path))
svc.ensure_zone(SERVER, DOMAIN, REVERSE)
alone = svc.report_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
check("the record only the master holds is reported, not removed",
      any("192.168.88.249" in m and "no other master" in m for m in alone))
check("...and it is still there", "192.168.88.249" in remote("cat {}".format(path)))
svc.prune_zone(DOMAIN, REVERSE, remote_dns_servers=[SERVER])
check("prune_zone removes it from the master", "192.168.88.249" not in remote("cat {}".format(path)))

print("\n== an unreachable master is retried, reported, and left out ==")
services.DNSService._reachable_cache = {}
started = time.time()
dead = svc.masters(["192.168.88.239"])
elapsed = time.time() - started
check("an address nothing answers is left out of the masters", dead == [])
check("...within a bounded time rather than hanging (took {:.0f}s)".format(elapsed), elapsed < 90)
started = time.time()
svc.masters(["192.168.88.239"])
check("...and is not probed again in the same process", time.time() - started < 1)

print()
if failures:
    print("{} LIVE CHECK(S) FAILED".format(len(failures)))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL LIVE REMOTE-MASTER CHECKS PASSED")
