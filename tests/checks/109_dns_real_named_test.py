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
import socket
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

# ── add_to_dns keeps one A record per name: a recreated cloud node's new address replaces its old one ──

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
check("add_to_dns replaces the node's own earlier record rather than leaving it beside the new one: " +
      dig("+short", "A", NODE).strip(),
      dig("+short", "A", NODE).split() == [NEW])
check("...a name it is only a prefix of keeps its record",
      dig("+short", "A", "node10." + DOMAIN).split() == [OLD])
check("...and a service record at the old address is retargeted to the new one too",
      dig("+short", "A", "svc1." + DOMAIN).split() == [NEW])
check("the zone still loads after add_to_dns replaces a record", "status: NOERROR" in dig("SOA", DOMAIN))

# The same two steps on a remote master (DNSService.add_to_dns()'s own _remote_set_node()/_remote_retarget_services())
# run as real sed commands over SSH. Run here, for real, on a copy of a zone file holding the same records, so the
# commands themselves are what is checked rather than their text.
remote_copy = tmp / "remote.lan"
remote_copy.write_text("node1         IN  A       {o}\nnode10   IN  A   {o}\nsvc1\tIN A  {o}\n".format(o=OLD))
sent = []
svc._remote = lambda server, cmd, check=True, capture=False: sent.append(cmd) or run("bash", "-c", cmd)
svc._remote_set_node("dns2.lab", str(remote_copy), "node1", dns_zone.node_record("node1", NEW))
svc._remote_retarget_services("dns2.lab", str(remote_copy), OLD, NEW)
check("the remote set+retarget runs two commands on the master", len(sent) == 2)
remote_records = dns_zone.records(remote_copy.read_text())
check("the remote update leaves the new node record, its neighbour, and its retargeted service: {}".format(
      sorted(remote_records)),
      remote_records == {"node1 IN A " + NEW, "node10 IN A " + OLD, "svc1 IN A " + NEW})

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

# ── a declared zone whose file never got a header is repaired, and then answers ─────────────────────────
# The shape a live cloud lab's zone was found in: declared in named.conf, its file holding A records and nothing
# else, because records reached it before its header did. BIND rejects such a zone whole, ensure_zone used to skip
# it as already declared, and so nothing ever repaired it.
forward.write_text("\n".join(line for line in intact.splitlines()
                             if line.split() and line.split()[0] not in ("$TTL", "@", "IN", "ns")) + "\n"
                   + "orphan IN A 192.0.2.77\n")
check("the reproduced zone has records and no SOA", not dns_zone.has_header(forward.read_text())
      and "orphan" in forward.read_text())
reload_named()
check("...and a real named rejects it whole", "status: SERVFAIL" in dig("SOA", DOMAIN))
before = len(re.findall(r'zone "{}"'.format(re.escape(DOMAIN)), named_conf.read_text()))
check("ensure_zone repairs it without calling it created, since its records were already there",
      svc.ensure_zone(None, DOMAIN, REVERSE) is False)
check("the repaired zone has a header in front of every record it held",
      dns_zone.has_header(forward.read_text()) and "orphan IN A 192.0.2.77" in forward.read_text())
check("the repair declares nothing twice",
      len(re.findall(r'zone "{}"'.format(re.escape(DOMAIN)), named_conf.read_text())) == before)
repaired = run("named-checkzone", DOMAIN, str(forward))
check("named-checkzone accepts the repaired zone: " + repaired.stdout.strip(), repaired.returncode == 0)
check("named serves the repaired zone's SOA", "status: NOERROR" in dig("SOA", DOMAIN))
check("...and answers the record that was there before the header",
      "192.0.2.77" in dig("+short", "A", "orphan." + DOMAIN))
text = forward.read_text()
check("a second ensure_zone leaves a zone with a header untouched",
      svc.ensure_zone(None, DOMAIN, REVERSE) is False and forward.read_text() == text)

# The same repair on a remote master is one shell command; run it in a real shell against real files.
remote_file = tmp / "remote.zone"
remote_file.write_text("orphan IN A 192.0.2.77\n")
header = dns_zone.forward_header(DOMAIN)
first = run("sh", "-c", services._header_command(str(remote_file), header))
check("the remote command adds a header to a file with none and exits 0",
      first.returncode == 0 and dns_zone.has_header(remote_file.read_text())
      and "orphan IN A 192.0.2.77" in remote_file.read_text())
check("...which named-checkzone accepts", run("named-checkzone", DOMAIN, str(remote_file)).returncode == 0)
text = remote_file.read_text()
again = run("sh", "-c", services._header_command(str(remote_file), header))
check("the remote command exits 3 and changes nothing when the header is there", again.returncode == 3
      and remote_file.read_text() == text)
missing = tmp / "missing.zone"
check("the remote command creates a missing file with just the header",
      run("sh", "-c", services._header_command(str(missing), header)).returncode == 0
      and dns_zone.has_header(missing.read_text()))
chained = run("sh", "-c", services._header_command(str(remote_file), header) + "; echo after")
check("its exit stops no command chained after it", "after" in chained.stdout)

forward.write_text(intact)
reload_named()

# ── a view master: one site's clients get its private addresses, everyone else the public ones ──────────
# The "remote" master is this container itself: every command DNSService would send over SSH runs in a local shell,
# against the real /etc and zone directory, while "this host" keeps its own copies in a separate directory. systemctl
# is a stand-in that restarts a daemonized named, since the container has no systemd.
VIEW_PORT = 5355
SERVER = "dnsvm.test"
SITE = "aws-acct1"
VIEW_DOMAIN = "sites.lab"
OTHER_DOMAIN = "other.lab"
PUBLIC, PRIVATE, KVM_ADDRESS = "198.51.100.10", "172.31.5.10", "192.168.88.20"
os_release = {k: v.strip('"') for k, v in (line.split("=", 1) for line in Path("/etc/os-release").read_text()
                                           .splitlines() if "=" in line)}
OS_ID, OS_LIKE = os_release.get("ID", ""), os_release.get("ID_LIKE", "")
remote_zone_dir = Path(dns_zone.zone_dir(OS_ID, OS_LIKE))
main_conf = Path(dns_zone.main_named_conf(OS_ID, OS_LIKE))
ext_conf = Path(dns_zone.remote_named_conf(OS_ID, OS_LIKE))
site_conf = Path(dns_zone.site_named_conf(OS_ID, OS_LIKE))
pid_file = tmp / "named-views.pid"
for path in (main_conf, ext_conf, site_conf, Path(dns_zone.SITE_MARKER)):
    if path.exists():
        path.unlink()
for name in (dns_zone.forward_file(VIEW_DOMAIN), dns_zone.site_file(VIEW_DOMAIN, SITE),
             dns_zone.forward_file(OTHER_DOMAIN)):
    if (remote_zone_dir / name).exists():
        (remote_zone_dir / name).unlink()
# named keeps no capability to write a directory it does not own, even started as root.
keys_dir = Path(dns_zone.named_dirs(OS_ID, OS_LIKE)[1])
keys_dir.mkdir(parents=True, exist_ok=True)
keys_dir.chmod(0o777)
# A client outside the site is this container's own non-loopback address, the loopback one being the site's.
_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_probe.connect(("192.0.2.1", 9))
OUTSIDE_SRC = _probe.getsockname()[0]
_probe.close()

stubs = tmp / "bin"
stubs.mkdir()
(stubs / "systemctl").write_text("""#!/bin/sh
pidf={pid}
alive() {{ [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; }}
case "$1" in
  cat) exit 0 ;;
  is-active) alive; exit ;;
  restart)
    if alive; then kill "$(cat "$pidf")"; for i in $(seq 50); do alive || break; sleep 0.1; done; fi
    rm -f "$pidf"
    named -c {conf} >/dev/null 2>&1 || exit 1
    for i in $(seq 50); do
      alive && ss -lnu | grep -q ':{port} ' && exit 0
      sleep 0.1
    done
    exit 1 ;;
esac
exit 0
""".format(pid=pid_file, conf=main_conf, port=VIEW_PORT))
(stubs / "systemctl").chmod(0o755)
os.environ["PATH"] = "{}:{}".format(stubs, os.environ["PATH"])

# A DNS VM as it was before views: the distribution's own named.conf with this project's include, and a zone of
# another lab that the site view must keep answering once it exists.
(remote_zone_dir / dns_zone.forward_file(OTHER_DOMAIN)).write_text(
    "\n".join(dns_zone.forward_header(OTHER_DOMAIN)) + "\n" + dns_zone.node_record("old", "203.0.113.5") + "\n")
ext_conf.write_text(dns_zone.stanza(OTHER_DOMAIN, str(remote_zone_dir / dns_zone.forward_file(OTHER_DOMAIN))) + "\n")
view_options = ['listen-on port {} {{ any; }};'.format(VIEW_PORT), "listen-on-v6 { none; };",
                'pid-file "{}";'.format(pid_file), "dnssec-validation no;"]
main_conf.write_text("options {{\n\t{}\n}};\ninclude \"{}\";\n".format("\n\t".join(view_options), ext_conf))
run("systemctl", "restart", "named")


def local_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    """ssh_run() for SERVER, run in a local shell."""
    result = subprocess.run(["bash", "-c", cmd], input=input_text, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result


services.ssh_run = local_ssh_run
services.detect_os = lambda host: {"id": OS_ID, "like": OS_LIKE}
local_dir = tmp / "this-host"
local_dir.mkdir()
services.NAMED_ZONE_DIR = local_dir
services.NAMED_CONF = tmp / "this-host-named.conf"
services.DNSService._reachable_cache = {SERVER: True}
services.DNSService._view_sites = {}
views = services.DNSService()
views._update_hosts_file = lambda *a, **k: None


def ask(source, *args):
    return run("dig", "+time=2", "+tries=1", "-b", source, "@" + source, "-p", str(VIEW_PORT), *args).stdout


SITE_SRC = "127.0.0.1"
check("serve_site_view converts the DNS VM", views.serve_site_view(SERVER, SITE, [SITE_SRC], view_options))
check("...writes the site marker", Path(dns_zone.SITE_MARKER).read_text().strip() == SITE)
check("...and named-checkconf accepts the named.conf it wrote: " + run("named-checkconf", str(main_conf)).stdout,
      run("named-checkconf", str(main_conf)).returncode == 0)
check("the other lab's zone is still answered to the site after the conversion",
      "203.0.113.5" in ask(SITE_SRC, "+short", "A", "old." + OTHER_DOMAIN))
check("...and to everyone else", "203.0.113.5" in ask(OUTSIDE_SRC, "+short", "A", "old." + OTHER_DOMAIN))
services.DNSService._view_sites = {}
again = local_ssh_run(SERVER, "cat {}".format(main_conf)).stdout
check("a second serve_site_view finds it configured and changes nothing",
      views.serve_site_view(SERVER, SITE, [SITE_SRC], view_options) and main_conf.read_text() == again)
check("a configuration named refuses is not applied",
      views.serve_site_view(SERVER, "other-site", [SITE_SRC], view_options + ["no-such-option yes;"]) is False
      and main_conf.read_text() == again)
services.DNSService._view_sites = {}

views.add_to_dns("cloud1." + VIEW_DOMAIN, PUBLIC, VIEW_DOMAIN, "", remote_dns_servers=[SERVER],
                 site=SITE, private_ip=PRIVATE)
check("the site's own clients get a node of the site at its private address",
      ask(SITE_SRC, "+short", "A", "cloud1." + VIEW_DOMAIN).strip() == PRIVATE)
check("every other client gets its public address",
      ask(OUTSIDE_SRC, "+short", "A", "cloud1." + VIEW_DOMAIN).strip() == PUBLIC)
check("this host holds the site's view file too, without serving it",
      (local_dir / dns_zone.site_file(VIEW_DOMAIN, SITE)).is_file()
      and SITE not in services.NAMED_CONF.read_text())

views.add_to_dns("kvm1." + VIEW_DOMAIN, KVM_ADDRESS, VIEW_DOMAIN, "", remote_dns_servers=[SERVER])
check("a node outside any site is at its one address in both views",
      ask(SITE_SRC, "+short", "A", "kvm1." + VIEW_DOMAIN).strip() == KVM_ADDRESS
      and ask(OUTSIDE_SRC, "+short", "A", "kvm1." + VIEW_DOMAIN).strip() == KVM_ADDRESS)

lab = {"nodes": {"cloud1." + VIEW_DOMAIN: {"myip": PUBLIC, "kcluster": "c1"}}}
views.add_service_dns(lab, "c1", "rke2", "app", VIEW_DOMAIN, remote_dns_servers=[SERVER])
check("a service record follows its node: private in the site's view",
      ask(SITE_SRC, "+short", "A", "app." + VIEW_DOMAIN).strip() == PRIVATE)
check("...and public in the external one", ask(OUTSIDE_SRC, "+short", "A", "app." + VIEW_DOMAIN).strip() == PUBLIC)

views.add_to_dns("cloud1." + VIEW_DOMAIN, PUBLIC, VIEW_DOMAIN, "", remote_dns_servers=[SERVER],
                 site=SITE, private_ip="172.31.5.99")
check("a node re-registered at a new private address has only that one in its site's view",
      ask(SITE_SRC, "+short", "A", "cloud1." + VIEW_DOMAIN).split() == ["172.31.5.99"])
check("...and its service record moved with it",
      ask(SITE_SRC, "+short", "A", "app." + VIEW_DOMAIN).split() == ["172.31.5.99"])

views.del_from_dns("cloud1." + VIEW_DOMAIN, PUBLIC, VIEW_DOMAIN, "", remote_dns_servers=[SERVER])
check("del_from_dns removes the node from the site's view",
      ask(SITE_SRC, "+short", "A", "cloud1." + VIEW_DOMAIN).strip() == "")
check("...and from the external one", ask(OUTSIDE_SRC, "+short", "A", "cloud1." + VIEW_DOMAIN).strip() == "")
check("...and the service records pointing at it in both",
      ask(SITE_SRC, "+short", "A", "app." + VIEW_DOMAIN).strip() == ""
      and ask(OUTSIDE_SRC, "+short", "A", "app." + VIEW_DOMAIN).strip() == "")
check("the node outside any site is untouched", ask(SITE_SRC, "+short", "A", "kvm1." + VIEW_DOMAIN).strip()
      == KVM_ADDRESS)

# The configuration a real cloud DNS VM gets, with the production options, is one named accepts.
import backends  # noqa: E402
production = tmp / "production-named.conf"
production.write_text(dns_zone.view_named_conf(
    backends.cloud_dns_options(["10.0.0.0/8", "172.16.0.0/12"]),
    ["localhost", "localnets", "10.0.0.0/8", "172.16.0.0/12"], OS_ID, OS_LIKE))
checked = run("named-checkconf", str(production))
check("named-checkconf accepts a cloud DNS VM's real named.conf, without a warning: " + checked.stdout.strip(),
      checked.returncode == 0 and checked.stdout.strip() == "")

run("systemctl", "stop", "named")
if pid_file.exists():
    run("sh", "-c", "kill $(cat {}) 2>/dev/null".format(pid_file))

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
