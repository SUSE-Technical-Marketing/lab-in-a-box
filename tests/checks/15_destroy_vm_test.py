#!/usr/bin/env python3
# Mocked unit tests for scripts/destroy_vm.py; backends.get_backend() is monkeypatched to return a fake backend
# recording every call made on it (matching how destroy_vm.py now goes
# through the backend abstraction instead of lab_creation's flat wrapper
# functions directly). Verifies the "existing node" no-op path and the
# DNS-before-delete ordering on the normal path. Run from 15_destroy_vm.sh,
# in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import destroy_vm  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


order = []


def _rec(name, ret=None):
    def _f(*a, **kw):
        order.append(name)
        return ret
    return _f


class _FakeBackend:
    def delete_vm(self, *a, **kw):
        order.append("delete_vm")


definition = {"nodes": {"vm1": {"myip": "192.168.1.50"}}, "common": {}}
config = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}
defaults = {}

destroy_vm.is_existing_node = lambda node_cfg: True
destroy_vm.warn = _rec("warn")
destroy_vm.backends.get_backend = _rec("get_backend", ret=_FakeBackend())
destroy_vm.load_vm_vars = _rec("load_vm_vars", ret={"myip": "192.168.1.50", "mydomain": "mydemo.lab"})
destroy_vm.del_from_dns = _rec("del_from_dns")

destroy_vm.destroy_vm(definition, config, defaults, "vm1")
check("destroy_vm: an 'existing' node warns and does nothing else",
      order == ["warn"])

order.clear()
destroy_vm.is_existing_node = lambda node_cfg: False
destroy_vm.destroy_vm(definition, config, defaults, "vm1")
check("destroy_vm: normal path resolves the backend, removes DNS, then deletes the VM",
      order == ["get_backend", "load_vm_vars", "del_from_dns", "delete_vm"])


# ── a cloud-backend node (empty myip in the JSON, by design) resolves its real IP
#    via backend.get_ip() BEFORE delete_vm() removes the instance ──
class _FakeCloudBackend:
    def __init__(self):
        self.calls = []

    def get_ip(self, vm_name):
        self.calls.append(("get_ip", vm_name))
        return "203.0.113.42"

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return False  # no DNS VM for this backend yet — nothing to also clean up

    def delete_vm(self, vm_name):
        self.calls.append(("delete_vm", vm_name))


cloud_definition = {"nodes": {"vm1": {"backend": "aws"}}, "common": {}}
cloud_backend = _FakeCloudBackend()
order.clear()
destroy_vm.backends.get_backend = _rec("get_backend", ret=cloud_backend)
destroy_vm.load_vm_vars = _rec("load_vm_vars", ret={"myip": "", "mydomain": "mydemo.lab"})
destroy_vm.del_from_dns = lambda *a, **kw: order.append(("del_from_dns", a, kw))

destroy_vm.destroy_vm(cloud_definition, config, defaults, "vm1")

check("destroy_vm: a cloud node with an empty myip resolves its real IP via backend.get_ip() "
      "before the instance is deleted",
      ("get_ip", "vm1") in cloud_backend.calls)
del_from_dns_call = next((c for c in order if isinstance(c, tuple) and c[0] == "del_from_dns"), None)
check("destroy_vm: del_from_dns() is called with the REAL resolved IP, not the empty JSON value",
      del_from_dns_call is not None and del_from_dns_call[1][1] == "203.0.113.42")
check("destroy_vm: get_ip() is called before delete_vm() — the instance must still exist to ask",
      cloud_backend.calls.index(("get_ip", "vm1")) < cloud_backend.calls.index(("delete_vm", "vm1")))


# ── the overlay hub's node registry: a destroyed node's line comes off it ───
# setup_vm.py's overlay.register_node() wrote the line; nothing took it off, and overlay.refresh_routes() then
# opened an SSH connection to a destroyed host on every later run. The hub is looked up, never created.
lookups, forgotten = [], []


def _hub(address):
    def _f(config, **kw):
        lookups.append(kw.get("lab_setup_path"))
        return address
    return _f


def _forget(hub_host, node_host):
    forgotten.append((hub_host, node_host))


overlay_definition = {"nodes": {"vm1": {"myip": "192.168.1.50"}, "vm2": {"myip": "192.168.1.51"}},
                      "common": {"overlay": "true"}}
overlay_config = dict(config, OVERLAY_HUB_ACCOUNT="aws-hub")
overlay_defaults = {"LAB_SETUP_PATH": "/opt/lab_creation"}
destroy_vm.load_vm_vars = lambda definition, vm_name: {"myip": "192.168.1.50", "mydomain": "mydemo.lab",
                                                       "overlay": "true"}
destroy_vm.del_from_dns = lambda *a, **kw: None
destroy_vm.backends.get_backend = lambda *a, **kw: _FakeBackend()
destroy_vm.overlay.existing_hub_host = _hub("hub.example")
destroy_vm.overlay.forget_node = _forget

order.clear()
destroy_vm._HUB_HOST = None
destroy_vm.destroy_vm(overlay_definition, overlay_config, overlay_defaults, "vm1")
check("destroy_vm: an overlay lab's node is forgotten on the hub, by name",
      forgotten == [("hub.example", "vm1")])
check("destroy_vm: the hub is looked up without creating one, and told where the lab setup lives",
      lookups == ["/opt/lab_creation"])
check("destroy_vm: the VM itself is still deleted", "delete_vm" in order)

destroy_vm.destroy_vm(overlay_definition, overlay_config, overlay_defaults, "vm2")
check("destroy_vm: the hub address is looked up once per process, not once per node a teardown destroys",
      lookups == ["/opt/lab_creation"] and forgotten[-1] == ("hub.example", "vm2"))

lookups.clear()
forgotten.clear()
destroy_vm._HUB_HOST = None
destroy_vm.load_vm_vars = lambda definition, vm_name: {"myip": "192.168.1.50", "mydomain": "mydemo.lab"}
destroy_vm.destroy_vm({"nodes": {"vm1": {"myip": "192.168.1.50"}}, "common": {}},
                      overlay_config, overlay_defaults, "vm1")
check("destroy_vm: a lab with no overlay asks the hub nothing at all",
      lookups == [] and forgotten == [])

lookups.clear()
forgotten.clear()
destroy_vm._HUB_HOST = None
destroy_vm.load_vm_vars = lambda definition, vm_name: {"myip": "192.168.1.50", "mydomain": "mydemo.lab",
                                                       "overlay": "true"}
destroy_vm.overlay.existing_hub_host = _hub("")
order.clear()
destroy_vm.destroy_vm(overlay_definition, overlay_config, overlay_defaults, "vm1")
destroy_vm.destroy_vm(overlay_definition, overlay_config, overlay_defaults, "vm2")
check("destroy_vm: no hub to talk to is skipped, and the teardown goes on",
      forgotten == [] and order.count("delete_vm") == 2)
check("destroy_vm: \"there is no hub\" is remembered too, so the absent one is not looked for again",
      lookups == ["/opt/lab_creation"])


# ── main(): --version exits cleanly ─────────────────────────────────────────
import io
from contextlib import redirect_stdout

old_argv = sys.argv
sys.argv = ["destroy_vm.py", "--version"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_vm.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --version: exits 0 and prints the version", code == 0 and "destroy_vm.py" in buf.getvalue())

sys.argv = ["destroy_vm.py"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_vm.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main: missing arguments prints usage and exits 1", code == 1 and "Usage" in buf.getvalue())


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all destroy_vm checks passed")
