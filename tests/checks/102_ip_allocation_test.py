#!/usr/bin/env python3.11
# Deterministic IP allocation — see 102_ip_allocation.sh.
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import ipalloc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


COMMON = {"mygw": "192.168.88.1", "mymask": "24", "mydns": "192.168.88.73", "mydomain": "mydemo.lab"}


def lab(**nodes):
    return {"common": dict(COMMON), "nodes": {name: dict(cfg) for name, cfg in nodes.items()}}


# ── the network comes from what the lab already says ───────────────────────
# No pool field: the gateway and the mask are already in the definition, and a third setting to keep in step with
# them is one more thing to get wrong.
check("the network is the gateway's own", str(ipalloc.lab_network(COMMON)) == "192.168.88.0/24")
check("a /25 gateway gives a /25", str(ipalloc.lab_network({"mygw": "10.0.0.130", "mymask": "25"}))
      == "10.0.0.128/25")
for bad in ({}, {"mygw": "192.168.88.1"}, {"mymask": "24"}, {"mygw": "nope", "mymask": "24"}):
    check("a lab that does not say has no network: {!r}".format(bad), ipalloc.lab_network(bad) is None)

# ── the same definition always gives the same addresses ────────────────────
net = ipalloc.lab_network(COMMON)
first = ipalloc.candidates("node101.mydemo.lab", net)
check("every usable address of the network is a candidate", len(first) == 254)
check("the order never changes", ipalloc.candidates("node101.mydemo.lab", net) == first)
check("another node starts somewhere else",
      ipalloc.candidates("node102.mydemo.lab", net)[0] != first[0])
check("the same name on another network lands somewhere else",
      ipalloc.candidates("node101.mydemo.lab", ipalloc.lab_network({"mygw": "10.1.2.1", "mymask": "24"}))[0]
      != first[0])
check("the candidates wrap around, so every address is reachable from any start",
      sorted(first) == sorted(str(ip) for ip in net.hosts()))
check("neither the network nor the broadcast address is a candidate",
      "192.168.88.0" not in first and "192.168.88.255" not in first)

with mock.patch.object(ipalloc, "local_ips", return_value=set()), \
     mock.patch.object(ipalloc, "dns_ips", return_value=set()):
    taken_twice = [ipalloc.allocate("n1.mydemo.lab", lab(**{"n1.mydemo.lab": {}}), probe=False) for _ in range(3)]
check("allocating twice gives the same answer, so a rebuilt lab keeps its address",
      len(set(taken_twice)) == 1 and taken_twice[0])

# ── what is ruled out ───────────────────────────────────────────────────────
with mock.patch.object(ipalloc, "local_ips", return_value=set()), \
     mock.patch.object(ipalloc, "dns_ips", return_value=set()):
    definition = lab(**{"n1.mydemo.lab": {}})
    wanted = ipalloc.allocate("n1.mydemo.lab", definition, probe=False)

    check("the gateway and the DNS server are never handed out",
          ipalloc.definition_ips(definition) >= {"192.168.88.1", "192.168.88.73"})

    other = lab(**{"n1.mydemo.lab": {}, "other": {"myip": wanted}})
    check("an address another node of this lab uses is skipped",
          ipalloc.allocate("n1.mydemo.lab", other, probe=False) != wanted)

    check("an address the backend reports in use is skipped",
          ipalloc.allocate("n1.mydemo.lab", definition, used={wanted}, probe=False) != wanted)

    with mock.patch.object(ipalloc, "answers_ping", side_effect=lambda ip: ip == wanted):
        check("an address that answers a ping is skipped, not warned about",
              ipalloc.allocate("n1.mydemo.lab", definition, probe=True) != wanted)
    with mock.patch.object(ipalloc, "answers_ping", return_value=False) as quiet:
        ipalloc.allocate("n1.mydemo.lab", definition, probe=True)
        check("the ping gate is used when it is asked for", quiet.called)
        quiet.reset_mock()
        ipalloc.allocate("n1.mydemo.lab", definition, probe=False)
        check("...and not when it is not", not quiet.called)

with mock.patch.object(ipalloc, "dns_ips", return_value=set()), \
     mock.patch.object(ipalloc, "local_ips", return_value={wanted}):
    check("this automation node's own address is never handed to a VM",
          ipalloc.allocate("n1.mydemo.lab", lab(**{"n1.mydemo.lab": {}}), probe=False) != wanted)

# The lab DNS is the ledger: the record is written before the VM exists, so an address in the zone is taken even
# though nothing answers on it yet.
with tempfile.TemporaryDirectory() as tmp:
    zone = Path(tmp) / "mydemo.lab.lan"
    zone.write_text("$TTL 86400\n@ IN SOA ns admin ( 1 )\nnode9         IN  A       192.168.88.9\n"
                    "; node8      IN  A       192.168.88.8\nsomething   IN  CNAME   node9\n")
    found = ipalloc.dns_ips("mydemo.lab", zone_dir=tmp)
    check("an address with an A record in the zone is taken", "192.168.88.9" in found)
    check("a lab with no domain reads no zone", ipalloc.dns_ips("", zone_dir=tmp) == set())
    check("a zone that is not there is not an error", ipalloc.dns_ips("absent.lab", zone_dir=tmp) == set())
    with mock.patch.object(ipalloc, "local_ips", return_value=set()):
        check("the zone's addresses are kept away from a new node",
              ipalloc.allocate("n1.mydemo.lab", lab(**{"n1.mydemo.lab": {}}), probe=False,
                               zone_dir=tmp) != "192.168.88.9")

# Teardown: a lab that never wrote the address down still has to remove the DNS records it created. A libvirt host
# cannot be asked what address a VM was given, so the zone that was written before the VM existed is the way back.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "mydemo.lab.lan").write_text("node9         IN  A       192.168.88.9\n"
                                           "other       IN  CNAME   node9\n")
    check("an address the lab never wrote down is recovered from the zone",
          ipalloc.dns_address("node9.mydemo.lab", "mydemo.lab", zone_dir=tmp) == "192.168.88.9")
    check("a name the zone does not hold recovers nothing it could delete by mistake",
          ipalloc.dns_address("absent.mydemo.lab", "mydemo.lab", zone_dir=tmp) == "")
    check("a CNAME is not mistaken for an address",
          ipalloc.dns_address("other.mydemo.lab", "mydemo.lab", zone_dir=tmp) == "")
teardown = (_REPO / "scripts" / "destroy_vm.py").read_text()
check("teardown recovers the address of any node, not only a cloud one",
      "ipalloc.dns_address(vm_name, env.get(\"mydomain\", \"\"))" in teardown
      and teardown.index("ipalloc.dns_address") < teardown.index("backends.CLOUD_BACKEND_NAMES:\n        if not myip"))

# A second run of a lab that never wrote its addresses down has to reach the same answer. The node's own record is
# its address, not a conflict to move away from.
with tempfile.TemporaryDirectory() as tmp:
    definition2 = lab(**{"n1.mydemo.lab": {}})
    with mock.patch.object(ipalloc, "local_ips", return_value=set()):
        picked = ipalloc.allocate("n1.mydemo.lab", definition2, probe=False, zone_dir=tmp)
        Path(tmp, "mydemo.lab.lan").write_text("n1         IN  A       {}\n".format(picked))
        again = ipalloc.allocate("n1.mydemo.lab", definition2, probe=False, zone_dir=tmp)
    check("a node already in the lab DNS keeps the address it was given", again == picked)
    with mock.patch.object(ipalloc, "local_ips", return_value=set()):
        other = ipalloc.allocate("n2.mydemo.lab", definition2, probe=False, zone_dir=tmp)
    check("...while another node is still kept away from it", other != picked)

check("an address is read out of a tool's output without parsing its format",
      ipalloc.ips_in_text("192.168.88.5 dev br0 lladdr 52:54:00:aa:bb:cc REACHABLE") == {"192.168.88.5"})

# ── a lab with nowhere to take an address from ─────────────────────────────
check("a lab with no network allocates nothing",
      ipalloc.allocate("n1", {"common": {}, "nodes": {"n1": {}}}, probe=False) is None)
small = {"common": {"mygw": "10.0.0.1", "mymask": "30"}, "nodes": {"n1": {}}}
check("a network whose addresses are all taken allocates nothing",
      ipalloc.allocate("n1", small, used={"10.0.0.1", "10.0.0.2"}, probe=False) is None)

# ── the MAC is decided the same way, for the same reason ──────────────────
import lab_creation as lc  # noqa: E402

mac_a = lc._generate_unused_mac(set(), vm_name="node101.mydemo.lab")
check("a node's MAC comes from its name, so a rebuilt lab keeps its DHCP reservation",
      mac_a == lc._generate_unused_mac(set(), vm_name="node101.mydemo.lab"))
check("another node gets another one", lc._generate_unused_mac(set(), vm_name="node102.mydemo.lab") != mac_a)
check("it is a locally administered unicast address", int(mac_a.split(":")[0], 16) & 0x03 == 0x02)
walked = lc._generate_unused_mac({mac_a}, vm_name="node101.mydemo.lab")
check("a taken address walks to the next one derived from the same name, not to chance",
      walked != mac_a and walked == lc._generate_unused_mac({mac_a}, vm_name="node101.mydemo.lab"))
check("with no name it is still random, as it always was",
      lc._generate_unused_mac(set()) != lc._generate_unused_mac(set()))
check("the run asks for the node's own MAC by name",
      "_generate_unused_mac(used_macs, vm_name=vm_name)" in (_REPO / "libs" / "backends.py").read_text())

# ── the backend side, the same shape as the MAC it sits beside ─────────────
import backends  # noqa: E402

check("a cloud backend has nothing to allocate, as it has no MAC to generate",
      backends._cloud_no_ip("") == "" and backends._cloud_no_ip("1.2.3.4") == "1.2.3.4")
for name in ("AWSBackend", "GCPBackend", "HetznerBackend", "AlibabaBackend", "ScalewayBackend",
             "UpCloudBackend", "OVHcloudBackend", "ExoscaleBackend"):
    cls = getattr(backends, name)
    check("{} leaves addressing to its provider".format(name),
          cls.check_or_generate_ip(None, "vm", "", {}) == "")
check("a libvirt lab allocates, because nobody else will",
      backends.LibvirtBackend.check_or_generate_ip is backends.VMBackend.check_or_generate_ip)
check("the decision is held under a lock, like the MAC's", isinstance(backends._ip_lock, type(backends._mac_lock)))

class _Definition(dict):
    """A loaded lab definition, which knows its own source path (libs/primary.py's LabDefinition)."""

    source_path = "/tmp/lab.json"


definition = _Definition(lab(**{"n1.mydemo.lab": {}}))
definition_saved = []
with mock.patch.object(backends.primary, "save_definition", side_effect=lambda d: definition_saved.append(d) or "/tmp/x"), \
     mock.patch.object(ipalloc, "local_ips", return_value=set()), \
     mock.patch.object(ipalloc, "dns_ips", return_value=set()), \
     mock.patch.object(ipalloc, "answers_ping", return_value=False):
    allocated = backends._check_or_generate_ip(set(), "n1.mydemo.lab", "", definition)
check("the allocation is written into the definition every later step reads",
      allocated and definition["nodes"]["n1.mydemo.lab"]["myip"] == allocated)
check("...and saved beside the lab's own file, never over it", definition_saved)

with mock.patch.object(backends.primary, "save_definition") as never:
    check("an address the lab asked for by name is used as it is",
          backends._check_or_generate_ip(set(), "n1.mydemo.lab", "192.168.88.50", lab()) == "192.168.88.50")
    check("...and nothing is written for it", not never.called)

try:
    backends._check_or_generate_ip(set(), "n1", "", _Definition({"common": {}, "nodes": {"n1": {}}}))
    check("a lab with nowhere to allocate from is an error naming myip", False)
except SystemExit:
    pass

# ── the preflight says which address it will be, rather than refusing ─────
source = (_REPO / "libs" / "lab_creation.py").read_text()
check("the preflight previews the allocation instead of demanding myip",
      "will be allocated from" in source and "_previewed_ips" in source)
check("...and still demands it when there is no network to allocate from",
      "the lab has no 'mygw' and 'mymask' to allocate one" in source)
check("each previewed address is held against the next node's",
      "_previewed_ips.add(would_get)" in source)
_setup_lab = (_REPO / "scripts" / "setup_lab.py").read_text()
check("the run allocates before it validates",
      "json_file = phase_allocate_ips(definition, config, json_file)" in _setup_lab
      and _setup_lab.index("phase_allocate_ips(definition, config, json_file)")
      < _setup_lab.index("ok = lc.validate_lab_definition(")
      and "check_or_generate_ip" in (_REPO / "scripts" / "setup_vm.py").read_text())
# Every add-on is its own process reading the lab file from disk, so the run has to continue with the file the
# allocation was saved to. Confirmed live: with the original file, install_rancher saw no myip and its service DNS
# record was never written, while the install still reported success.
check("...and then continues with the file that holds the addresses",
      "return allocated" in _setup_lab and "primary.save_definition(definition)" in _setup_lab)

# ── the web UI asks the same allocator ────────────────────────────────────
import discovery  # noqa: E402

with mock.patch.object(discovery, "libs_dir", return_value=str(_REPO / "libs")):
    preview = discovery.preview_addresses(lab(**{"n1.mydemo.lab": {}, "n2.mydemo.lab": {},
                                                 "fixed": {"myip": "192.168.88.60"},
                                                 "ec2": {"backend": "aws"}}))
check("every VM with no address of its own is previewed", sorted(preview) == ["n1.mydemo.lab", "n2.mydemo.lab"])
check("two VMs are never previewed onto one address", len(set(preview.values())) == 2)
check("what the preview says is what the allocator says",
      preview["n1.mydemo.lab"] == ipalloc.allocate("n1.mydemo.lab", lab(**{"n1.mydemo.lab": {}}), probe=False))
with mock.patch.object(discovery, "libs_dir", return_value=str(_REPO / "libs")):
    check("a lab with no network previews nothing",
          discovery.preview_addresses({"common": {}, "nodes": {"n1": {}}}) == {})

app = (_REPO / "webui" / "htdocs" / "app.js").read_text()
# Leaving the field blank IS the request: there is no tick box to find, and the builder does not demand an address
# the run will allocate. It only insists when the lab has no network to take one from.
check("a blank address does not stop the builder",
      'errors.push("VM " + n.name + " has no IP address (myip), and the lab has no mygw and mymask' in app)
check("...and it does insist when there is no network to allocate from",
      'set(model.common, "mygw") && set(model.common, "mymask")' in app)
check("no tick box is needed any more",
      "autoAddr" not in app and "autoAddr" not in (_REPO / "webui" / "htdocs" / "index.html").read_text())
check("the canvas shows the address the run would use",
      "refreshPreviewAddresses" in app and "(allocated)" in app)
check("the preview is asked for whenever a VM has no address of its own",
      'i.type === "node" && i.name && !has(i.cfg, "myip")' in app)
check("the preview goes through the API, not a copy of the allocator in the browser",
      '"addresses"' in (_REPO / "webui" / "lib" / "api.py").read_text())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all IP-allocation checks passed")
