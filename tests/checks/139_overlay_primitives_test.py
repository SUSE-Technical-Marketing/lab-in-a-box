#!/usr/bin/env python3
# Unit tests for the four per-backend primitives the cross-cloud overlay needs from libs/backends.py:
#   get_private_ip(vm)          a node's address on the network its site routes
#   get_subnet_cidr(vm)         the range another site has to route to reach this one
#   ensure_ports_open(ports)    opening a port with no VM named (the overlay hub as an existing host)
#   disable_source_dest_check() clearing the provider filter that drops forwarded packets
# Every provider call is faked: no cloud account and no CLI is needed. What is asserted is which field each
# backend reads and which command it builds, since a wrong field name fails silently and the overlay just stops
# routing. The field names come from each provider's own published schema, not from memory.
# Also covers the shared guest-side helper the backends fall back to when the provider reports neither.
# Run from 139_overlay_primitives.sh, in its own container — see tests/run_tests.sh.
import contextlib
import subprocess
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


backends.log = lambda *a, **kw: None
_warnings = []
backends._lc.warn = lambda msg, *a, **kw: _warnings.append(msg)


def _cp(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


# ── the shared guest-side helper ─────────────────────────────────────────────
# One SSH round trip answers both questions: the address is the node's own, the prefix is its network's.
with mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, "10.20.30.40/22\n")):
    check("guest_primary_address: the guest's own interface address with its prefix",
          backends.guest_primary_address("h") == "10.20.30.40/22")
    check("guest_private_ip: the address without the prefix",
          backends.guest_private_ip("h") == "10.20.30.40")
    check("guest_subnet_cidr: the network, not the host address",
          backends.guest_subnet_cidr("h") == "10.20.28.0/22")

# A guest that answers nothing, or anything that is not an address with a prefix, is not an address.
for _bad in ("", "not an address", "10.20.30.40", "eth0", "fe80::1"):
    with mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, _bad + "\n")):
        check("guest_primary_address: {!r} is rejected rather than returned".format(_bad),
              backends.guest_primary_address("h") == "")
        check("guest_subnet_cidr: {!r} gives no CIDR".format(_bad), backends.guest_subnet_cidr("h") == "")

# An unreachable host is a "" answer, not an exception, so a caller falls back as on a silent provider.
with mock.patch.object(backends._lc, "ssh_run", side_effect=OSError("no route")):
    check("guest_primary_address: an unreachable host gives \"\", not an exception",
          backends.guest_primary_address("h") == "")

# It must ask the interface the guest actually routes out through, not a hardcoded device.
_asked = {}
with mock.patch.object(backends._lc, "ssh_run",
                       side_effect=lambda host, cmd, **kw: _asked.setdefault("cmd", cmd) and None or _cp(0, "1.2.3.4/8")):
    backends.guest_primary_address("h")
check("guest_primary_address: resolves the routing interface rather than assuming one",
      "route get" in _asked.get("cmd", "") and "addr show dev" in _asked.get("cmd", ""))


# ── every backend answers the whole interface ────────────────────────────────
_CLOUD = {
    "HetznerBackend": backends.HetznerBackend("tok"),
    "AWSBackend": backends.AWSBackend("eu-central-1", profile="lab"),
    "GCPBackend": backends.GCPBackend("proj", "europe-west1-b"),
    "AlibabaBackend": backends.AlibabaBackend("k", "s", "eu-central-1", "sg-1", "vsw-1"),
    "ScalewayBackend": backends.ScalewayBackend("sk", "proj", "fr-par-1"),
    "UpCloudBackend": backends.UpCloudBackend("u", "p", "de-fra1"),
    "OVHcloudBackend": backends.OVHcloudBackend("ak", "as", "ck", "svc", "GRA7"),
    "ExoscaleBackend": backends.ExoscaleBackend("k", "s", "ch-gva-2"),
}
for _name, _b in _CLOUD.items():
    for _m in ("get_private_ip", "get_subnet_cidr", "ensure_ports_open", "disable_source_dest_check"):
        check("{}: {} is callable".format(_name, _m), callable(getattr(_b, _m)))
    check("{}: get_private_ip is its own, not the base no-op".format(_name),
          type(_b).get_private_ip is not backends.VMBackend.get_private_ip)
    check("{}: get_subnet_cidr is its own, not the base no-op".format(_name),
          type(_b).get_subnet_cidr is not backends.VMBackend.get_subnet_cidr)

# get_subnet_cidr takes an optional VM: a backend configured with its subnet ignores it, one that reads the
# range off an instance needs it. Callers pass it; the signature stays compatible with a call that does not.
for _name, _b in _CLOUD.items():
    check("{}: get_subnet_cidr() is callable with no VM name".format(_name),
          type(_b).get_subnet_cidr.__defaults__ == ("",))

# libvirt and harvester keep the base answers: their address is the lab definition's own myip.
for _name in ("LibvirtBackend", "HarvesterBackend"):
    _cls = getattr(backends, _name)
    check("{}: get_private_ip stays the base no-op".format(_name),
          _cls.get_private_ip is backends.VMBackend.get_private_ip)
    check("{}: disable_source_dest_check stays the base no-op".format(_name),
          _cls.disable_source_dest_check is backends.VMBackend.disable_source_dest_check)


# ── Hetzner: private_net[].ip, and the network's own ip_range ────────────────
_hz = _CLOUD["HetznerBackend"]
_server = {"id": 7, "public_net": {"ipv4": {"ip": "203.0.113.7"}},
           "private_net": [{"network": 42, "ip": "10.0.1.5", "alias_ips": [], "mac_address": "x"}]}
with mock.patch.object(backends.HetznerBackend, "_find_server", return_value=_server):
    check("Hetzner: get_private_ip reads private_net[].ip", _hz.get_private_ip("gw") == "10.0.1.5")
    with mock.patch.object(backends.HetznerBackend, "_api",
                           return_value={"network": {"ip_range": "10.0.0.0/16"}}) as api:
        check("Hetzner: get_subnet_cidr is the attached network's ip_range",
              _hz.get_subnet_cidr("gw") == "10.0.0.0/16")
        check("Hetzner: it asks for that network by the id private_net carries",
              api.call_args[0][1] == "/networks/42")

# No private network attached is the Hetzner default, so the guest is asked instead of returning nothing.
with mock.patch.object(backends.HetznerBackend, "_find_server",
                       return_value={"id": 7, "public_net": {"ipv4": {"ip": "203.0.113.7"}}, "private_net": []}):
    check("Hetzner: no private network means no private IP", _hz.get_private_ip("gw") is None)
    with mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, "203.0.113.7/24\n")):
        check("Hetzner: get_subnet_cidr falls back to the guest's own prefix",
              _hz.get_subnet_cidr("gw") == "203.0.113.0/24")
check("Hetzner: get_subnet_cidr with no VM named cannot guess a network",
      _hz.get_subnet_cidr() is None)
check("Hetzner: ensure_ports_open stays the base no-op — inbound is accepted by default",
      backends.HetznerBackend.ensure_ports_open is backends.VMBackend.ensure_ports_open)
check("Hetzner: disable_source_dest_check stays the base no-op — no such filter is published",
      backends.HetznerBackend.disable_source_dest_check is backends.VMBackend.disable_source_dest_check)


# ── GCP: networkIP, the subnetwork's ipCidrRange, and canIpForward ──────────
_gcp = _CLOUD["GCPBackend"]
_inst = {"name": "gw", "status": "RUNNING", "canIpForward": False, "networkInterfaces": [
    {"networkIP": "10.132.0.9",
     "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/europe-west1/subnetworks/sub-a"}]}
with mock.patch.object(backends.GCPBackend, "_find_instance", return_value=_inst):
    check("GCP: get_private_ip reads networkInterfaces[0].networkIP", _gcp.get_private_ip("gw") == "10.132.0.9")
    with mock.patch.object(backends.GCPBackend, "_gcloud",
                           return_value={"ipCidrRange": "10.132.0.0/20"}) as g:
        check("GCP: get_subnet_cidr is the subnetwork's ipCidrRange",
              _gcp.get_subnet_cidr("gw") == "10.132.0.0/20")
        check("GCP: it describes the subnetwork the instance is on, in that subnetwork's region",
              "sub-a" in g.call_args[0] and "europe-west1" in g.call_args[0])

# disable_source_dest_check: gcloud has no flag for canIpForward on `instances update`, so the instance's own
# configuration is exported, the field set, and the result applied with a refresh.
_calls = []


def _fake_gcloud(self, *args, **kwargs):
    _calls.append(args)
    if args[:3] == ("compute", "instances", "export"):
        Path(args[args.index("--destination") + 1]).write_text("name: gw\ncanIpForward: false\n")
    return None


with mock.patch.object(backends.GCPBackend, "_find_instance", return_value=_inst), \
        mock.patch.object(backends.GCPBackend, "_gcloud", _fake_gcloud):
    _gcp.disable_source_dest_check("gw")
_exported = [c for c in _calls if c[:3] == ("compute", "instances", "export")]
_updated = [c for c in _calls if c[:3] == ("compute", "instances", "update-from-file")]
check("GCP: disable_source_dest_check exports the instance's own configuration", len(_exported) == 1)
check("GCP: it applies the change with update-from-file", len(_updated) == 1)
check("GCP: the update is allowed to refresh but not to restart the instance",
      "--most-disruptive-allowed-action" in _updated[0] and "REFRESH" in _updated[0])
check("GCP: it does not reach for a --can-ip-forward flag on `instances update`, which gcloud has not got",
      not any(c[:3] == ("compute", "instances", "update") for c in _calls))

# An instance that already forwards is left alone, so a re-run costs nothing.
_calls[:] = []
with mock.patch.object(backends.GCPBackend, "_find_instance", return_value=dict(_inst, canIpForward=True)), \
        mock.patch.object(backends.GCPBackend, "_gcloud", _fake_gcloud):
    _gcp.disable_source_dest_check("gw")
check("GCP: an instance that already forwards is not exported or updated again", _calls == [])

# A gateway this project creates gets it at create time, so the export path has nothing to do afterwards.
_created = []
with mock.patch.object(backends.GCPBackend, "_gcloud", lambda self, *a, **kw: _created.append(a) or None), \
        mock.patch.object(backends, "_poll_for_ip", lambda f, n: "203.0.113.1"), \
        mock.patch.object(backends.GCPBackend, "get_ip", lambda self, n: "203.0.113.1"):
    _gcp.lab_setup_path = "/tmp"
    _gcp.create_vm("gw", 1, 1024, 10, None, config_method="cloud-init", iso_image="img", can_ip_forward=True)
    _gcp.create_vm("node", 1, 1024, 10, None, config_method="cloud-init", iso_image="img")
_creates = [c for c in _created if c[:3] == ("compute", "instances", "create")]
check("GCP: create_vm(can_ip_forward=True) passes --can-ip-forward",
      len(_creates) == 2 and "--can-ip-forward" in _creates[0])
check("GCP: an ordinary node is NOT allowed to forward", "--can-ip-forward" not in _creates[1])


# ── Alibaba: VpcAttributes, the VSwitch's CidrBlock, and the shared group ───
_ali = _CLOUD["AlibabaBackend"]
with mock.patch.object(backends.AlibabaBackend, "_find_instance", return_value={
        "VpcAttributes": {"PrivateIpAddress": {"IpAddress": ["192.168.5.6"]}}}):
    check("Alibaba: get_private_ip reads VpcAttributes.PrivateIpAddress.IpAddress",
          _ali.get_private_ip("gw") == "192.168.5.6")
with mock.patch.object(backends.AlibabaBackend, "_aliyun",
                       return_value={"VSwitches": {"VSwitch": [{"CidrBlock": "192.168.5.0/24"}]}}) as al:
    check("Alibaba: get_subnet_cidr is the configured VSwitch's CidrBlock",
          _ali.get_subnet_cidr("gw") == "192.168.5.0/24")
    check("Alibaba: it asks about ALIBABA_VSWITCH_ID, which the account is configured with",
          "vsw-1" in al.call_args[0])

_rules = []
with mock.patch.object(backends.AlibabaBackend, "_aliyun",
                       side_effect=lambda *a, **kw: _rules.append(a) or None):
    _ali.ensure_ports_open(["51820/udp"])
check("Alibaba: ensure_ports_open authorizes the port with no VM named", len(_rules) == 1)
check("Alibaba: it opens it on the security group the account's instances share",
      "sg-1" in _rules[0] and "udp" in _rules[0] and "51820/51820" in _rules[0])
check("Alibaba: disable_source_dest_check stays the base no-op — ModifyInstanceAttribute has no such field",
      backends.AlibabaBackend.disable_source_dest_check is backends.VMBackend.disable_source_dest_check)


# ── UpCloud: the private address is in the server, the range is not ─────────
_up = _CLOUD["UpCloudBackend"]
_srv = {"uuid": "u1", "ip_addresses": {"ip_address": [
    {"access": "public", "address": "203.0.113.20"}, {"access": "private", "address": "10.9.0.4"}]}}
with mock.patch.object(backends.UpCloudBackend, "_find_server", return_value=_srv):
    check("UpCloud: get_private_ip takes the address whose access is private, not the first one",
          _up.get_private_ip("gw") == "10.9.0.4")
    with mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, "10.9.0.4/22\n")):
        check("UpCloud: get_subnet_cidr comes from the guest, which already holds the prefix",
              _up.get_subnet_cidr("gw") == "10.9.0.0/22")


# ── OVHcloud: the private entry in ipAddresses; nothing else exists ─────────
_ovh = _CLOUD["OVHcloudBackend"]
_oi = {"id": "i1", "ipAddresses": [{"ip": "203.0.113.30", "type": "public", "version": 4},
                                   {"ip": "10.3.0.7", "type": "private", "version": 4}]}
with mock.patch.object(backends.OVHcloudBackend, "_find_instance", return_value=_oi):
    check("OVHcloud: get_private_ip takes the private IPv4 entry, not the first one",
          _ovh.get_private_ip("gw") == "10.3.0.7")
    with mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, "203.0.113.30/24\n")):
        check("OVHcloud: get_subnet_cidr comes from the guest",
              _ovh.get_subnet_cidr("gw") == "203.0.113.0/24")
check("OVHcloud: ensure_ports_open stays the base no-op — its /cloud API has no security-group concept at all",
      backends.OVHcloudBackend.ensure_ports_open is backends.VMBackend.ensure_ports_open)
check("OVHcloud: disable_source_dest_check stays the base no-op — no port-security concept either",
      backends.OVHcloudBackend.disable_source_dest_check is backends.VMBackend.disable_source_dest_check)


# ── Scaleway and Exoscale: the provider reports neither, so the guest does ──
# Scaleway's own private_ip is deprecated and reported as null on a routed-IP instance, which is how instances
# are created now; an Exoscale instance carries no private address at all. Reading either field would have
# returned nothing for ever, and the overlay would simply have stopped routing.
for _name in ("ScalewayBackend", "ExoscaleBackend"):
    _b = _CLOUD[_name]
    with mock.patch.object(type(_b), "get_ip", lambda self, n: "203.0.113.40"), \
            mock.patch.object(backends._lc, "ssh_run", return_value=_cp(0, "172.16.4.9/20\n")):
        check("{}: get_private_ip is the guest's own address".format(_name),
              _b.get_private_ip("gw") == "172.16.4.9")
        check("{}: get_subnet_cidr is the guest's own network".format(_name),
              _b.get_subnet_cidr("gw") == "172.16.0.0/20")
    with mock.patch.object(type(_b), "get_ip", lambda self, n: None):
        check("{}: an instance with no address gives no private IP".format(_name),
              _b.get_private_ip("gw") is None)
        check("{}: and no CIDR".format(_name), _b.get_subnet_cidr("gw") is None)
    check("{}: get_subnet_cidr with no VM named returns None rather than guessing".format(_name),
          _b.get_subnet_cidr() is None)


# ── open_ports at create time, for the VMs that never reach setup_vm.py ─────
# The cloud DNS VM (port 53) and the overlay hub (the WireGuard port) are created straight through create_vm(),
# so a backend where a port is closed until a rule says otherwise has to act on open_ports there. Without this
# the overlay hub's own port is never opened on GCP, Alibaba or Exoscale and the overlay cannot form at all.
for _name, _fake, _expect in (
        ("GCPBackend", "_gcloud", "firewall-rules"),
        ("AlibabaBackend", "_aliyun", "AuthorizeSecurityGroup"),
        ("ExoscaleBackend", "_exo", "security-group")):
    _b = _CLOUD[_name]
    _b.lab_setup_path = "/tmp"
    _seen = []
    with contextlib.ExitStack() as _stack:
        _stack.enter_context(mock.patch.object(type(_b), _fake,
                                               lambda self, *a, **kw: _seen.append(a) or None))
        _stack.enter_context(mock.patch.object(backends, "_poll_for_ip", lambda f, n: "203.0.113.1"))
        _stack.enter_context(mock.patch.object(type(_b), "get_ip", lambda self, n: "203.0.113.1"))
        _b.create_vm("lab-overlay-gw-x", 1, 1024, 10, None,
                     config_method="cloud-init", iso_image="img", open_ports=["51820/udp"])
    check("{}: create_vm(open_ports=...) opens the port, for a VM that never reaches "
          "setup_vm.py's own call".format(_name),
          any(_expect in " ".join(str(x) for x in c) for c in _seen))


# ── the bug class itself: a kwarg a create_vm body reads must be one it declares ──
# A parameter that arrives only through **kwargs leaves the body's own name undefined, which is a NameError at
# the first lab that passes it and nothing at all before that.
import inspect  # noqa: E402

_src = (_REPO / "libs" / "backends.py").read_text()
for _cls in [c for c in vars(backends).values()
             if inspect.isclass(c) and issubclass(c, backends.VMBackend) and c is not backends.VMBackend]:
    _s = _src.index("class {}(".format(_cls.__name__))
    _n = _src.find("\nclass ", _s + 10)
    _b_src = _src[_s:_n if _n > 0 else len(_src)]
    _i = _b_src.index("    def create_vm(")
    _body = _b_src[_i:_b_src.index("\n    def ", _i + 10)]
    _params = set(inspect.signature(_cls.create_vm).parameters)
    for _kw in ("open_ports", "can_ip_forward", "nested_virtualization"):
        _reads = ("if {}:".format(_kw) in _body
                  or "({})".format(_kw) in _body
                  or "{} ==".format(_kw) in _body)
        check("{}.create_vm reads {} only if it declares it".format(_cls.__name__, _kw),
              not _reads or _kw in _params)

print("Failed: {}".format(len(failures)))
if failures:
    sys.exit(1)
print("all overlay-primitive checks passed")
