#!/usr/bin/env python3
# Unit tests for libs/reconcile.py and the static_ip flag it acts on. No provider is reachable from here, so every
# backend is a double and the zone file is a temporary file; what is asserted is which nodes are judged to have
# moved, what is done about them, and that a node is never left half-registered because another one failed. Run
# from 141_address_reconcile.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402
import reconcile  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── static_ip_requested(): one place decides what the flag means ───────────
# load_vm_vars() has already merged `common` with the node's own keys by the time this sees them, so the per-node
# override is that merge's job and not a second rule here.
check("static_ip \"true\" asks for a reserved address", backends.static_ip_requested({"static_ip": "true"}))
check("the flag is read case-insensitively, as every other string flag of a definition is",
      backends.static_ip_requested({"static_ip": "True"}))
check("static_ip \"false\" does not", not backends.static_ip_requested({"static_ip": "false"}))
check("a definition that never mentions static_ip does not, so existing labs are unchanged",
      not backends.static_ip_requested({}))
check("neither does an empty value, nor a missing mapping",
      not backends.static_ip_requested({"static_ip": ""}) and not backends.static_ip_requested(None))
check("the flag is declared in both sections of the lab schema, so the lab-builder and an external consumer "
      "know it",
      all("static_ip" in {f["name"] for f in section["fields"]}
          for name, section in __import__("lab_schema_base").base_lab_schema()["sections"].items()
          if name in ("common", "nodes")))


# ── ensure_node_address(): the reservation, and saying so when there is none ─
class _Backend(object):
    """The parts of a backend libs/reconcile.py calls."""

    def __init__(self, address="203.0.113.1", reserved="", exists=True):
        self.address, self.reserved, self.exists = address, reserved, exists
        self.calls = []

    def get_ip(self, vm_name):
        self.calls.append(("get_ip", vm_name))
        return self.address

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return self.exists

    def ensure_stable_address(self, vm_name):
        self.calls.append(("ensure_stable_address", vm_name))
        return self.reserved


b = _Backend(reserved="198.51.100.7")
check("ensure_node_address() reserves an address when the lab asks for one, and returns it",
      reconcile.ensure_node_address(b, "aws", "vm1", {"static_ip": "true"}) == "198.51.100.7")

b = _Backend(reserved="198.51.100.7")
check("ensure_node_address() asks for nothing when the lab did not ask for a fixed address",
      reconcile.ensure_node_address(b, "aws", "vm1", {}) == "" and b.calls == [])

warned = []
b = _Backend(reserved="")
with mock.patch.object(reconcile, "warn", side_effect=lambda m: warned.append(m)):
    result = reconcile.ensure_node_address(b, "exoscale", "vm1", {"static_ip": "true"})
check("a backend that reserves nothing returns \"\", so the caller keeps the address it had", result == "")
check("...and the operator is told, naming the backend, rather than the flag silently doing nothing",
      warned and "static_ip" in warned[0] and "exoscale" in warned[0])


# ── recorded_address(): the zone is the record, not the lab definition ─────
with tempfile.TemporaryDirectory() as tmp:
    zone = Path(tmp) / "mydemo.lab.lan"
    zone.write_text("$TTL 86400\nnode101         IN  A       203.0.113.1\n"
                    "rancher         IN  CNAME   node101\n")
    with mock.patch.object(reconcile.dns_zone, "zone_path", return_value=str(zone)):
        check("recorded_address() reads the node's own A record out of the zone",
              reconcile.recorded_address("mydemo.lab", "node101.mydemo.lab") == "203.0.113.1")
        check("a node with no record of its own reads as \"\", which is as much a reason to register it as a "
              "wrong one is",
              reconcile.recorded_address("mydemo.lab", "node999.mydemo.lab") == "")
    with mock.patch.object(reconcile.dns_zone, "zone_path", return_value=str(Path(tmp) / "absent.lan")):
        check("a zone this host does not serve reads as \"\" instead of failing: not every lab's zone is here",
              reconcile.recorded_address("mydemo.lab", "node101.mydemo.lab") == "")
check("a node whose lab has no domain reads as \"\" without touching the filesystem",
      reconcile.recorded_address("", "node101") == "")


# ── drift() and reconcile(): which nodes moved, and what is done about them ─
def _context(entries):
    """A stand-in for _node_context(), which is tested on its own below."""
    return lambda definition, config, vm_names=None: iter(entries)


ENV = {"mydomain": "mydemo.lab"}
moved_backend = _Backend(address="198.51.100.20")
still_backend = _Backend(address="203.0.113.1")
with mock.patch.object(reconcile, "_node_context",
                       _context([("moved.mydemo.lab", ENV, moved_backend),
                                 ("still.mydemo.lab", ENV, still_backend)])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"):
    found = reconcile.drift({}, {})
check("drift() reports a node whose provider address is not the one in the zone",
      list(found) == ["moved.mydemo.lab"] and found["moved.mydemo.lab"] == ("203.0.113.1", "198.51.100.20"))

gone_backend = _Backend(address=None)
with mock.patch.object(reconcile, "_node_context", _context([("gone.mydemo.lab", ENV, gone_backend)])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"):
    check("a node the provider reports no address for is not drift: it is destroyed or still starting",
          reconcile.drift({}, {}) == {})

refreshed = []
with mock.patch.object(reconcile, "_node_context",
                       _context([("moved.mydemo.lab", ENV, _Backend(address="198.51.100.20")),
                                 ("still.mydemo.lab", ENV, _Backend(address="203.0.113.1"))])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"), \
        mock.patch.object(reconcile.primary, "load_defaults", return_value={}):
    report = reconcile.reconcile({}, {}, refresh_node=lambda d, c, df, vm: refreshed.append(vm))
check("reconcile() registers the node that moved", refreshed == ["moved.mydemo.lab"])
check("...and leaves a node whose address is unchanged alone, so a reconcile on a settled lab does nothing",
      list(report) == ["moved.mydemo.lab"])
check("the report says what changed, for a command to print", report["moved.mydemo.lab"] == "203.0.113.1 -> 198.51.100.20")

refreshed = []
with mock.patch.object(reconcile, "_node_context",
                       _context([("moved.mydemo.lab", ENV, _Backend(address="198.51.100.20")),
                                 ("still.mydemo.lab", ENV, _Backend(address="203.0.113.1"))])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"), \
        mock.patch.object(reconcile.primary, "load_defaults", return_value={}):
    reconcile.reconcile({}, {}, refresh_node=lambda d, c, df, vm: refreshed.append(vm), always=True)
check("always=True registers every node, which is what starting a lab wants: a guest that just booted may have "
      "taken its provider's resolver from DHCP again even where its address is unchanged",
      refreshed == ["moved.mydemo.lab", "still.mydemo.lab"])

refreshed = []
with mock.patch.object(reconcile, "_node_context",
                       _context([("gone.mydemo.lab", ENV, _Backend(address=None)),
                                 ("up.mydemo.lab", ENV, _Backend(address="203.0.113.1"))])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"), \
        mock.patch.object(reconcile.primary, "load_defaults", return_value={}):
    report = reconcile.reconcile({}, {}, refresh_node=lambda d, c, df, vm: refreshed.append(vm), always=True)
check("even with always=True, a node the provider reports no address for is left alone: registering it would "
      "write a record with no address in it", refreshed == ["up.mydemo.lab"] and report == {})

moved_backend = _Backend(address="198.51.100.20")
with mock.patch.object(reconcile, "_node_context", _context([("moved.mydemo.lab", ENV, moved_backend)])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"):
    found = reconcile.drift({}, {})
check("drift() is the read-only question reconcile_lab.py --dry-run asks: it says what moved and registers "
      "nothing", found == {"moved.mydemo.lab": ("203.0.113.1", "198.51.100.20")}
      and not any(call[0] == "ensure_stable_address" for call in moved_backend.calls))


def _fails_for(bad):
    def _refresh(definition, config, defaults, vm_name):
        if vm_name == bad:
            raise RuntimeError("unreachable")
        refreshed.append(vm_name)
    return _refresh


refreshed = []
with mock.patch.object(reconcile, "_node_context",
                       _context([("bad.mydemo.lab", ENV, _Backend(address="198.51.100.20")),
                                 ("good.mydemo.lab", ENV, _Backend(address="198.51.100.21"))])), \
        mock.patch.object(reconcile, "recorded_address", return_value="203.0.113.1"), \
        mock.patch.object(reconcile.primary, "load_defaults", return_value={}), \
        mock.patch.object(reconcile, "warn", side_effect=lambda m: None):
    report = reconcile.reconcile({}, {}, refresh_node=_fails_for("bad.mydemo.lab"))
check("one node that cannot be brought current does not stop the others", refreshed == ["good.mydemo.lab"])
check("...and it is reported as not applied, so a scheduled run can fail on it",
      "not applied" in report["bad.mydemo.lab"] and "unreachable" in report["bad.mydemo.lab"])


# ── _node_context(): cloud nodes only, and only the ones asked for ─────────
DEFINITION = {"nodes": {"a.mydemo.lab": {}, "b.mydemo.lab": {}, "local.mydemo.lab": {}}}
BACKEND_OF = {"a.mydemo.lab": "aws", "b.mydemo.lab": "gcp", "local.mydemo.lab": "libvirt"}
with mock.patch.object(reconcile.primary, "load_defaults", return_value={}), \
        mock.patch.object(reconcile.backends, "effective_backend_name",
                          side_effect=lambda d, c, vm: BACKEND_OF[vm]), \
        mock.patch.object(reconcile.backends, "get_backend", return_value=_Backend()), \
        mock.patch.object(reconcile, "load_vm_vars", return_value={}):
    seen = [vm for vm, _env, _backend in reconcile._node_context(DEFINITION, {})]
    check("_node_context() yields the cloud nodes, in definition order", seen == ["a.mydemo.lab", "b.mydemo.lab"])
    check("...and leaves a libvirt node out: it answers at the address its definition gives it, which no "
          "restart changes", "local.mydemo.lab" not in seen)
    narrowed = [vm for vm, _env, _backend in reconcile._node_context(DEFINITION, {}, ["b.mydemo.lab"])]
    check("vm_names narrows it to the nodes named", narrowed == ["b.mydemo.lab"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all address-reconcile checks passed")
