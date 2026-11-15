#!/usr/bin/env python3
# Mocked unit tests for setup_lab.py's cloud-DNS-VM precondition and setup_vm.ensure_cloud_dns_ready().
# Everything a provider would be asked is monkeypatched: this verifies the order the steps run in and which nodes
# are left untouched, not real provisioning. Run from 125_cloud_dns_precondition.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402
import lab_creation as lc  # noqa: E402
import setup_lab  # noqa: E402
import setup_vm  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


lc.log = lambda *a, **kw: None
lc.error = lambda *a, **kw: None
setup_lab.lc.log = lc.log
setup_lab.lc.error = lc.error

_LAB = {"common": {"backend": "aws", "mydomain": "mydemo.lab"},
        "nodes": {"node1.mydemo.lab": {}}}


def _run_create_one_vm(dns_ready):
    """
    _create_one_vm() for one cloud node, with every step recorded. `dns_ready` False makes the DNS VM
    precondition fail the way backends.ensure_cloud_dns_vm() does, with die().
    """
    order = []

    def _ensure(definition, config, defaults, vm_name):
        order.append("ensure_cloud_dns_ready")
        if not dns_ready:
            raise SystemExit("cloud DNS VM 'lab-dns-aws' did not start")

    with mock.patch.object(setup_lab, "ensure_cloud_dns_ready", _ensure), \
            mock.patch.object(setup_lab, "destroy_vm",
                              lambda *a, **kw: order.append("destroy_vm")), \
            mock.patch.object(setup_lab, "provision_vm",
                              lambda *a, **kw: (order.append("provision_vm"), [])[1]), \
            mock.patch.object(setup_lab, "_merged_env", lambda *a, **kw: {}), \
            mock.patch.object(setup_lab.lc, "purge_known_host", lambda *a, **kw: None), \
            mock.patch.object(setup_lab, "_report", setup_lab._RunReport()):
        setup_lab._create_one_vm(_LAB, {}, {}, "lab.json", False, "node1.mydemo.lab", {})
        report = setup_lab._report
    return order, report


order, report = _run_create_one_vm(dns_ready=True)
check("the DNS VM is checked before the node is destroyed",
      order.index("ensure_cloud_dns_ready") < order.index("destroy_vm"))
check("...and the node is still destroyed and provisioned when it is ready",
      order == ["ensure_cloud_dns_ready", "destroy_vm", "provision_vm"])

order, report = _run_create_one_vm(dns_ready=False)
check("a DNS VM that cannot be made ready leaves the existing VM alone",
      "destroy_vm" not in order and "provision_vm" not in order)
check("...and the node is reported FAILED rather than silently skipped",
      ("node1.mydemo.lab", "FAILED") in report.nodes)
check("...naming the DNS VM as the reason, so the operator knows what to fix",
      any("DNS VM" in e for e in report.errors))

# ── ensure_cloud_dns_ready(): which nodes it is a precondition for ──────────
_calls = []


def _run_precondition(definition, vm_name, backend_name):
    _calls[:] = []
    with mock.patch.object(setup_vm.backends, "effective_backend_name", lambda *a, **kw: backend_name), \
            mock.patch.object(setup_vm.backends, "get_backend", lambda *a, **kw: object()), \
            mock.patch.object(setup_vm.backends, "cloud_dns_image", lambda *a, **kw: ""), \
            mock.patch.object(setup_vm.backends, "ensure_cloud_dns_vm",
                              lambda *a, **kw: (_calls.append(kw), "10.0.0.53")[1]), \
            mock.patch.object(setup_vm, "load_vm_vars", lambda *a, **kw: {"mydomain": "mydemo.lab"}), \
            mock.patch.object(Path, "read_text", lambda self, *a, **kw: "ssh-ed25519 AAAAtest"):
        setup_vm.ensure_cloud_dns_ready(definition, {}, {}, vm_name)
    return _calls


check("a cloud node's DNS VM is a precondition",
      len(_run_precondition(_LAB, "node1.mydemo.lab", "aws")) == 1)
check("a libvirt node has no cloud DNS VM to wait for",
      _run_precondition(_LAB, "node1.mydemo.lab", "libvirt") == [])
check("a Harvester node serves its own DNS and is left out",
      _run_precondition({"common": {"backend": "aws"},
                         "nodes": {"h1": {"kcluster": "hc1"}},
                         "kclusters": {"hc1": {"clu_type": "harvester"}}}, "h1", "aws") == [])
check("the precondition asks for existence and health only, leaving the site view to register_dns()",
      _run_precondition(_LAB, "node1.mydemo.lab", "aws")[0].get("site", "") == "")

# The precondition must name the same DNS VM register_dns() will, or it could pass while provisioning goes on to
# create a second one: same function, and the same image source behind it.
check("the precondition goes through backends.ensure_cloud_dns_vm(), as register_dns() does",
      "backends.ensure_cloud_dns_vm(" in (_REPO / "scripts" / "setup_vm.py").read_text())
check("...with the account's own CLOUD_DNS_IMAGE, so it does not create one from a different image",
      "preferred_image" in _run_precondition(_LAB, "node1.mydemo.lab", "aws")[0])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud DNS precondition checks passed")
