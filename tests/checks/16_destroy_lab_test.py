#!/usr/bin/env python3
# Mocked unit tests for scripts/destroy_lab.py; destroy_vm() and lab_creation.purge_known_host()
# (ssh-keygen known-hosts cleanup — not necessarily installed in the test
# container) are monkeypatched. Verifies per-node dispatch across the whole
# lab and that one node's destroy failure never blocks the rest. Run from
# 16_destroy_lab.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import destroy_lab  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


keygen_calls = []


def _fake_purge_known_host(*names):
    keygen_calls.append(names)


destroy_lab.lab_creation.purge_known_host = _fake_purge_known_host

definition = {
    "common": {"lab_name": "test-lab"},
    "nodes": {"vm1": {}, "vm2": {}, "vm3": {}},
}
config = {}
defaults = {}

# ── Every node is torn down; a failure on one doesn't block the rest ────────
processed = []


def _flaky_destroy(definition, config, defaults, vm_name):
    processed.append(vm_name)
    if vm_name == "vm2":
        raise RuntimeError("simulated destroy failure")
    if vm_name == "vm3":
        raise SystemExit(1)


destroy_lab.destroy_vm = _flaky_destroy
destroy_lab.destroy_lab(definition, config, defaults, "lab.json")

check("destroy_lab: every node is attempted despite mid-loop failures",
      processed == ["vm1", "vm2", "vm3"])
check("destroy_lab: ssh-keygen known-hosts cleanup issued once per node",
      len(keygen_calls) == 3)
check("destroy_lab: known-hosts cleanup targets each vm_name",
      all(any(vm in " ".join(call) for call in keygen_calls) for vm in ("vm1", "vm2", "vm3")))

# ── The happy path: every node destroyed cleanly, nothing raised ───────────
processed.clear()
keygen_calls.clear()
destroy_lab.destroy_vm = lambda definition, config, defaults, vm_name: processed.append(vm_name)
try:
    destroy_lab.destroy_lab(definition, config, defaults, "lab.json")
    ok = True
except Exception:
    ok = False
check("destroy_lab: happy path processes all nodes without raising",
      ok and processed == ["vm1", "vm2", "vm3"])


# ── main(): --version / missing-args ────────────────────────────────────────
import io
from contextlib import redirect_stdout

old_argv = sys.argv
sys.argv = ["destroy_lab.py", "--version"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --version: exits 0 and prints the version", code == 0 and "destroy_lab.py" in buf.getvalue())

sys.argv = ["destroy_lab.py"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main: missing lab.json argument prints usage and exits 1", code == 1 and "Usage" in buf.getvalue())


# ── --with-infra: the shared VMs, and the three gates that keep them ───────
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

CLOUD_LAB = {"common": {"lab_name": "cloud-lab", "mydomain": "mydemo.lab", "backend": "aws"},
             "nodes": {"node1.mydemo.lab": {}, "node2.mydemo.lab": {}}}


class _Backend(object):
    """The parts of a backend destroy_site_infra() calls. `present` names the VMs that exist."""

    def __init__(self, present=(), dns_ip="203.0.113.9", account="tmm"):
        self.present, self.dns_ip, self.account = set(present), dns_ip, account
        self.destroyed = []

    def vm_exists(self, vm_name):
        return vm_name in self.present

    def get_ip(self, vm_name):
        return self.dns_ip

    def delete_vm(self, vm_name):
        self.destroyed.append(vm_name)


def with_infra(zones_on_server, present=("lab-dns-aws-tmm", "lab-overlay-gw-aws-tmm"), other_labs=(),
               local_ips=(), backend_name="aws", config=None, lab_file="/root/cloud-lab.json"):
    """destroy_site_infra() over CLOUD_LAB, returning the backend double so the call can be asserted on."""
    backend = _Backend(present=present)
    with mock.patch.object(destroy_lab.backends, "effective_backend_name", return_value=backend_name), \
            mock.patch.object(destroy_lab.backends, "resolve_cloud_account",
                              return_value=("tmm", {}, "aws")), \
            mock.patch.object(destroy_lab.backends, "get_backend", return_value=backend), \
            mock.patch.object(destroy_lab.backends, "CLOUD_BACKEND_NAMES", ("aws", "gcp")), \
            mock.patch.object(destroy_lab.ipalloc, "local_ips", return_value=set(local_ips)), \
            mock.patch.object(destroy_lab, "other_labs_on_site", return_value=list(other_labs)), \
            mock.patch.object(destroy_lab.services.DNSService, "declared_zones",
                              return_value=set(zones_on_server)), \
            mock.patch.object(destroy_lab.lab_creation, "load_vm_vars",
                              create=True, return_value={"mydomain": "mydemo.lab"}), \
            mock.patch.object(destroy_lab, "load_vm_vars", return_value={"mydomain": "mydemo.lab"}):
        destroy_lab.destroy_site_infra(CLOUD_LAB, config or {}, {}, lab_file)
    return backend


backend = with_infra({"mydemo.lab"})
check("--with-infra destroys the site's DNS VM when it holds only this lab's zone",
      "lab-dns-aws-tmm" in backend.destroyed)
check("...and the site's overlay gateway with it", "lab-overlay-gw-aws-tmm" in backend.destroyed)

backend = with_infra({"mydemo.lab", "mydemoaws.lab"})
check("a DNS server still holding another lab's zone is kept, which is the only gate that can see a lab this "
      "automation node never deployed", backend.destroyed == [])

backend = with_infra({"mydemo.lab"}, other_labs=["another-lab"])
check("a site another recorded lab also uses keeps both of its shared VMs", backend.destroyed == [])

backend = with_infra({"mydemo.lab"}, local_ips={"203.0.113.9"})
check("a site whose DNS server is this automation node is never destroyed", backend.destroyed == [])

backend = with_infra({"mydemo.lab"}, backend_name="libvirt")
check("a libvirt or Harvester site has no shared VM of its own to destroy", backend.destroyed == [])

backend = with_infra({"mydemo.lab"}, config={"OVERLAY_HUB_ACCOUNT": "tmm"})
check("the overlay hub is left running even when this lab's site is its own account: it serves every account, "
      "not this site",
      "lab-dns-aws-tmm" in backend.destroyed and "lab-overlay-gw-aws-tmm" not in backend.destroyed)

backend = with_infra({"mydemo.lab"}, present=("lab-dns-aws-tmm",))
check("a site with no gateway destroys only its DNS VM", backend.destroyed == ["lab-dns-aws-tmm"])

# Destroyed through delete_vm() and nothing else: it is the only caller of release_stable_address(), and these
# VMs reserve their addresses unconditionally, which is billed whether or not anything uses them.
source = (_REPO / "scripts" / "destroy_lab.py").read_text()
check("the shared VMs are destroyed with backend.delete_vm(), the only path that releases the reserved address",
      "backend.delete_vm(dns_vm)" in source and "backend.delete_vm(gateway)" in source)
check("--with-infra is off by default, so a plain destroy leaves the shared VMs alone",
      "with_infra=False" in source and 'with_infra = "--with-infra" in sys.argv[1:]' in source)
check("the flag is documented in the command's own help", "--with-infra" in destroy_lab.__doc__)

# other_labs_on_site() reads each other lab's definition rather than trusting a recorded copy.
with tempfile.TemporaryDirectory() as tmp:
    other = Path(tmp, "other.json")
    other.write_text('{"common": {"mydomain": "other.lab"}, "nodes": {"n1.other.lab": {}}}')
    entries = [{"lab": "other", "definition": str(other)},
               {"lab": "cloud-lab", "definition": "/root/cloud-lab.json"},
               {"lab": "vanished", "definition": str(Path(tmp, "absent.json"))}]
    with mock.patch.object(destroy_lab.lab_registry, "entries", return_value=entries), \
            mock.patch.object(destroy_lab, "lab_sites", return_value={"aws-tmm": ("n1.other.lab", "tmm", set())}):
        found = destroy_lab.other_labs_on_site("/root/cloud-lab.json", "aws-tmm", {}, {})
check("another lab on the same site is found and named", "other" in found)
check("this lab itself is never counted as another lab using the site",
      not any(name.startswith("cloud-lab") for name in found))
check("a lab whose definition is gone counts as using the site, because it cannot be shown not to",
      any(name.startswith("vanished") for name in found))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all destroy_lab checks passed")
