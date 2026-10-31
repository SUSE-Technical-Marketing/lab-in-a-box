#!/usr/bin/env python3
# Unit tests for the parts of libs/backends.py that a parallel cloud run shares: the per-backend lock that makes
# each shared resource ensured once, the cloud DNS VM's health check before reuse and the provider resolver its
# first boot uses, and cloud-account resolution with its --cloud-account override, its ambiguity error and its
# multiple-account warning. Every SSH call, provider call and credentials lookup is faked, so nothing here reaches
# a cloud, a network or /etc.
# Run from 110_cloud_shared_resources.sh, in its own container — see tests/run_tests.sh.
import sys
import threading
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402
import lab_creation as lc  # noqa: E402
import primary  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Died(Exception):
    """Raised in place of die(), so a check can assert that a path refuses rather than exiting the process."""


def _die(msg, *a, **kw):
    raise Died(msg)


backends.die = _die
lc.die = _die
_warnings = []
lc.warn = lambda msg, *a, **kw: _warnings.append(msg)
backends.log = lambda *a, **kw: None
lc.log = lambda *a, **kw: None


class FakeBackend(object):
    """The parts of a cloud backend ensure_cloud_dns_vm() and the account resolution actually call."""

    def __init__(self, account="", exists=False, ip="10.0.0.9", private="172.31.0.9"):
        self.account = account
        self._exists = exists
        self._ip = ip
        self._private = private
        self.created = []

    def vm_exists(self, name):
        return self._exists

    def get_ip(self, name):
        return self._ip

    def get_private_ip(self, name):
        return self._private

    def provider_resolver(self):
        return "169.254.169.253"

    def opensuse_image(self):
        return "leap16"

    def copy_vm_image(self, *a, **kw):
        pass

    def push_provisioning_files(self, *a, **kw):
        pass

    def create_vm(self, name, *a, **kw):
        self.created.append(name)
        return self._ip


# ── the shared-resource lock ─────────────────────────────────────────────────

b1 = FakeBackend(account="acct-a")
b2 = FakeBackend(account="acct-b")
check("shared_resource_lock: the same backend, account and resource give the same lock",
      backends.shared_resource_lock(b1, "dns") is backends.shared_resource_lock(b1, "dns"))
check("shared_resource_lock: a different resource gives a different lock",
      backends.shared_resource_lock(b1, "dns") is not backends.shared_resource_lock(b1, "gateway"))
check("shared_resource_lock: a different account gives a different lock, so accounts do not block each other",
      backends.shared_resource_lock(b1, "dns") is not backends.shared_resource_lock(b2, "dns"))

# The race itself: without the lock, N threads each find no DNS VM and each create one. create_vm() is recorded,
# so the count of creations is the assertion.
_created = []
_created_lock = threading.Lock()


def _racy_ensure(backend):
    with backends.shared_resource_lock(backend, "racetest"):
        seen = list(_created)
        threading.Event().wait(0.01)          # widen the window a real provider call would open
        if not seen:
            with _created_lock:
                _created.append("dns-vm")


_threads = [threading.Thread(target=_racy_ensure, args=(b1,)) for _ in range(4)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
check("shared_resource_lock: four concurrent ensure steps create the resource once", len(_created) == 1)


# ── the cloud DNS VM: reuse only a server that answers ───────────────────────

_asked = []


def _ssh_ok(ip, cmd, timeout=0):
    _asked.append((ip, cmd, timeout))
    return True


def _ssh_dead(ip, cmd, timeout=0):
    _asked.append((ip, cmd, timeout))
    return False


_orig_wait = lc.wait_for_ssh_command
_orig_serve = backends.services.DNSService

lc.wait_for_ssh_command = _ssh_ok
backends.services.DNSService = lambda: type("S", (), {"serve_site_view": lambda *a, **kw: None})()

existing = FakeBackend(exists=True)
_asked[:] = []
ip = backends.ensure_cloud_dns_vm(existing, "aws", "ssh-key", "mydemo.lab", "img", "/tmp", site="")
check("ensure_cloud_dns_vm: an existing DNS VM that answers is reused", ip == "10.0.0.9")
check("ensure_cloud_dns_vm: reuse asks whether named is actually serving",
      _asked and "named" in _asked[0][1])
check("ensure_cloud_dns_vm: nothing is created when an existing VM answers", existing.created == [])

lc.wait_for_ssh_command = _ssh_dead
dead = FakeBackend(exists=True)
try:
    backends.ensure_cloud_dns_vm(dead, "aws", "ssh-key", "mydemo.lab", "img", "/tmp", site="")
    refused = ""
except Died as e:
    refused = str(e)
check("ensure_cloud_dns_vm: an existing DNS VM that does not serve is refused, not reused",
      "not serving DNS" in refused)
check("ensure_cloud_dns_vm: the refusal names the VM and says how to recover",
      "lab-dns" in refused and "Destroy it" in refused)
check("ensure_cloud_dns_vm: a dead DNS VM is never silently replaced", dead.created == [])

lc.wait_for_ssh_command = _orig_wait
backends.services.DNSService = _orig_serve


# ── the provider resolver the DNS VM's first boot uses ───────────────────────

ud = backends._cloud_dns_vm_user_data("ssh-key", "mydemo.lab", provider_resolver="169.254.169.253")
check("_cloud_dns_vm_user_data: the provider resolver is written to resolv.conf", "169.254.169.253" in ud)
check("_cloud_dns_vm_user_data: it is written before the package install",
      ud.index("169.254.169.253") < ud.index("BIND_INSTALL"))
plain = backends._cloud_dns_vm_user_data("ssh-key", "mydemo.lab")
check("_cloud_dns_vm_user_data: no provider resolver leaves the guest's own resolver alone",
      "resolv.conf" not in plain)
check("VMBackend.provider_resolver: none by default", backends.VMBackend.provider_resolver(object()) == "")
check("AWSBackend.provider_resolver: the VPC's own always-reachable resolver",
      backends.AWSBackend.provider_resolver(object()) == "169.254.169.253")


# ── cloud-account resolution ─────────────────────────────────────────────────

DEFN = {"common": {"backend": "aws"}, "nodes": {"n1": {}}}


def _accounts(matches):
    primary.find_cloud_account_for_cloudtype = lambda ct, config=None: (
        matches[0] if len(matches) == 1 else None, list(matches))


backends.set_cloud_account_override("")
backends._accounts_selected.clear()
_accounts(["aws-tmm"])
check("_resolve_account_name: one matching credentials file is used automatically",
      backends._resolve_account_name(DEFN, {}, "n1") == "aws-tmm")

backends._accounts_selected.clear()
_accounts(["aws-euw2-lab", "aws-tmm", "aws-tmm-euw2"])
try:
    backends._resolve_account_name(DEFN, {}, "n1")
    ambiguous = ""
except Died as e:
    ambiguous = str(e)
check("_resolve_account_name: several matching files are an error, not a guess", ambiguous != "")
check("_resolve_account_name: the error names every matching account",
      all(n in ambiguous for n in ("aws-euw2-lab", "aws-tmm", "aws-tmm-euw2")))
check("_resolve_account_name: the error offers --cloud-account first", "--cloud-account" in ambiguous)
check("_resolve_account_name: the error also offers the lab-definition field",
      "cloud_account" in ambiguous)
check("_resolve_account_name: the error says why it cannot pick one", "separate cloud platform" in ambiguous)

backends._accounts_selected.clear()
backends.set_cloud_account_override("aws-tmm")
check("_resolve_account_name: --cloud-account resolves an otherwise ambiguous lookup",
      backends._resolve_account_name(DEFN, {}, "n1") == "aws-tmm")
pinned = {"common": {"backend": "aws", "cloud_account": "aws-euw2-lab"}, "nodes": {"n1": {}}}
check("_resolve_account_name: --cloud-account outranks the lab definition",
      backends._resolve_account_name(pinned, {}, "n1") == "aws-tmm")
backends.set_cloud_account_override("")
check("_resolve_account_name: without the override the lab definition is used",
      backends._resolve_account_name(pinned, {}, "n1") == "aws-euw2-lab")

node_pinned = {"common": {"backend": "aws", "cloud_account": "aws-euw2-lab"},
               "nodes": {"n1": {"cloud_account": "aws-tmm"}}}
check("_resolve_account_name: a node's own cloud_account outranks common",
      backends._resolve_account_name(node_pinned, {}, "n1") == "aws-tmm")

libvirt_defn = {"common": {"backend": "libvirt"}, "nodes": {"n1": {}}}
check("_resolve_account_name: a libvirt node never resolves an account",
      backends._resolve_account_name(libvirt_defn, {}, "n1") == "")


# ── the multiple-account warning ─────────────────────────────────────────────

backends._accounts_selected.clear()
_warnings[:] = []
backends._note_account_selected("aws-tmm", "aws")
check("the account warning: one account warns about nothing", _warnings == [])
backends._note_account_selected("aws-tmm", "aws")
check("the account warning: the same account again warns about nothing", _warnings == [])
backends._note_account_selected("aws-euw2-lab", "aws")
check("the account warning: a second distinct account warns once", len(_warnings) == 1)
check("the account warning: it names both accounts",
      "aws-euw2-lab" in _warnings[0] and "aws-tmm" in _warnings[0])
check("the account warning: it says each account is a separate platform",
      "separate" in _warnings[0] and "overlay" in _warnings[0])
backends._note_account_selected("aws-euw2-lab", "aws")
check("the account warning: it is not repeated per node", len(_warnings) == 1)


# ── the --cloud-account option itself ────────────────────────────────────────

backends.set_cloud_account_override("")
rest = backends.take_cloud_account_option(["--cloud-account", "aws-tmm", "lab.json"])
check("take_cloud_account_option: the separate form is applied", backends._cloud_account_override == "aws-tmm")
check("take_cloud_account_option: the separate form is stripped from the arguments", rest == ["lab.json"])

backends.set_cloud_account_override("")
rest = backends.take_cloud_account_option(["--keep", "--cloud-account=aws-tmm", "lab.json"])
check("take_cloud_account_option: the = form is applied", backends._cloud_account_override == "aws-tmm")
check("take_cloud_account_option: other options are left untouched", rest == ["--keep", "lab.json"])

backends.set_cloud_account_override("")
rest = backends.take_cloud_account_option(["lab.json", "node1"])
check("take_cloud_account_option: no option leaves no override", backends._cloud_account_override == "")
check("take_cloud_account_option: positional arguments keep their order", rest == ["lab.json", "node1"])

for bad in (["--cloud-account"], ["--cloud-account", "--keep"], ["--cloud-account="]):
    try:
        backends.take_cloud_account_option(list(bad))
        missing = ""
    except Died as e:
        missing = str(e)
    check("take_cloud_account_option: {} is refused rather than read as no account".format(" ".join(bad) or "--"),
          "needs the name of a cloud account" in missing)

print("FAILURES: {}".format(len(failures)) if failures else "all cloud shared-resource checks passed")
sys.exit(1 if failures else 0)
