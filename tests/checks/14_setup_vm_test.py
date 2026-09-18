#!/usr/bin/env python3
# Mocked unit tests for scripts/setup_vm.py — backends.get_backend() is
# monkeypatched to return a fake backend recording every call made on it
# (matching how setup_vm.py now goes through the backend abstraction
# instead of lab_creation's flat wrapper functions directly), since no live
# KVM host is available. Verifies provision_vm()'s call ordering (DNS
# registered before the VM is created, VM created before the connectivity
# wait) and the "existing node" refusal — not real provisioning. Run from
# 14_setup_vm.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import setup_vm  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


order = []
create_vm_kwargs = {}


def _rec(name, ret=None):
    def _f(*a, **kw):
        order.append(name)
        return ret
    return _f


class _FakeBackend:
    remote_host = "hv1"

    def check_or_generate_mac(self, *a, **kw):
        order.append("check_or_generate_mac")
        return "aa:bb:cc:dd:ee:ff", "network=default,model=virtio"

    def copy_vm_image(self, *a, **kw):
        order.append("copy_vm_image")

    def push_provisioning_files(self, *a, **kw):
        order.append("push_provisioning_files")

    def create_vm(self, *a, **kw):
        order.append("create_vm")
        create_vm_kwargs.update(kw)

    def reboot_vm(self, *a, **kw):
        order.append("reboot_vm")


definition = {
    "nodes": {"vm1": {"myip": "192.168.1.50"}},
    "common": {"ISO_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2"},
}
config = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}
defaults = {"ISO_LOC": "/iso", "LAB_SETUP_PATH": "/lab", "VM_IMG_LOC": "/var/lib/libvirt/images/"}

setup_vm.is_existing_node = lambda node_cfg: False
setup_vm.validate_lab_definition = lambda *a, **kw: True
setup_vm.backends.get_backend = lambda *a, **kw: _FakeBackend()
setup_vm.load_vm_vars = lambda definition, vm_name: {
    "myip": "192.168.1.50", "mymac": "", "mydomain": "mydemo.lab",
    "config_method": "cloud-init", "VM_CPU": "2", "VM_MEM": "4096", "VM_DSK": "40",
    "ISO_IMAGE": "img.qcow2", "cloud_instance_type": "m5.2xlarge",
}
setup_vm.prepare_cloud_init = _rec("prepare_cloud_init")
setup_vm.prepare_ignition_combustion = _rec("prepare_ignition_combustion")
setup_vm.prepare_virt_customize_for_vm = _rec("prepare_virt_customize_for_vm")
setup_vm.prepare_install_iso = _rec("prepare_install_iso")
setup_vm.add_to_dns = _rec("add_to_dns")
setup_vm.clean_ssh_keys = _rec("clean_ssh_keys")
setup_vm.check_ssh_conn = _rec("check_ssh_conn")

setup_vm.provision_vm(definition, config, defaults, "vm1")

check("provision_vm: MAC resolved before the image is copied",
      order.index("check_or_generate_mac") < order.index("copy_vm_image"))
check("provision_vm: config_method='cloud-init' dispatches to prepare_cloud_init only",
      "prepare_cloud_init" in order
      and "prepare_ignition_combustion" not in order
      and "prepare_virt_customize_for_vm" not in order
      and "prepare_install_iso" not in order)
check("provision_vm: DNS is registered AFTER the VM is created (2026-09-09 fix — a cloud "
      "backend's real IP is only known once create_vm() returns; see TODO)",
      order.index("create_vm") < order.index("add_to_dns"))
check("provision_vm: the VM is created before the first connectivity wait",
      order.index("create_vm") < order.index("check_ssh_conn"))
check("provision_vm: rebooted, then waited on again, after the first connectivity check",
      order.count("check_ssh_conn") == 2 and order.index("reboot_vm") == order.index("check_ssh_conn") + 1)
check("provision_vm: stale SSH host keys cleaned before the connectivity wait",
      order.index("clean_ssh_keys") < order.index("check_ssh_conn"))
check("provision_vm: cloud_instance_type is threaded from env through to backend.create_vm() "
      "(added 2026-09-10 — see README's Compute backends table)",
      create_vm_kwargs.get("cloud_instance_type") == "m5.2xlarge")


# ── An "existing" node must never be provisioned ────────────────────────────
setup_vm.is_existing_node = lambda node_cfg: True
died = False
try:
    setup_vm.provision_vm(definition, config, defaults, "vm1")
except SystemExit:
    died = True
check("provision_vm: refuses to provision a node marked 'existing'", died)


# ── config_method dispatch: empty string -> ignition+combustion ─────────────
setup_vm.is_existing_node = lambda node_cfg: False
order.clear()
setup_vm.load_vm_vars = lambda definition, vm_name: {
    "myip": "192.168.1.50", "mymac": "", "mydomain": "mydemo.lab",
    "config_method": "", "VM_CPU": "2", "VM_MEM": "4096", "VM_DSK": "40",
    "ISO_IMAGE": "img.qcow2",
}
setup_vm.provision_vm(definition, config, defaults, "vm1")
check("provision_vm: config_method='' dispatches to prepare_ignition_combustion",
      "prepare_ignition_combustion" in order and "prepare_cloud_init" not in order)


# ── main(): --version / --help exit cleanly ─────────────────────────────────
import io
from contextlib import redirect_stdout

old_argv = sys.argv
sys.argv = ["setup_vm.py", "--version"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        setup_vm.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --version: exits 0 and prints the version", code == 0 and "setup_vm.py" in buf.getvalue())

sys.argv = ["setup_vm.py", "--help"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        setup_vm.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --help: exits 0 and prints usage", code == 0 and "Usage" in buf.getvalue())


# ── main(): destroy-before-recreate ──────────────────────────────────────────
# Confirmed live 2026-09-17/18: setup_vm.py's own standalone entrypoint never
# did this — setup_lab.py's own orchestration always calls destroy_vm() before
# provision_vm() for every node (unless --keep says it's reusable), but a
# direct `setup_vm.py <lab.json> <vm>` retry after a failed attempt died with
# "Disk ... is already in use by other guests" instead of just recreating,
# forcing a manual destroy_vm.py call first every time. Mirrors setup_lab.py's
# own try/except shape (destroy_vm() is already safe to call unconditionally),
# except a genuine destroy failure aborts outright here — there's no "next
# node" to continue on to the way setup_lab.py's multi-node loop has.
setup_vm.primary.load_defaults = lambda: defaults
setup_vm.primary.load_config = lambda: config
setup_vm.primary.load_definition = lambda json_file: definition
setup_vm.provision_vm = _rec("provision_vm")

order.clear()
setup_vm.destroy_vm = _rec("destroy_vm")
sys.argv = ["setup_vm.py", "lab.json", "vm1"]
try:
    setup_vm.main()
finally:
    sys.argv = old_argv
check("main(): destroy_vm() is called before provision_vm()",
      order == ["destroy_vm", "provision_vm"])


def _destroy_vm_system_exit(*a, **kw):
    order.append("destroy_vm")
    raise SystemExit(1)


order.clear()
setup_vm.destroy_vm = _destroy_vm_system_exit
sys.argv = ["setup_vm.py", "lab.json", "vm1"]
try:
    setup_vm.main()
finally:
    sys.argv = old_argv
check("main(): a SystemExit from destroy_vm() (nothing to destroy, or an "
      "'existing' node refusal) does not stop provision_vm() from running",
      order == ["destroy_vm", "provision_vm"])


def _destroy_vm_runtime_error(*a, **kw):
    order.append("destroy_vm")
    raise RuntimeError("simulated destroy failure")


order.clear()
setup_vm.destroy_vm = _destroy_vm_runtime_error
sys.argv = ["setup_vm.py", "lab.json", "vm1"]
died = False
try:
    setup_vm.main()
except SystemExit:
    died = True
finally:
    sys.argv = old_argv
check("main(): a genuine RuntimeError from destroy_vm() aborts (die()) "
      "instead of proceeding to provision_vm()",
      died and order == ["destroy_vm"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_vm checks passed")
