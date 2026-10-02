#!/usr/bin/env python3
# LAB_HOSTS_FILE — see 64_lab_hosts_file.sh.
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import primary  # noqa: E402
import services  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    services.NAMED_ZONE_DIR = tmp
    hosts = tmp / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    dns = services.DNSService()
    dns.restart_named = lambda remote_servers=None: None

    primary.load_config = lambda paths=None: {}
    dns.add_to_dns("vm1.lab", "10.0.0.5", "lab", "0.0.10")
    check("feature off without LAB_HOSTS_FILE", hosts.read_text() == "127.0.0.1 localhost\n")

    primary.load_config = lambda paths=None: {"LAB_HOSTS_FILE": str(hosts)}
    dns.add_to_dns("vm1.lab", "10.0.0.5", "lab", "0.0.10")
    dns.add_to_dns("vm10.lab", "10.0.0.6", "lab", "0.0.10")
    text = hosts.read_text()
    check("line added", "10.0.0.5  vm1.lab vm1  # lab-in-a-box" in text)
    check("existing content kept", text.startswith("127.0.0.1 localhost\n"))

    dns.add_to_dns("vm1.lab", "10.0.0.9", "lab", "0.0.10")      # new IP (cloud re-create)
    text = hosts.read_text()
    check("re-registration replaces the old IP", "10.0.0.5" not in text and "10.0.0.9  vm1.lab vm1" in text)
    check("vm10 untouched by vm1 updates", "10.0.0.6  vm10.lab vm10" in text)

    dns.del_from_dns("vm1.lab", "10.0.0.9", "lab", "0.0.10")
    text = hosts.read_text()
    check("removed on delete", "vm1.lab" not in text and "vm10.lab" in text)

    def broken(paths=None):
        raise SystemExit("no cfg")
    primary.load_config = broken
    dns.add_to_dns("vm2.lab", "10.0.0.7", "lab", "0.0.10")
    check("no/invalid cfg means off, not a crash", "vm2.lab" not in hosts.read_text())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all LAB_HOSTS_FILE checks passed")
