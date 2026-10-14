#!/usr/bin/env python3
# Pure-logic unit tests for scripts/refresh_hypervisor_status.py (masking,
# host/image selection; SSH is mocked)
# and webui/lib/discovery.py's status()/dynamic ISO_IMAGE enum injection (a
# plain temp file, no mocking needed). Run from 08_hypervisor_status.sh, in
# its own container — see tests/run_tests.sh.
import importlib.util
import json
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# lab_creation.py/primary.py live in libs/ post-cutover —
# legacy_bash/ is not a fallback path.
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import lab_creation  # noqa: E402
import primary  # noqa: E402

_loader = SourceFileLoader("refresh_hypervisor_status", str(_REPO / "scripts" / "refresh_hypervisor_status.py"))
_spec = importlib.util.spec_from_loader("refresh_hypervisor_status", _loader)
rhs = importlib.util.module_from_spec(_spec)
_loader.exec_module(rhs)

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# -- _mask ----------------------------------------------------------------
check("secret-shaped key masked", rhs._mask("ROOT_SSH_KEY", "ssh-rsa AAAA...") == "********")
check("password-shaped key masked", rhs._mask("VM_ROOT_PASS", "hunter2") == "********")
check("non-secret key left alone", rhs._mask("REMOTE_HOST", "nuc1.mydemo.lab") == "nuc1.mydemo.lab")

# -- _configured_hosts ------------------------------------------------------
check("KVM_HOSTS splits on whitespace",
      rhs._configured_hosts({"KVM_HOSTS": "nuc1 nuc2", "REMOTE_HOST": "nuc1"}) == ["nuc1", "nuc2"])
check("falls back to REMOTE_HOST when KVM_HOSTS unset",
      rhs._configured_hosts({"REMOTE_HOST": "nuc1"}) == ["nuc1"])
check("empty when neither set", rhs._configured_hosts({}) == [])

# -- list_images (mocked ssh_output) -----------------------------------------
_orig_ssh_output = lab_creation.ssh_output
lab_creation.ssh_output = lambda host, cmd: "a.qcow2\nb.ISO\nreadme.txt\nc.qcow2\n"
try:
    images = rhs.list_images("nuc1", "/var/lib/libvirt/images/sources")
    check("list_images keeps only .iso/.qcow2, case-insensitive, sorted",
          images == ["a.qcow2", "b.ISO", "c.qcow2"])
    check("list_images returns [] with no host/iso_loc", rhs.list_images("", "/x") == [] and rhs.list_images("h", "") == [])
finally:
    lab_creation.ssh_output = _orig_ssh_output

# -- host_status (mocked ssh_output, success + failure) ----------------------
# Regression test: host_status must not run virsh via a qemu+ssh://root@{host} URI when it is
# already executing
# remotely ON {host} — a redundant loopback SSH hop whose host key is never
# pre-accepted, hanging forever when run unattended. virsh must run locally
# (qemu:///system) since we're already on the target host.
_seen_cmds = []


def _fake_ok(host, cmd):
    _seen_cmds.append(cmd)
    if cmd == "nproc":
        return "8"
    if cmd.startswith("virsh") and "list --name" in cmd:
        return "vm1\nvm2"
    if "vcpucount" in cmd:
        return "2"
    if cmd.startswith("free"):
        return "4096"
    if cmd.startswith("df"):
        return "10240M"
    if cmd.startswith("ls -1"):          # the per-host source-image listing
        return "a.qcow2\nb.iso\nnotes.txt"
    raise AssertionError("unexpected command: " + cmd)


lab_creation.ssh_output = _fake_ok
try:
    st = rhs.host_status("nuc1", "/var/lib/libvirt/images/")
    check("host_status: free_cpu = total - sum(per-vm vcpucount)", st["free_cpu"] == 4)
    check("host_status: free_mem_mb parsed", st["free_mem_mb"] == 4096)
    check("host_status: free_disk_mb parsed", st["free_disk_mb"] == 10240)
    check("host_status: no error on success", st["error"] is None)
    check("host_status: virsh runs locally (qemu:///system), never a qemu+ssh:// loopback",
          any("qemu:///system" in c for c in _seen_cmds) and not any("qemu+ssh" in c for c in _seen_cmds))
finally:
    lab_creation.ssh_output = _orig_ssh_output


def _fake_fail(host, cmd):
    raise RuntimeError("SSH command failed (rc=255)")


lab_creation.ssh_output = _fake_fail
try:
    st = rhs.host_status("unreachable", "/var/lib/libvirt/images/")
    check("host_status: unreachable host reports an error, doesn't raise", st["error"] is not None)
    check("host_status: numeric fields are None on failure", st["free_cpu"] is None)
finally:
    lab_creation.ssh_output = _orig_ssh_output

# -- build_status: end-to-end masking (mocked load_config/load_defaults/ssh) --
_orig_load_config = primary.load_config
_orig_load_defaults = primary.load_defaults
primary.load_config = lambda *a, **k: {
    "REMOTE_HOST": "nuc1", "VIRT_SRV": "qemu+ssh://root@nuc1/system?keyfile=.ssh/id_rsa",
    "ROOT_SSH_KEY": "ssh-rsa AAAAsecret", "VM_ROOT_PASS": "hunter2",
}
primary.load_defaults = lambda *a, **k: {"ISO_LOC": "/var/lib/libvirt/images/sources", "VM_IMG_LOC": "/var/lib/libvirt/images/"}
lab_creation.ssh_output = _fake_ok
try:
    status = rhs.build_status()
    check("build_status: secret cfg keys never appear in config output",
          "ROOT_SSH_KEY" not in status["config"] and "VM_ROOT_PASS" not in status["config"])
    check("build_status: non-secret cfg keys pass through", status["config"].get("REMOTE_HOST") == "nuc1")
    check("build_status: one host entry for the single configured host", len(status["hosts"]) == 1)
    check("build_status: images are listed per host and as their union",
          status["images_by_host"] == {"nuc1": ["a.qcow2", "b.iso"]} and status["images"] == ["a.qcow2", "b.iso"])

    # Several KVM hosts: each is probed for its own images, and the union holds every name once.
    primary.load_config = lambda *a, **k: {"KVM_HOSTS": "nuc1 nuc2", "REMOTE_HOST": "nuc1"}
    status = rhs.build_status()
    check("build_status: every configured host is listed with its own images",
          sorted(status["images_by_host"]) == ["nuc1", "nuc2"])
    check("build_status: the union holds each name once", status["images"] == ["a.qcow2", "b.iso"])
finally:
    primary.load_config = _orig_load_config
    primary.load_defaults = _orig_load_defaults
    lab_creation.ssh_output = _orig_ssh_output

# -- discovery.py: status() file reading + dynamic ISO_IMAGE enum injection --
import discovery  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    status_path = os.path.join(td, "status.json")
    os.environ["LABBUILDER_STATUS_FILE"] = status_path

    check("discovery.status(): unavailable when no snapshot file exists yet",
          discovery.status()["available"] is False)

    with open(status_path, "w") as f:
        json.dump({"generated_at": "now", "config": {}, "hosts": [], "images": ["a.qcow2", "b.iso"]}, f)

    st = discovery.status()
    check("discovery.status(): available once the snapshot exists", st["available"] is True)
    check("discovery.status(): images list passed through", st["images"] == ["a.qcow2", "b.iso"])

    schema_tree = {"common": {"fields": [
        {"name": "ISO_IMAGE", "type": "string", "required": True},
        {"name": "VM_MEM", "type": "integer", "required": False},
        {"name": "SOURCE_IMAGE", "type": "string", "required": True},
    ]}}
    discovery._inject_dynamic_enums(schema_tree, discovery.dynamic_enums())
    iso_field = schema_tree["common"]["fields"][0]
    mem_field = schema_tree["common"]["fields"][1]
    src_field = schema_tree["common"]["fields"][2]
    check("dynamic enum: SOURCE_IMAGE field gets the live image list",
          src_field.get("enum") == ["a.qcow2", "b.iso"])
    check("dynamic enum: the deprecated ISO_IMAGE field still gets it too",
          iso_field.get("enum") == ["a.qcow2", "b.iso"])
    check("dynamic enum: unrelated fields are untouched", "enum" not in mem_field)

    # image_choices(): one entry per host and image, labelled "<host> - <file name>", ordered by host then name.
    with open(status_path, "w") as f:
        json.dump({"generated_at": "now", "config": {}, "hosts": [],
                   "images": ["a.qcow2", "b.iso"],
                   "images_by_host": {"nuc2": ["b.iso"], "nuc1": ["b.iso", "a.qcow2"]}}, f)
    choices = discovery.image_choices()
    check("image_choices: an entry per host and image, value is the file name, label is '<host> - <name>'",
          choices == [{"value": "a.qcow2", "label": "nuc1 - a.qcow2"},
                      {"value": "b.iso", "label": "nuc1 - b.iso"},
                      {"value": "b.iso", "label": "nuc2 - b.iso"}])
    check("image_choices: an older snapshot with only the flat list still yields plain names",
          discovery.image_choices({"images": ["x.qcow2"]}) == ["x.qcow2"])
    check("image_choices: no snapshot yields nothing, leaving the field free text",
          discovery.image_choices({}) == [])

    # kvm_host: the configured hosts, led by the empty value that means auto-select.
    snap = {"hosts": [{"host": "nuc1", "error": None}, {"host": "nuc2", "error": "unreachable: timed out"}]}
    hc = discovery.host_choices(snap)
    check("host_choices: auto comes first with the empty value a lab stores when kvm_host is omitted",
          hc[0]["value"] == "" and hc[0]["label"].startswith("(default) auto"))
    check("host_choices: one entry per configured host, in snapshot order",
          [h["value"] for h in hc[1:]] == ["nuc1", "nuc2"])
    check("host_choices: a host the refresh could not reach is marked but still selectable",
          hc[2]["label"] == "nuc2 (unreachable)" and hc[1]["label"] == "nuc1")
    check("host_choices: no known host yields nothing, leaving the field free text",
          discovery.host_choices({"hosts": []}) == [])

    tree = {"nodes": {"fields": [{"name": "kvm_host", "type": "string", "required": False}]}}
    discovery._inject_dynamic_enums(tree, discovery.dynamic_enums(snap))
    check("dynamic enum: the kvm_host field gets the host list",
          [h["value"] for h in tree["nodes"]["fields"][0]["enum"]] == ["", "nuc1", "nuc2"])
    tree = {"nodes": {"fields": [{"name": "kvm_host", "type": "string", "required": False}]}}
    discovery._inject_dynamic_enums(tree, discovery.dynamic_enums({"hosts": []}))
    check("dynamic enum: kvm_host is left as free text when no host is known",
          "enum" not in tree["nodes"]["fields"][0])

del os.environ["LABBUILDER_STATUS_FILE"]


# ── main(): output path, atomic write, broken configuration ────────────────────────────────────────────────────────
from unittest import mock  # noqa: E402

_out_dir = tempfile.mkdtemp()
_out = os.path.join(_out_dir, "sub", "status.json")
with mock.patch.object(sys, "argv", ["refresh_hypervisor_status.py", _out]), \
        mock.patch.object(rhs, "build_status", return_value={"hosts": [], "config": {}, "images": ["x.qcow2"]}):
    rhs.main()
check("main: writes build_status() to the path argument, creating its directory",
      json.load(open(_out))["images"] == ["x.qcow2"])
check("main: the status file is world-readable and no .tmp is left",
      os.stat(_out).st_mode & 0o777 == 0o644 and not os.path.exists(_out + ".tmp"))


def _no_config():
    raise SystemExit(1)


with mock.patch.object(sys, "argv", ["refresh_hypervisor_status.py", _out]), \
        mock.patch.object(rhs, "build_status", side_effect=_no_config):
    rhs.main()
_status = json.load(open(_out))
check("main: an unreadable lab_creation.cfg still writes a status with an error and no hosts",
      "lab_creation.cfg" in _status.get("error", "") and _status["hosts"] == [] and "generated_at" in _status)
_env_out = os.path.join(_out_dir, "env.json")
with mock.patch.object(sys, "argv", ["refresh_hypervisor_status.py"]), \
        mock.patch.dict(os.environ, {"LABBUILDER_STATUS_FILE": _env_out}), \
        mock.patch.object(rhs, "build_status", return_value={"hosts": []}):
    rhs.main()
check("main: without an argument, writes to $LABBUILDER_STATUS_FILE", os.path.isfile(_env_out))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all hypervisor-status checks passed")
