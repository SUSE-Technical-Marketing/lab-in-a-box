#!/usr/bin/env python3
# Mocked-subprocess unit tests for libs/lab_creation.py —
# SSH and subprocess calls are mocked. Covers: ssh_run/ssh_output
# command shape, the multi-KVM-host resolve/locate/select logic, load_vm_vars merge
# order, and validate_lab_definition's preflight checks (with subprocess.run
# mocked so this runs in a container with no virsh/ping/jq installed). Run
# from 10_lab_creation_core.sh, in its own container — see tests/run_tests.sh.
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402
import io as _io_early  # noqa: E402
from contextlib import redirect_stderr as _redirect_stderr_early  # noqa: E402
import primary  # noqa: E402
import targets  # noqa: E402

# Captured before any test below monkeypatches lc.subprocess.run to a fake —
# process_template() (used by the prepare_cloud_init() tests near the end of
# this file) needs the REAL subprocess.run to actually shell out to bash.
_real_subprocess_run = lc.subprocess.run

# Same reasoning: targets.check_ssh_only_reachability itself gets
# permanently reassigned to a lambda further down this file (mocking it
# out for validate_lab_definition()'s own tests) — the Python-3.6-kwarg
# regression test near the end of this file needs the REAL implementation.
_real_check_ssh_only_reachability = targets.check_ssh_only_reachability

# This test container has no local virsh/virt-install (see the file
# header) — force run_libvirt_tool()'s local branch regardless, so the
# mocked lc.subprocess.run below is what actually gets called (matching
# the real automation VM host, which always has these tools locally) —
# rather than silently falling through to the SSH-fallback branch, whose
# argv shape ("ssh", ..., "virsh --connect qemu:///system ...") these
# fakes don't recognize.
lc._has_local_binary = lambda binary: True

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Drop-in for subprocess.run: records every call, returns a scripted
    result keyed by a substring match against the argv joined with spaces."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []  # list of (substring, FakeCompleted)

    def __call__(self, args, **kwargs):
        cmd = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
        self.calls.append((cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeCompleted()


# ── ssh_run: command shape + check=True error handling ──────────────────────
fake = FakeRun()
lc.subprocess.run = fake
lc.ssh_run("host1", "echo hi")
check("ssh_run: exactly one subprocess.run call", len(fake.calls) == 1)
cmd, kwargs = fake.calls[0]
check("ssh_run: uses accept-new host key checking", "StrictHostKeyChecking=accept-new" in cmd)
check("ssh_run: connects as root@<hostname>", "root@host1" in cmd)
check("ssh_run: command passed through verbatim", cmd.endswith("echo hi"))

fake = FakeRun(responses=[("false-cmd", FakeCompleted(returncode=1))])
lc.subprocess.run = fake
died = False
try:
    lc.ssh_run("host1", "false-cmd")
except RuntimeError:
    died = True
check("ssh_run: check=True raises RuntimeError on non-zero exit", died)

fake = FakeRun(responses=[("false-cmd", FakeCompleted(returncode=1))])
lc.subprocess.run = fake
result = lc.ssh_run("host1", "false-cmd", check=False)
check("ssh_run: check=False returns the result instead of raising", result.returncode == 1)

fake = FakeRun(responses=[("cat /etc/hostname", FakeCompleted(stdout="  myhost\n"))])
lc.subprocess.run = fake
check("ssh_output: strips stdout", lc.ssh_output("host1", "cat /etc/hostname") == "myhost")

# ssh_run accepts a `user` override, which defaults to root. Harvester disables root SSH, so its nodes use another user.
fake = FakeRun()
lc.subprocess.run = fake
lc.ssh_run("host1", "echo hi")
check("ssh_run: defaults to root@<hostname> when user is omitted", "root@host1" in fake.calls[0][0])

fake = FakeRun()
lc.subprocess.run = fake
lc.ssh_run("host1", "echo hi", user="rancher")
check("ssh_run: user= overrides the SSH login name", "rancher@host1" in fake.calls[0][0])

# ── purge_known_host: stale host keys for reused addresses ──────────────────
# A stale host key for a reused address makes ssh_run() refuse a new VM. The test checks that purge_known_host() removes it.
fake = FakeRun()
lc.subprocess.run = fake
lc.purge_known_host("host1", "192.168.88.150")
check("purge_known_host: calls ssh-keygen -R once per name given",
      len(fake.calls) == 2
      and "ssh-keygen -f" in fake.calls[0][0] and fake.calls[0][0].endswith("-R host1")
      and fake.calls[1][0].endswith("-R 192.168.88.150"))

fake = FakeRun()
lc.subprocess.run = fake
lc.purge_known_host("", None, "real-host")
check("purge_known_host: silently skips falsy names instead of shelling out for them",
      len(fake.calls) == 1 and fake.calls[0][0].endswith("-R real-host"))

fake = FakeRun(responses=[("ssh-keygen", FakeCompleted(returncode=1))])
lc.subprocess.run = fake
try:
    lc.purge_known_host("nonexistent-host")
    ok = True
except Exception:
    ok = False
check("purge_known_host: never raises, even when ssh-keygen finds nothing to remove (exit 1)", ok)


# ── ensure_lab_ssh_key / distribute_lab_ssh_key ──────────────────────────────
# One keypair for the whole lab, generated and kept ON the lab-host VM
# itself (never locally, never per-VM), then pushed to every nested VM so
# the lab-host VM can reach any of them.
_lab_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplekeycontent lab-in-a-box"

# Key doesn't exist yet -> generated, then read back.
fake = FakeRun(responses=[
    ("test -f", FakeCompleted(returncode=1)),
    ("ssh-keygen", FakeCompleted(returncode=0)),
    ("cat", FakeCompleted(stdout=_lab_pubkey + "\n")),
])
lc.subprocess.run = fake
pubkey = lc.ensure_lab_ssh_key("10.0.0.5")
check("ensure_lab_ssh_key: returns the generated public key, stripped",
      pubkey == _lab_pubkey)
keygen_calls = [c for c, _ in fake.calls if "ssh-keygen" in c]
check("ensure_lab_ssh_key: generates a new key when none exists",
      len(keygen_calls) == 1)
check("ensure_lab_ssh_key: generates ed25519, no passphrase, at the fixed default path",
      "-t ed25519" in keygen_calls[0] and "-N ''" in keygen_calls[0]
      and "/root/.ssh/id_lab_ed25519" in keygen_calls[0])

# Key already exists -> reused, no ssh-keygen call at all.
fake = FakeRun(responses=[
    ("test -f", FakeCompleted(returncode=0)),
    ("cat", FakeCompleted(stdout=_lab_pubkey + "\n")),
])
lc.subprocess.run = fake
pubkey = lc.ensure_lab_ssh_key("10.0.0.5")
check("ensure_lab_ssh_key: reuses an existing key rather than regenerating",
      pubkey == _lab_pubkey and not any("ssh-keygen" in c for c, _ in fake.calls))

# distribute_lab_ssh_key: one two-hop SSH per target, run FROM the lab-host
# VM (the orchestrator itself has no direct route into the nested VMs'
# internal NAT range) — never embeds the pubkey directly in a shell string,
# routes it through stdin at every hop instead.
fake = FakeRun()
lc.subprocess.run = fake
lc.distribute_lab_ssh_key("10.0.0.5", _lab_pubkey, ["192.168.150.10", "192.168.150.11"])
check("distribute_lab_ssh_key: exactly one SSH call per target VM",
      len(fake.calls) == 2)
for cmd, kwargs in fake.calls:
    check("distribute_lab_ssh_key: call goes to the lab-host VM, not the target directly",
          "root@10.0.0.5" in cmd)
    check("distribute_lab_ssh_key: the inner hop targets the nested VM's own root@",
          "root@192.168.150.1" in cmd)
    check("distribute_lab_ssh_key: never embeds the pubkey literally in the command string",
          _lab_pubkey not in cmd)
    check("distribute_lab_ssh_key: passes the pubkey via stdin instead",
          kwargs.get("input") == _lab_pubkey)
    check("distribute_lab_ssh_key: idempotent (checks for the existing line before appending)",
          "grep -qxF" in cmd)
    check("distribute_lab_ssh_key: POSIX-sh compatible, no bash-only here-strings",
          "<<<" not in cmd)


# ── total_lab_resources: pure function, no I/O ───────────────────────────────
res_def = {
    "common": {"VM_CPU": "2", "VM_MEM": "4096", "VM_DSK": "40"},
    "nodes": {
        "vm1": {},  # falls back to common's values entirely
        "vm2": {"VM_CPU": "4", "VM_MEM": "8192", "VM_DSK": "80"},  # per-node override wins
    },
}
check("total_lab_resources: per-node values override common, common fills in the rest, and it sums both nodes",
      lc.total_lab_resources(res_def) == (2 + 4, 4096 + 8192, 40 + 80))

check("total_lab_resources: an empty lab (no nodes) totals to zero",
      lc.total_lab_resources({"common": {}, "nodes": {}}) == (0, 0, 0))

check("total_lab_resources: a node with neither its own value nor a common default contributes 0",
      lc.total_lab_resources({"common": {}, "nodes": {"vm1": {"VM_CPU": "2"}}}) == (2, 0, 0))


# ── resolve_kvm_host / locate_kvm_host / select_kvm_host ─────────────────────
# Single-host config (today's default): no probing, no selection logic runs.
definition = {"nodes": {"vm1": {}}, "common": {}}
config = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}
probed = []
lc._host_resources = lambda host, vm_img_loc: probed.append(host) or (99, 99999, 99999)
host, virt_srv = lc.resolve_kvm_host(definition, "vm1", config)
check("resolve_kvm_host: single configured host used directly, no probing", host == "hv1" and not probed)
check("resolve_kvm_host: reuses the configured VIRT_SRV verbatim for the default host",
      virt_srv == config["VIRT_SRV"])

# Multi-host: explicit per-node override wins over selection.
definition2 = {"nodes": {"vm1": {"kvm_host": "hv3"}}, "common": {}}
config2 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2 hv3"}
host, virt_srv = lc.resolve_kvm_host(definition2, "vm1", config2)
check("resolve_kvm_host: explicit nodes.kvm_host overrides selection", host == "hv3")
check("resolve_kvm_host: derives a fresh qemu+ssh URI for a non-default host",
      virt_srv == "qemu+ssh://root@hv3/system?keyfile=.ssh/id_rsa")

# Multi-host: resource-based selection picks the host with the most free
# memory among those that qualify.
definition3 = {"nodes": {"vm1": {"VM_CPU": 4, "VM_MEM": 4096, "VM_DSK": 20}}, "common": {}}
config3 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2"}
resources = {"hv1": (2, 2048, 2048), "hv2": (8, 16384, 40960)}  # hv1 too small, hv2 qualifies
lc._host_resources = lambda host, vm_img_loc: resources[host]
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: selects the only qualifying host under resource pressure", host == "hv2")

resources2 = {"hv1": (8, 8192, 40960), "hv2": (8, 16384, 40960)}  # both qualify, hv2 has more RAM
lc._host_resources = lambda host, vm_img_loc: resources2[host]
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: picks the host with the most free memory among qualifiers", host == "hv2")

resources3 = {"hv1": (1, 512, 512), "hv2": (1, 512, 512)}  # neither qualifies
lc._host_resources = lambda host, vm_img_loc: resources3[host]
died = False
try:
    lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
except SystemExit:
    died = True
check("resolve_kvm_host: dies when no configured host has enough resources", died)

# A host that fails to answer is disqualified, not fatal, if another qualifies.
def _flaky_resources(host, vm_img_loc):
    if host == "hv1":
        raise RuntimeError("ssh timeout")
    return (8, 16384, 40960)


lc._host_resources = _flaky_resources
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: an unreachable candidate host is skipped, not fatal", host == "hv2")

# locate_kvm_host: probes each configured host's virsh dominfo for an
# existing VM and returns the first one that has it.
fake = FakeRun(responses=[
    ("--connect qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa dominfo vm1", FakeCompleted(returncode=1)),
    ("--connect qemu+ssh://root@hv2/system?keyfile=.ssh/id_rsa dominfo vm1", FakeCompleted(returncode=0)),
])
lc.subprocess.run = fake
config4 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2"}
host, virt_srv = lc.locate_kvm_host(definition2, "vm1", config4) if False else (None, None)
# (definition2 has an explicit kvm_host override which would short-circuit
# probing; use a definition with no override to actually exercise probing)
definition4 = {"nodes": {"vm1": {}}, "common": {}}
host, virt_srv = lc.locate_kvm_host(definition4, "vm1", config4)
check("locate_kvm_host: finds the VM on the second configured host", host == "hv2")

fake = FakeRun(responses=[])  # every dominfo call returns the default FakeCompleted(returncode=0)
lc.subprocess.run = fake
host, virt_srv = lc.locate_kvm_host(definition4, "vm1", config4)
check("locate_kvm_host: returns the first host that has the domain", host == "hv1")

fake = FakeRun(responses=[
    ("dominfo vm1", FakeCompleted(returncode=1)),
])
lc.subprocess.run = fake
died = False
try:
    lc.locate_kvm_host(definition4, "vm1", config4)
except SystemExit:
    died = True
check("locate_kvm_host: dies when no configured host has the domain", died)


# ── load_vm_vars: merge order + auto-detect fallbacks ────────────────────────
lc._detect_dns = lambda: "8.8.8.8"
lc._detect_gateway = lambda: "192.168.1.1"
lc._detect_domain = lambda: "mydemo.lab"
lc._detect_netmask = lambda: "24"
definition5 = {
    "common": {"VM_MEM": "4096", "mydns": ""},
    "nodes": {"vm1": {"myip": "192.168.1.50", "kcluster": "cluster1", "VM_MEM": "8192"}},
}
env = lc.load_vm_vars(definition5, "vm1")
check("load_vm_vars: per-node value overrides common", env["VM_MEM"] == "8192")
check("load_vm_vars: kcluster key is renamed to clu_name", env.get("clu_name") == "cluster1" and "kcluster" not in env)
check("load_vm_vars: auto-detects mydns when unset/empty", env["mydns"] == "8.8.8.8")
check("load_vm_vars: auto-detects mygw when absent", env["mygw"] == "192.168.1.1")
check("load_vm_vars: derives mynet_reverse from myip when present", "mynet_reverse" in env and env["mynet_reverse"])

definition6 = {"common": {}, "nodes": {"vm1": {}}}
env = lc.load_vm_vars(definition6, "vm1")
check("load_vm_vars: no mynet_reverse when myip is absent", "mynet_reverse" not in env)

# mydomain left out: the domain the node names share, else this host's own; names in different domains are fatal.
_nd = primary.node_name_domain
check("node_name_domain: FQDN names sharing a domain give that domain",
      _nd({"nodes": {"a.sko.lab": {}, "b.sko.lab": {}}}) == "sko.lab")
check("node_name_domain: only the first label is the host name",
      _nd({"nodes": {"a.east.sko.lab": {}}}) == "east.sko.lab")
check("node_name_domain: short names give no domain", _nd({"nodes": {"a": {}, "b": {}}}) == "")
check("node_name_domain: a node with its own mydomain is left out",
      _nd({"nodes": {"a.sko.lab": {}, "b.other.lab": {"mydomain": "other.lab"}}}) == "sko.lab")
for _label, _nodes in (("different domains", {"a.sko.lab": {}, "b.other.lab": {}}),
                       ("an FQDN mixed with a short name", {"a.sko.lab": {}, "b": {}})):
    try:
        _nd({"nodes": _nodes})
        _msg = ""
    except ValueError as e:
        _msg = str(e)
    check("node_name_domain: {} raise, asking for mydomain".format(_label),
          "cannot determine the default domain name" in _msg and "mydomain" in _msg)

env = lc.load_vm_vars({"common": {}, "nodes": {"rodeo1.mydemosko.lab": {}}}, "rodeo1.mydemosko.lab")
check("load_vm_vars: no mydomain takes the node names' shared domain, not this host's",
      env["mydomain"] == "mydemosko.lab")
env = lc.load_vm_vars({"common": {"mydomain": "set.lab"}, "nodes": {"r.mydemosko.lab": {}}}, "r.mydemosko.lab")
check("load_vm_vars: an explicit mydomain wins over the node names", env["mydomain"] == "set.lab")
env = lc.load_vm_vars({"common": {}, "nodes": {"vm1": {}}}, "vm1")
check("load_vm_vars: short names still fall back to this host's domain", env["mydomain"] == "mydemo.lab")
try:
    with _redirect_stderr_early(_io_early.StringIO()):
        lc.load_vm_vars({"common": {}, "nodes": {"a.sko.lab": {}, "b.other.lab": {}}}, "a.sko.lab")
    _died = False
except SystemExit:
    _died = True
check("load_vm_vars: node names in different domains with no mydomain are fatal", _died)


# ── validate_lab_definition: preflight checks ────────────────────────────────
# subprocess.run is mocked throughout (no virsh/ping/jq in the test
# container); resolve_kvm_host is pinned to a single configured host so no
# multi-host probing runs during these checks.
lc._host_resources = lambda host, vm_img_loc: (99, 99999, 99999)
targets.check_ssh_only_reachability = lambda node, timeout=5: True


def _lab_def(data):
    """
    Write `data` to a temporary .json file and wrap the file in a primary.LabDefinition. The fixture has the same shape that
    validate_lab_definition() receives in production, from primary.load_definition(). The validator reads definition.source_path for
    its banner and for the per-addon --validate subprocesses, so the fixture carries that path.
    """
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return primary.LabDefinition(data, f.name, "json")


base_common = {"SOURCE_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2"}
single_host_cfg = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}

# All node fixtures below use config_method="virt_customize" to sidestep
# section 8's local ignition/combustion/cloud-init template-file checks,
# which are irrelevant to what this test is exercising (section 4-7's
# structural/hypervisor checks). "echo ok" is scripted to succeed (section
# 7's per-host SSH-reachability probe); every other subprocess.run call
# (virsh dominfo, ping, test -f) falls through to FakeRun's default
# returncode=0, which validate_lab_definition treats as WARN-only
# ("already exists" / "already responding" / image found) — never an ERROR
# — so it doesn't affect the pass/fail assertions below.
img_check_ok = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

# validate_lab_definition takes the already-loaded definition directly (read/
# parsed exactly once by the caller, via primary.load_definition) — it never
# re-reads/re-parses the file itself, and takes no separate path argument at
# all: a LabDefinition already knows its own source_path (used for the
# preflight banner and for delegating to each addon's own
# `install_<addon> --validate <path>` subprocess, section 5 below) — see the
# function's own docstring. Parsing (JSON/YAML format auto-detection,
# malformed-input handling) is primary.py's job and is covered in
# 11_primary_test.py instead, not re-tested here.

# Valid minimal definition.
lc.subprocess.run = img_check_ok
good = _lab_def({"common": dict(base_common),
                  "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                     "config_method": "virt_customize"}}})
check("validate_lab_definition: valid minimal definition passes",
      lc.validate_lab_definition(good, single_host_cfg, "/iso", "/lab") is True)

# _template_state: an unreadable directory on the way must not abort a validation run (the lab-builder's CGI cannot
# read the template tree, which holds the expanded root password hash).
_tpl_dir = tempfile.mkdtemp()
_tpl = os.path.join(_tpl_dir, "sub", "template")
os.makedirs(os.path.dirname(_tpl))
open(_tpl, "w").close()
check("_template_state: an existing readable file is True", lc._template_state(_tpl) is True)
check("_template_state: a missing file is False", lc._template_state(os.path.join(_tpl_dir, "nope")) is False)
if os.geteuid() != 0:
    os.chmod(os.path.dirname(_tpl), 0o000)
    check("_template_state: an unreadable parent is None, not an exception", lc._template_state(_tpl) is None)
    os.chmod(os.path.dirname(_tpl), 0o700)
else:
    check("_template_state: an unreadable parent is None, not an exception",
          lc._template_state.__doc__ is not None)   # root bypasses the permission bits; covered live instead


# host_checks=False (the lab-builder's CGI, which has no SSH access): the definition's own contents are still checked,
# but nothing may reach a hypervisor or the network.
_virsh_calls = []
_orig_virsh = lc.run_libvirt_tool
lc.run_libvirt_tool = lambda *a, **kw: _virsh_calls.append(a) or FakeCompleted(returncode=0)
check("validate_lab_definition: host_checks=False still validates the definition",
      lc.validate_lab_definition(good, single_host_cfg, "/iso", "/lab", host_checks=False) is True)
check("validate_lab_definition: host_checks=False runs no libvirt call", _virsh_calls == [])
_mixed = _lab_def({"common": dict(base_common),
                   "nodes": {"a.sko.lab": {"myip": "192.168.1.52", "mymac": "aa:bb:cc:dd:ee:0b",
                                           "config_method": "virt_customize"},
                             "b.other.lab": {"myip": "192.168.1.53", "mymac": "aa:bb:cc:dd:ee:0c",
                                             "config_method": "virt_customize"}}})
_issues = []
check("validate_lab_definition: node names in different domains with no mydomain fail",
      lc.validate_lab_definition(_mixed, single_host_cfg, "/iso", "/lab", host_checks=False,
                                 issues_out=_issues) is False)
check("validate_lab_definition: and the error asks for mydomain",
      any("cannot determine the default domain name" in line for line in _issues))
_keys_bad = _lab_def({"common": dict(base_common, ssh_authorized_keys=["http://example.com/keys"]),
                      "nodes": {"vm1": {"myip": "192.168.1.54", "mymac": "aa:bb:cc:dd:ee:0d",
                                        "config_method": "virt_customize"}}})
_issues = []
check("validate_lab_definition: an ssh_authorized_keys entry over http fails",
      lc.validate_lab_definition(_keys_bad, single_host_cfg, "/iso", "/lab", host_checks=False,
                                 issues_out=_issues) is False
      and any("common.ssh_authorized_keys[0]" in line and "https" in line for line in _issues))
_keys_ok = _lab_def({"common": dict(base_common, ssh_authorized_keys=[
                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAaaaa a@b", {"key": "/not/here.pub", "user": "bob"}]),
                     "nodes": {"vm1": {"myip": "192.168.1.55", "mymac": "aa:bb:cc:dd:ee:0e",
                                       "config_method": "virt_customize"}}})
check("validate_lab_definition: a key line and a file path pass without host checks",
      lc.validate_lab_definition(_keys_ok, single_host_cfg, "/iso", "/lab", host_checks=False) is True)
# An add-on's --validate runs through the REAL subprocess.run: this container's python3 is 3.6, as the lab-builder
# CGI's can be, so a keyword argument newer than 3.6 (capture_output=, text=) fails here as it does there.
import apps  # noqa: E402
_fake_addon = os.path.join(tempfile.mkdtemp(), "install_fakeaddon")
with open(_fake_addon, "w") as _fh:
    _fh.write("#!/bin/sh\necho '[ERROR] fakeaddon.fake_field is required'\nexit 1\n")
os.chmod(_fake_addon, 0o755)
_orig_installer, _orig_run = apps.addon_installer, lc.subprocess.run
apps.addon_installer = lambda name: _fake_addon if name == "fakeaddon" else None
lc.subprocess.run = _real_subprocess_run
_with_addon = _lab_def({"common": dict(base_common),
                        "nodes": {"vm1": {"myip": "192.168.1.56", "mymac": "aa:bb:cc:dd:ee:0f",
                                          "config_method": "virt_customize", "addons": ["fakeaddon"]}}})
_issues = []
try:
    _ok = lc.validate_lab_definition(_with_addon, single_host_cfg, "/iso", "/lab", host_checks=False,
                                     issues_out=_issues)
    _crash = ""
except TypeError as e:
    _ok, _crash = None, str(e)
check("validate_lab_definition: an add-on's --validate runs under this python3 ({})".format(_crash or "ok"),
      _crash == "")
check("validate_lab_definition: the add-on's own error is reported",
      _ok is False and any("fakeaddon.fake_field is required" in line for line in _issues))
apps.addon_installer, lc.subprocess.run = _orig_installer, _orig_run

# read_credentials=False (the lab-builder's public Validate): an account is only looked for, never read, and its type
# comes from its <type>-<account> name.
_cred_dir = tempfile.mkdtemp()
with open(os.path.join(_cred_dir, "aws-team.yaml"), "w") as _fh:
    _fh.write("{this is not: [valid yaml\n")          # would be a parse error if it were read
open(os.path.join(_cred_dir, "corp.yaml"), "w").close()
_cred_cfg = dict(single_host_cfg, CREDENTIALS_PATH=_cred_dir)


def _cloud_lab(account):
    return _lab_def({"common": dict(base_common, cloud_account=account), "nodes": {"c1.mydemo.lab": {}}})


_issues = []
lc.validate_lab_definition(_cloud_lab("aws-team"), _cred_cfg, "/iso", "/lab", host_checks=False,
                           read_credentials=False, issues_out=_issues)
check("validate_lab_definition: without read_credentials an account file is not read",
      not any("aws-team" in line for line in _issues))
_issues = []
check("validate_lab_definition: ...but read with it (the default), where its parse error shows",
      lc.validate_lab_definition(_cloud_lab("aws-team"), _cred_cfg, "/iso", "/lab", host_checks=False,
                                 issues_out=_issues) is False
      and any("aws-team" in line for line in _issues))
_issues = []
check("validate_lab_definition: without read_credentials a missing account is still an error",
      lc.validate_lab_definition(_cloud_lab("aws-nope"), _cred_cfg, "/iso", "/lab", host_checks=False,
                                 read_credentials=False, issues_out=_issues) is False
      and any("not found" in line for line in _issues))
_issues = []
_ok = lc.validate_lab_definition(_cloud_lab("corp"), _cred_cfg, "/iso", "/lab", host_checks=False,
                                 read_credentials=False, issues_out=_issues)
check("validate_lab_definition: an account name without its type is a warning asking to log in",
      any("WARN" in line and "log in" in line for line in _issues))
_conflict = _lab_def({"common": dict(base_common, cloud_account="aws-team", backend="gcp"), "nodes": {"c1.mydemo.lab": {}}})
_issues = []
check("validate_lab_definition: a type from the name that contradicts backend is an error",
      lc.validate_lab_definition(_conflict, _cred_cfg, "/iso", "/lab", host_checks=False, read_credentials=False,
                                 issues_out=_issues) is False
      and any("backend is set to 'gcp'" in line for line in _issues))

# mygw/mymask/mydns on an all-cloud lab: the provider assigns the addresses and the cloud DNS VM serves the zone,
# so the fields are not read and the preflight says so — unless a node still uses them, or the caller runs a
# definition the operator did not write (setup_lab.py --example).
_net_common = dict(base_common, mygw="192.168.88.1", mymask="24", mydns="192.168.88.73", mydomain="mydemo.lab")
_all_cloud = _lab_def({"common": dict(_net_common, backend="aws"), "nodes": {"c1.mydemo.lab": {}}})
_issues = []
lc.validate_lab_definition(_all_cloud, single_host_cfg, "/iso", "/lab", host_checks=False,
                           read_credentials=False, issues_out=_issues)
check("validate_lab_definition: an all-cloud lab is told its network fields are not read",
      any("WARN" in line and "not read by this lab" in line and "'mygw'" in line and "'mydns'" in line
          for line in _issues))
check("validate_lab_definition: and that mydomain still is", any("'mydomain' is used" in line for line in _issues))
_issues = []
lc.validate_lab_definition(_all_cloud, single_host_cfg, "/iso", "/lab", host_checks=False,
                           read_credentials=False, issues_out=_issues, warn_unused_fields=False)
check("validate_lab_definition: warn_unused_fields=False leaves that advice out",
      not any("not read by this lab" in line for line in _issues))
_mixed_backends = _lab_def({"common": dict(_net_common, backend="aws"),
                            "nodes": {"c1.mydemo.lab": {}, "v1.mydemo.lab": {"backend": "libvirt",
                                                                             "myip": "192.168.88.51"}}})
_issues = []
lc.validate_lab_definition(_mixed_backends, single_host_cfg, "/iso", "/lab", host_checks=False,
                           read_credentials=False, issues_out=_issues)
check("validate_lab_definition: a node that does use them keeps the advice away",
      not any("not read by this lab" in line for line in _issues))
_issues = []
lc.validate_lab_definition(_lab_def({"common": dict(base_common, backend="aws", mydomain="mydemo.lab"),
                                     "nodes": {"c1.mydemo.lab": {}}}),
                           single_host_cfg, "/iso", "/lab", host_checks=False,
                           read_credentials=False, issues_out=_issues)
check("validate_lab_definition: a lab that sets none of them is told nothing",
      not any("not read by this lab" in line for line in _issues))

# A credential file that cannot be read is an error returned, not an exception (a directory stands in for an unreadable
# file: this container runs as root, which reads any file).
os.mkdir(os.path.join(_cred_dir, "aws-dir.yaml"))
_data, _err = primary.try_load_cloud_account("aws-dir", config=_cred_cfg)
check("try_load_cloud_account: an unreadable file is returned as an error",
      _data is None and "cannot be read" in (_err or ""))

_mixed_set = _lab_def({"common": dict(base_common, mydomain="mydemo.lab"), "nodes": dict(_mixed["nodes"])})
check("validate_lab_definition: the same names pass once mydomain is set",
      lc.validate_lab_definition(_mixed_set, single_host_cfg, "/iso", "/lab", host_checks=False) is True)
bad_no_ip = _lab_def({"common": dict(base_common),
                      "nodes": {"vm1": {"mymac": "aa:bb:cc:dd:ee:09", "config_method": "virt_customize"}}})
check("validate_lab_definition: host_checks=False reports a missing myip",
      lc.validate_lab_definition(bad_no_ip, single_host_cfg, "/iso", "/lab", host_checks=False) is False)
check("validate_lab_definition: host_checks=False skips the source-image check (no ISO_LOC error)",
      lc.validate_lab_definition(_lab_def({"common": dict(base_common),
                                           "nodes": {"vm1": dict({"myip": "192.168.1.51",
                                                                  "mymac": "aa:bb:cc:dd:ee:0a",
                                                                  "config_method": "virt_customize"})}}),
                                 single_host_cfg, "", "/lab", host_checks=False) is True)
lc.run_libvirt_tool = _orig_virsh
lc.subprocess.run = img_check_ok

# Missing 'nodes'/'common'.
empty = _lab_def({})
check("validate_lab_definition: missing nodes/common fails",
      lc.validate_lab_definition(empty, single_host_cfg, "/iso", "/lab") is False)

# Duplicate IP across two nodes.
lc.subprocess.run = img_check_ok
dup_ip = _lab_def({
    "common": dict(base_common),
    "nodes": {
        "vm1": {"myip": "192.168.1.50", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.50", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: duplicate IP across nodes fails",
      lc.validate_lab_definition(dup_ip, single_host_cfg, "/iso", "/lab") is False)

# Node references an undefined kcluster.
bad_kcluster = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "kcluster": "nope", "config_method": "virt_customize"}},
    "kclusters": {},
})
check("validate_lab_definition: undefined kcluster reference fails",
      lc.validate_lab_definition(bad_kcluster, single_host_cfg, "/iso", "/lab") is False)

# Invalid backend name.
bad_backend = _lab_def({"common": dict(base_common, backend="not-a-backend"),
                         "nodes": {"vm1": {"myip": "192.168.1.50", "config_method": "virt_customize"}}})
check("validate_lab_definition: invalid common.backend fails",
      lc.validate_lab_definition(bad_backend, single_host_cfg, "/iso", "/lab") is False)

# Existing node unreachable via SSH.
targets.check_ssh_only_reachability = lambda node, timeout=5: False
existing_unreachable = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "existing": True, "config_method": "virt_customize"}},
})
check("validate_lab_definition: unreachable 'existing' node fails",
      lc.validate_lab_definition(existing_unreachable, single_host_cfg, "/iso", "/lab") is False)

# Existing node reachable: skips hypervisor/image checks entirely, passes.
targets.check_ssh_only_reachability = lambda node, timeout=5: True
existing_reachable = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "existing": True, "config_method": "virt_customize"}},
})
check("validate_lab_definition: reachable 'existing' node passes",
      lc.validate_lab_definition(existing_reachable, single_host_cfg, "/iso", "/lab") is True)

# ── backend-aware SOURCE_IMAGE check ───────────────────────────────────────────
# The SOURCE_IMAGE existence check applies only to libvirt. Other backends resolve images in their own environment.
img_check_missing = FakeRun(responses=[
    ("echo ok", FakeCompleted(returncode=0, stdout="ok")),
    ("test -f", FakeCompleted(returncode=1)),
])

lc.subprocess.run = img_check_missing
libvirt_missing_image = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.60", "config_method": "virt_customize"}},
})
check("validate_lab_definition: libvirt node with a missing SOURCE_IMAGE still fails (baseline)",
      lc.validate_lab_definition(libvirt_missing_image, single_host_cfg, "/iso", "/lab") is False)

lc.subprocess.run = img_check_missing
harvester_missing_image = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.61", "backend": "harvester", "config_method": "virt_customize"}},
})
check("validate_lab_definition: harvester-backend node skips the libvirt-only SOURCE_IMAGE check",
      lc.validate_lab_definition(harvester_missing_image, single_host_cfg, "/iso", "/lab") is True)

lc.subprocess.run = img_check_missing
harvester_via_common = _lab_def({
    "common": dict(base_common, backend="harvester"),
    "nodes": {"vm1": {"myip": "192.168.1.62", "config_method": "virt_customize"}},
})
check("validate_lab_definition: common.backend=harvester (no per-node override) also skips the check",
      lc.validate_lab_definition(harvester_via_common, single_host_cfg, "/iso", "/lab") is True)


# ── a cloud-backend node's myip may be empty ────────────────────────────────
# A cloud node's IP is known only after the provider assigns it, so myip is left empty for it. libvirt and Harvester nodes still require myip.
lc.subprocess.run = img_check_ok
cloud_no_myip = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"backend": "aws", "config_method": "virt_customize"}},
})
check("validate_lab_definition: an aws-backend node with no myip at all still passes preflight",
      lc.validate_lab_definition(cloud_no_myip, single_host_cfg, "/iso", "/lab") is True)

# A cloud node has no KVM host, so the preflight must not ask a hypervisor whether a VM of that name exists there.
# Live on AWS, that question hit a same-named libvirt VM an earlier lab had left on the hypervisor and the preflight
# announced it would be "destroyed and recreated", which was never true of the node being validated.
_asked = []


def _record_virsh(cmd, *a, **k):
    _asked.append(cmd)
    return img_check_ok(cmd, *a, **k)


lc.subprocess.run = _record_virsh
cloud_host_check = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"backend": "aws", "config_method": "cloud-init"}},
})
lc.validate_lab_definition(cloud_host_check, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: never asks a hypervisor about a cloud node's VM name",
      not any("dominfo" in " ".join(str(x) for x in c) for c in _asked))
check("validate_lab_definition: never pings a cloud node's address either",
      not any("ping" in " ".join(str(x) for x in c) for c in _asked))

lc.subprocess.run = img_check_ok
cloud_no_myip_via_common = _lab_def({
    "common": dict(base_common, backend="hetzner"),
    "nodes": {"vm1": {"config_method": "virt_customize"}},
})
check("validate_lab_definition: common.backend=hetzner (no per-node override) also skips the "
      "myip requirement", lc.validate_lab_definition(cloud_no_myip_via_common, single_host_cfg, "/iso", "/lab") is True)

lc.subprocess.run = img_check_ok
libvirt_no_myip = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"config_method": "virt_customize"}},
})
check("validate_lab_definition: a plain libvirt node with no myip still fails (baseline, unaffected)",
      lc.validate_lab_definition(libvirt_no_myip, single_host_cfg, "/iso", "/lab") is False)


# ── cloud_account: multiple cloud accounts (like KVM_HOSTS for hypervisors) ──
# validate_lab_definition() does `from primary import try_load_cloud_account`
# INSIDE the function, so patching the name on the primary module is what takes
# effect (same direct-assignment style as lc.subprocess.run above).
import io as _acct_io
from contextlib import redirect_stdout as _acct_redirect
_orig_try_acct = primary.try_load_cloud_account

# a valid account -> treated as a cloud node: no myip needed
lc.subprocess.run = img_check_ok
primary.try_load_cloud_account = lambda name, config=None, passphrase_prompt=None: (
    {"CLOUDTYPE": "aws", "AWS_REGION": "eu-central-1"}, None)
acct_node = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"cloud_account": "aws-sbx", "config_method": "virt_customize"}},
})
check("validate_lab_definition: a node with a valid cloud_account needs no myip (cloud node)",
      lc.validate_lab_definition(acct_node, single_host_cfg, "/iso", "/lab") is True)

# a missing / unresolvable account file -> a preflight ERROR
lc.subprocess.run = img_check_ok
primary.try_load_cloud_account = lambda name, config=None, passphrase_prompt=None: (
    None, "cloud account 'ghost' not found — looked for ...")
bad_acct = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"cloud_account": "ghost", "config_method": "virt_customize"}},
})
_b = _acct_io.StringIO()
with _acct_redirect(_b):
    ok = lc.validate_lab_definition(bad_acct, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an unresolvable cloud_account is a preflight ERROR",
      ok is False and "not found" in _b.getvalue())

# account cloudtype disagreeing with an explicit backend -> ERROR
lc.subprocess.run = img_check_ok
primary.try_load_cloud_account = lambda name, config=None, passphrase_prompt=None: ({"CLOUDTYPE": "aws"}, None)
mismatch = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"cloud_account": "aws-sbx", "backend": "gcp", "config_method": "virt_customize"}},
})
_b = _acct_io.StringIO()
with _acct_redirect(_b):
    ok = lc.validate_lab_definition(mismatch, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: cloud_account cloudtype vs. an explicit disagreeing backend is an ERROR",
      ok is False and "make them agree" in _b.getvalue())

primary.try_load_cloud_account = _orig_try_acct


# ── common.SOURCE_IMAGE is required only when a node lacks its own override ────
# A lab whose nodes all set their own SOURCE_IMAGE needs no common default. virt_customize is used on every node, so the test covers only the SOURCE_IMAGE rule.
common_no_iso = dict(base_common)
common_no_iso["SOURCE_IMAGE"] = ""
# "echo ok" -> SSH reachability check for the image-exists-on-hypervisor
# check; everything else (the "test -f" image check itself) falls through
# to the default FakeCompleted(returncode=0) — succeeds.
lc.subprocess.run = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

lab_all_nodes_override_iso = _lab_def({
    "common": common_no_iso,
    "nodes": {
        "vm1": {"myip": "192.168.1.70", "SOURCE_IMAGE": "vm1.qcow2", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.71", "SOURCE_IMAGE": "vm2.qcow2", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: empty common.SOURCE_IMAGE passes when every node overrides it",
      lc.validate_lab_definition(lab_all_nodes_override_iso, single_host_cfg, "/iso", "/lab") is True)

lab_one_node_missing_iso = _lab_def({
    "common": common_no_iso,
    "nodes": {
        "vm1": {"myip": "192.168.1.72", "SOURCE_IMAGE": "vm1.qcow2", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.73", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: empty common.SOURCE_IMAGE still fails when a node has no override",
      lc.validate_lab_definition(lab_one_node_missing_iso, single_host_cfg, "/iso", "/lab") is False)


# ── config_method="" (Ignition+Combustion) warns for a non-Micro image ──────
# A node with config_method "" on a non-Micro image gets a warning, not an error, because the heuristic can be wrong.
# The warning applies only to the true Ignition default.
import io as _io
from contextlib import redirect_stdout as _redirect_stdout

lc.subprocess.run = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

lab_ignition_non_micro = _lab_def({
    "common": dict(base_common, **{"SOURCE_IMAGE": "SLES15-SP6-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2"}),
    "nodes": {"vm1": {"myip": "192.168.1.80"}},  # config_method omitted -> Ignition+Combustion
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    # Not asserting ok is True here: config_method="" (Ignition) also
    # requires real ignition/combustion template files to exist at
    # lab_setup_path, a separate, existing check unrelated to this
    # fix — "/lab" (this test's fixture path) never has them, so this
    # particular lab genuinely fails preflight for that reason regardless.
    # What's under test is specifically that the mismatch gets flagged as
    # a WARNING (not folded into/blocked by that unrelated error).
    lc.validate_lab_definition(lab_ignition_non_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: config_method='' + non-Micro image warns about the mismatch",
      "is likely unsupported on SOURCE_IMAGE" in buf.getvalue())
check("validate_lab_definition: the mismatch is reported as a WARNING, not an ERROR",
      "[WARN]" in buf.getvalue().split("is likely unsupported on SOURCE_IMAGE")[0].splitlines()[-1])

lab_ignition_micro = _lab_def({
    "common": dict(base_common, **{"SOURCE_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2"}),
    "nodes": {"vm1": {"myip": "192.168.1.81"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_ignition_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: config_method='' + a genuine SLE Micro image warns about nothing",
      "is likely unsupported on SOURCE_IMAGE" not in buf.getvalue())

# cloud-init is not supported on the kvm-and-xen Minimal-VM image family, which uses JeOS Firstboot. The test expects a warning
# for cloud-init on that image. Only virt_customize works for this family.
lab_cloudinit_non_micro = _lab_def({
    "common": dict(base_common, **{
        "SOURCE_IMAGE": "SLES15-SP6-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.82"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_non_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: cloud-init on a kvm-and-xen Minimal-VM image also warns (JeOS Firstboot, not cloud-init)",
      "is likely unsupported on SOURCE_IMAGE" in buf.getvalue())

# ... but an image this heuristic doesn't recognize at all is left alone,
# regardless of config_method — "if distribution is not in the list then
# just continue" (explicit design requirement, not an oversight).
lab_cloudinit_unknown_image = _lab_def({
    "common": dict(base_common, **{
        "SOURCE_IMAGE": "some-completely-unrecognized-image.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.83"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_unknown_image, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an unrecognized SOURCE_IMAGE is never warned about",
      "is likely unsupported on SOURCE_IMAGE" not in buf.getvalue())

# The Debian nocloud pattern requires "debian" as well as "nocloud". Alibaba's nocloud images use the same word for another
# purpose and do ship cloud-init, so "nocloud" alone must not match them.
lab_cloudinit_aliyun = _lab_def({
    "common": dict(base_common, **{
        "SOURCE_IMAGE": "aliyun_2_1903_x64_20G_nocloud_alibase_20230103.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.84"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_aliyun, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: Alibaba Cloud Linux's own \"nocloud\" filename quirk doesn't "
      "false-positive as Debian's nocloud (no-cloud-init) variant",
      "is likely unsupported on SOURCE_IMAGE" not in buf.getvalue())

# ... but the real Debian nocloud variant still correctly warns (guards
# against the fix above being too permissive in the other direction).
lab_cloudinit_debian_nocloud = _lab_def({
    "common": dict(base_common, **{
        "SOURCE_IMAGE": "debian-12-nocloud-amd64.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.85"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_debian_nocloud, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: Debian's own nocloud variant (no cloud-init) still warns",
      "is likely unsupported on SOURCE_IMAGE" in buf.getvalue())


# ── config_method enum validation ────────────────────────────────────────────
# A config_method that matches no create_vm() branch, such as virt-customize with a hyphen, must be reported.
lab_bad_config_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {"myip": "192.168.1.90", "config_method": "virt-customize"}},
})
ok = lc.validate_lab_definition(lab_bad_config_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an invalid config_method value fails preflight", ok is False)


# ── installer-ISO vs. config_method mismatch ─────────────────────────────────
# An SOURCE_IMAGE ending in .iso used with any config_method other than install_iso fails in copy_vm_image(). It is an error.
lab_iso_wrong_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {
        "myip": "192.168.1.91", "config_method": "cloud-init",
        "SOURCE_IMAGE": "ubuntu-24.04-live-server-amd64.iso",
    }},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    ok = lc.validate_lab_definition(lab_iso_wrong_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an installer ISO with a non-install_iso config_method fails preflight",
      ok is False and "is an installer ISO but config_method is" in buf.getvalue())
check("validate_lab_definition: the installer-ISO mismatch doesn't also fire the softer "
      "compatibility warning (would just be redundant noise on top of the real error)",
      "is likely unsupported on SOURCE_IMAGE" not in buf.getvalue())

lab_iso_right_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {
        "myip": "192.168.1.92", "config_method": "install_iso",
        "SOURCE_IMAGE": "ubuntu-24.04-live-server-amd64.iso",
    }},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_iso_right_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an installer ISO with config_method=install_iso is not flagged",
      "is an installer ISO but config_method is" not in buf.getvalue())


# ── an explicit per-node config_method="" overrides a non-empty common ──────
# The compatibility check treats a per-node empty config_method as an explicit value. An SL Micro node that selects Ignition
# under a cloud-init common does not warn.
lab_explicit_empty_override = _lab_def({
    "common": dict(base_common, **{
        "SOURCE_IMAGE": "SL-Micro.x86_64-6.2-Default-qcow-GM.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.93", "config_method": ""}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_explicit_empty_override, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an explicit per-node config_method=\"\" wins over a non-empty "
      "common.config_method, instead of silently inheriting it",
      "is likely unsupported on SOURCE_IMAGE" not in buf.getvalue())


# ── VM_DSK auto-raise when it's below the source image's virtual size ───────
# `qemu-img resize` fails if asked to shrink below the image's own virtual
# size — detect it in preflight, bump VM_DSK up in memory, and warn.
_GIB = 1024 ** 3
_qemu_info_20g = FakeRun(responses=[
    ("echo ok", FakeCompleted(returncode=0, stdout="ok")),
    ("qemu-img info", FakeCompleted(returncode=0, stdout=json.dumps({"virtual-size": 20 * _GIB}))),
])
lc.subprocess.run = _qemu_info_20g
undersized = _lab_def({
    "common": dict(base_common, VM_DSK="8"),
    "nodes": {"vm1": {"myip": "192.168.1.94", "mymac": "aa:bb:cc:dd:ee:94",
                       "config_method": "virt_customize"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    ok = lc.validate_lab_definition(undersized, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an undersized VM_DSK is a WARNING, not an ERROR (preflight still passes)",
      ok is True)
check("validate_lab_definition: it warns that VM_DSK is being raised to the image's size",
      "raising VM_DSK to 20 GiB" in buf.getvalue())
check("validate_lab_definition: it bumps the node's VM_DSK to the image size in memory",
      undersized["nodes"]["vm1"]["VM_DSK"] == "20")

# A VM_DSK already >= the image size is left alone, no warning.
lc.subprocess.run = _qemu_info_20g
big_enough = _lab_def({
    "common": dict(base_common, VM_DSK="40"),
    "nodes": {"vm1": {"myip": "192.168.1.95", "mymac": "aa:bb:cc:dd:ee:95",
                       "config_method": "virt_customize"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(big_enough, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: a big-enough VM_DSK is left untouched",
      big_enough["nodes"]["vm1"].get("VM_DSK", "40") == "40" and "raising VM_DSK" not in buf.getvalue())


# ── issues_out: hands the raw preflight issue lines back to the caller ──────
lc.subprocess.run = _qemu_info_20g
collected = []
lc.validate_lab_definition(_lab_def({
    "common": dict(base_common, VM_DSK="8"),
    "nodes": {"vm1": {"myip": "192.168.1.96", "config_method": "virt_customize"}},
}), single_host_cfg, "/iso", "/lab", issues_out=collected)
check("validate_lab_definition: issues_out receives the preflight's issue lines",
      any("raising VM_DSK" in line for line in collected))


# ── set_debug / _looks_noisy: quiet-by-default ssh_run output gating ────────
check("_looks_noisy: flags a line containing 'ERROR'", lc._looks_noisy("something ERROR here") is True)
check("_looks_noisy: flags a line containing 'warning'", lc._looks_noisy("a warning: x") is True)
check("_looks_noisy: a clean line is not noisy", lc._looks_noisy("all fine, done") is False)

lc.set_debug(False)
_quiet_ok = FakeRun(responses=[("", FakeCompleted(returncode=0, stdout="lots of chatter\n"))])
lc.subprocess.run = _quiet_ok
_b = _io.StringIO()
with _redirect_stdout(_b):
    lc.ssh_run("host1", "some command", check=False)
check("ssh_run: a clean command's stdout is suppressed when debug is off", _b.getvalue() == "")

_noisy = FakeRun(responses=[("", FakeCompleted(returncode=0, stdout="WARNING: disk almost full\n"))])
lc.subprocess.run = _noisy
_b = _io.StringIO()
with _redirect_stdout(_b):
    lc.ssh_run("host1", "some command", check=False)
check("ssh_run: a command whose output looks like a warning is still shown", "disk almost full" in _b.getvalue())

_failed = FakeRun(responses=[("", FakeCompleted(returncode=3, stdout="it broke\n"))])
lc.subprocess.run = _failed
_b = _io.StringIO()
with _redirect_stdout(_b):
    lc.ssh_run("host1", "some command", check=False)
check("ssh_run: a failing command's output is shown even without debug", "it broke" in _b.getvalue())

import subprocess  # noqa: E402
from contextlib import redirect_stderr as _redirect_stderr  # noqa: E402

_fq = FakeRun()
lc.subprocess.run = _fq
lc.ssh_run("host1", "true", check=False)
lc.scp_to("host1", "/tmp/a", "/tmp/b")
check("ssh_run/scp_to: LogLevel=ERROR, not -q, when debug is off",
      all("-o LogLevel=ERROR" in c and " -q " not in c for c, _ in _fq.calls))
check("set_debug(False): LAB_DEBUG is not exported", "LAB_DEBUG" not in os.environ)

lc.set_debug(True)
_fd = FakeRun()
lc.subprocess.run = _fd
with _redirect_stdout(_io.StringIO()):
    lc.ssh_run("host1", "true", check=False)
lc.scp_to("host1", "/tmp/a", "/tmp/b")
check("ssh_run/scp_to: no quieting options when debug is on",
      not any(" -q " in c or "LogLevel" in c for c, _ in _fd.calls))
check("set_debug(True): LAB_DEBUG=1 is exported for child processes", os.environ.get("LAB_DEBUG") == "1")
check("set_debug(True): _SSH_BASE itself is left unchanged", "LogLevel=ERROR" in lc._SSH_BASE)
check("_ssh_opts: debug also drops a caller's own -q",
      lc._ssh_opts(["ssh", "-q", "-o", "ConnectTimeout=5"]) == ["ssh", "-o", "ConnectTimeout=5"])
lc.set_debug(False)

# ssh_run's failure message: whole command, and ssh's own error on rc=255
_long = "echo " + "x" * 200
lc.subprocess.run = FakeRun(responses=[("", FakeCompleted(returncode=255, stdout="",
                                                          stderr="Could not resolve hostname h9"))])
try:
    lc.ssh_run("h9", _long, capture=True)
    _msg = ""
except RuntimeError as e:
    _msg = str(e)
check("ssh_run: failure message carries the whole command", _long in _msg)
check("ssh_run: rc=255 failure quotes ssh's own error", "Could not resolve hostname h9" in _msg)

# require_ssh(): dies with ssh's reason after ssh_reachable()'s retries; returns quietly when reachable
_real_sleep = lc.time.sleep
lc.time.sleep = lambda s: None
lc.subprocess.run = FakeRun(responses=[("", FakeCompleted(returncode=255, stdout="",
                                                          stderr="Could not resolve hostname h9"))])
_e = _io.StringIO()
try:
    with _redirect_stderr(_e):
        lc.require_ssh("h9")
    _died = False
except SystemExit:
    _died = True
check("require_ssh: dies when the host never answers", _died)
check("require_ssh: names ssh's own error", "Could not resolve hostname h9" in _e.getvalue())
lc.subprocess.run = FakeRun()
try:
    lc.require_ssh("h1")
    _ok = True
except SystemExit:
    _ok = False
check("require_ssh: returns when the host answers", _ok)
lc.time.sleep = _real_sleep

# detect_os(): its line names the host it read
lc.subprocess.run = FakeRun(responses=[("os-release", FakeCompleted(stdout="sles|16.1|suse"))])
_b = _io.StringIO()
with _redirect_stdout(_b):
    _info = lc.detect_os("dns1.mydemo.lab")
check("detect_os: the printed line names the host", "Detected remote OS on dns1.mydemo.lab: id=sles" in _b.getvalue())
check("detect_os: returns the parsed fields", _info["id"] == "sles" and _info["ver_major"] == "16")

_child = _real_subprocess_run(
    [sys.executable, "-c", "import sys; sys.path.insert(0, {!r}); import lab_creation; "
                           "print(lab_creation.debug_enabled())".format(str(_REPO / "libs"))],
    env=dict(os.environ, LAB_DEBUG="1"), stdout=subprocess.PIPE, universal_newlines=True)
check("LAB_DEBUG=1 in the environment turns debug on at import", _child.stdout.strip() == "True")


# ── prepare_cloud_init(): network_renderer defaulting/override ──────────────
# Real bash-eval process_template() against the real shipped templates (not
# mocked) — a real substitution bug in template_network-config would still be
# caught here, matching 30_setup_harvester_cluster_test.py's own precedent.
# Restore the real subprocess.run first — several tests above this point
# leave lc.subprocess.run monkeypatched to a fake for their own purposes.
lc.subprocess.run = _real_subprocess_run


def _render_network_config(variables):
    # prepare_cloud_init() always reads /root/.ssh/id_rsa.pub (mirrors bash's own
    # local `ROOT_SSH_KEY=$(cat ~/.ssh/id_rsa.pub)` reassignment) — this
    # container has no such key yet; ensure one exists rather than mock the
    # read, since a real automation VM always has one from its own bootstrap.
    pubkey_path = Path("/root/.ssh/id_rsa.pub")
    pubkey_path.parent.mkdir(parents=True, exist_ok=True)
    if not pubkey_path.exists():
        pubkey_path.write_text("ssh-rsa AAAAtest test@test\n")
    with tempfile.TemporaryDirectory() as tmp:
        ci_dir = Path(tmp) / "cloud-init"
        ci_dir.mkdir()
        for kind in ("user-data", "network-config", "network-config-dhcp", "network-config-dhcp-nomac", "meta-data"):
            src = _REPO / "templates" / "cloud-init.template_{}".format(kind)
            (ci_dir / "template_{}".format(kind)).write_text(src.read_text())
        lc.prepare_cloud_init("vm1.mydemo.lab", tmp, variables)
        return (ci_dir / "vm1.mydemo.lab_network-config").read_text()


def _render_user_data(variables):
    pubkey_path = Path("/root/.ssh/id_rsa.pub")
    pubkey_path.parent.mkdir(parents=True, exist_ok=True)
    if not pubkey_path.exists():
        pubkey_path.write_text("ssh-rsa AAAAtest test@test\n")
    with tempfile.TemporaryDirectory() as tmp:
        ci_dir = Path(tmp) / "cloud-init"
        ci_dir.mkdir()
        for kind in ("user-data", "network-config", "network-config-dhcp", "network-config-dhcp-nomac", "meta-data"):
            src = _REPO / "templates" / "cloud-init.template_{}".format(kind)
            (ci_dir / "template_{}".format(kind)).write_text(src.read_text())
        lc.prepare_cloud_init("vm1.mydemo.lab", tmp, variables)
        return (ci_dir / "vm1.mydemo.lab_user-data").read_text()

_base_vars = {
    "_vm_name": "vm1.mydemo.lab", "mymac": "52:54:00:aa:bb:cc", "myip": "192.168.1.50",
    "mymask": "24", "mygw": "192.168.1.1", "mydns": "192.168.1.1", "mydomain": "mydemo.lab",
    "SOURCE_IMAGE": "", "ROOT_PWD_HASH": "x",
}

rendered = _render_network_config(dict(_base_vars))
check("prepare_cloud_init defaults network_renderer to NetworkManager when the lab JSON omits it "
      "(SLE Micro/SLES/Leap's own default — unchanged behavior for every existing lab)",
      "renderer: NetworkManager" in rendered)

# The non-Ubuntu branch of template_user-data disables firewalld. SLES images enable it by default, and it drops inbound SSH.
# The command is a no-op on images without firewalld.
rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ami-sles15sp7-byos"))
check("prepare_cloud_init: a non-Ubuntu image's user-data disables firewalld via runcmd "
      "(real bug: a SLES BYOS AMI's default-enabled firewalld silently blocked inbound SSH "
      "even though the security group and NACL were both correctly configured)",
      "systemctl disable --now firewalld" in rendered)

rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ubuntu-24.04-server-cloudimg-amd64.img"))
check("prepare_cloud_init: an Ubuntu image's user-data does NOT get the firewalld runcmd "
      "(it has its own netplan runcmd instead, from the existing ubuntu branch)",
      "firewalld" not in rendered and "netplan apply" in rendered)

# A guarded /etc/hosts entry for the FQDN is appended in the runcmd of both branches. Without it, mgradm cannot compute the server
# FQDN, because the VPC DNS does not know the lab's own zone. The append is idempotent.
rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ami-sles15sp7-byos"))
check("prepare_cloud_init: a non-Ubuntu image's user-data adds a self-referential /etc/hosts "
      "entry for its own FQDN (real bug: mgradm's own hostname -f failed without one, since "
      "AWS's VPC DNS resolver knows nothing about this lab's custom on-prem zone)",
      'grep -qF "vm1.mydemo.lab" /etc/hosts || echo "127.0.0.1 vm1.mydemo.lab vm1" >> /etc/hosts'
      in rendered)

rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ubuntu-24.04-server-cloudimg-amd64.img"))
check("prepare_cloud_init: an Ubuntu image's user-data gets the same self-referential "
      "/etc/hosts entry too, alongside its own netplan runcmd",
      'grep -qF "vm1.mydemo.lab" /etc/hosts || echo "127.0.0.1 vm1.mydemo.lab vm1" >> /etc/hosts'
      in rendered and "netplan apply" in rendered)

# The SLES cloud-init build can drop the default gateway route when it writes the network configuration. The runcmd of the
# non-Ubuntu branch therefore writes the wicked routes file directly, and applies the route at runtime. Both steps are idempotent
# and non-fatal.
rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ami-sles15sp7-byos"))
check("prepare_cloud_init: a non-Ubuntu image's user-data writes a default route into wicked's "
      "own routes file directly (real bug: this SLES image's bundled cloud-init silently drops "
      "the gateway route from its own network-config, even when the route data is otherwise "
      "correct — confirmed via `cloud-init devel net-convert` and a live TypeError crash)",
      'grep -qF "default 192.168.1.1" /etc/sysconfig/network/routes 2>/dev/null || '
      "printf 'default %s - -\\n' \"192.168.1.1\" >> /etc/sysconfig/network/routes" in rendered)
check("prepare_cloud_init: the same fix also applies the route immediately at runtime, not just "
      "persists it for the next boot",
      "ip route show default | grep -q . || ip route add default via 192.168.1.1 || true" in rendered)

rendered = _render_user_data(dict(_base_vars, SOURCE_IMAGE="ubuntu-24.04-server-cloudimg-amd64.img"))
check("prepare_cloud_init: an Ubuntu image's user-data does NOT get the wicked-routes-file "
      "workaround (Ubuntu's own netplan runcmd already applies routes correctly)",
      "/etc/sysconfig/network/routes" not in rendered)

rendered = _render_network_config(dict(_base_vars, network_renderer="networkd"))
check("prepare_cloud_init honors an explicit network_renderer override (e.g. for an Ubuntu guest, "
      "whose distro defaults to systemd-networkd, not NetworkManager)",
      "renderer: networkd" in rendered)

rendered = _render_network_config(dict(_base_vars, network_renderer=""))
check("prepare_cloud_init treats an explicit empty-string network_renderer the same as omitted, "
      "not as a literal blank renderer line",
      "renderer: NetworkManager" in rendered)

# An empty/omitted myip (the USB-delivery lab-host VM's own case — its
# address is unknown at build time, unlike every other node this project
# creates) selects the DHCP template instead of static addressing.
dhcp_vars = dict(_base_vars)
dhcp_vars["myip"] = ""
rendered = _render_network_config(dhcp_vars)
check("prepare_cloud_init: empty myip selects the DHCP network-config template",
      "dhcp4: true" in rendered and "addresses:" not in rendered)

del dhcp_vars["myip"]
rendered = _render_network_config(dhcp_vars)
check("prepare_cloud_init: omitted myip (not just empty-string) also selects DHCP",
      "dhcp4: true" in rendered)

check("prepare_cloud_init: a real static myip still gets the static template, unchanged",
      "dhcp4: false" in _render_network_config(dict(_base_vars))
      and "addresses:" in _render_network_config(dict(_base_vars)))

# A cloud backend leaves both myip and mymac empty. The mac-matched DHCP template would then render an empty match, so the
# name-glob template is used only when both are empty. The USB lab-host VM has a mymac and keeps the mac-matched template.
cloud_vars = dict(_base_vars)
cloud_vars["myip"] = ""
cloud_vars["mymac"] = ""
rendered = _render_network_config(cloud_vars)
check("prepare_cloud_init: empty myip AND empty mymac (a cloud backend node) selects the "
      "name-glob DHCP template, not an empty macaddress match",
      "dhcp4: true" in rendered and "macaddress" not in rendered and 'name: "e*"' in rendered)

usb_host_vars = dict(_base_vars)
usb_host_vars["myip"] = ""
rendered = _render_network_config(usb_host_vars)
check("prepare_cloud_init: empty myip with a real mymac (the USB-delivery lab-host VM's own "
      "case) still gets the original macaddress-matched DHCP template, unaffected",
      "dhcp4: true" in rendered and 'macaddress: "52:54:00:aa:bb:cc"' in rendered)

# setup_vm.py does not put _vm_name in the variables it passes to prepare_cloud_init(). This test omits _vm_name on purpose,
# so the caller contract is tested, not the fixture's own _vm_name.
vars_without_vm_name = dict(_base_vars)
del vars_without_vm_name["_vm_name"]
with tempfile.TemporaryDirectory() as tmp:
    ci_dir = Path(tmp) / "cloud-init"
    ci_dir.mkdir()
    for kind in ("user-data", "network-config", "network-config-dhcp", "meta-data"):
        src = _REPO / "templates" / "cloud-init.template_{}".format(kind)
        (ci_dir / "template_{}".format(kind)).write_text(src.read_text())
    lc.prepare_cloud_init("vm1.mydemo.lab", tmp, vars_without_vm_name)
    meta_data = (ci_dir / "vm1.mydemo.lab_meta-data").read_text()
check("prepare_cloud_init: instance-id/local-hostname are populated even when the caller "
      "doesn't pass _vm_name explicitly",
      "instance-id: vm1.mydemo.lab" in meta_data and "local-hostname: vm1.mydemo.lab" in meta_data)


# ── check_ssh_only_reachability: Python 3.6 subprocess arguments ─────────────
# subprocess.run() is called with stdout=PIPE, stderr=PIPE and universal_newlines=True. The test suite runs on Python 3.6.
def _py36_strict_run(args, **kwargs):
    if "capture_output" in kwargs or "text" in kwargs:
        raise TypeError("run() got an unexpected keyword argument (mimics Python 3.6)")
    return FakeCompleted(stdout="ok\n")


targets.subprocess.run = _py36_strict_run
ssh_reach_error = None
try:
    result = _real_check_ssh_only_reachability("vm1.mydemo.lab")
except TypeError as e:
    ssh_reach_error = e
check("check_ssh_only_reachability: never raises TypeError under Python-3.6-strict "
      "subprocess.run kwargs", ssh_reach_error is None)
check("check_ssh_only_reachability: still returns the correct True/False result "
      "once past that", result is True)


# ── ensure_iso_install_tree ────────────────────────────────────────────────
# A bare path given to --location fails when virt-install runs on another host. The ISO is mounted on the hypervisor and served over HTTP instead.
fake = FakeRun(responses=[
    ("mountpoint -q", FakeCompleted(returncode=1)),   # not yet mounted
    ("systemctl is-active install-iso-server.service", FakeCompleted(returncode=1)),  # not yet running
])
lc.subprocess.run = fake
url = lc.ensure_iso_install_tree("hv1", "/iso", "rhel-10.2-x86_64-dvd.iso")
cmds = [c[0] for c in fake.calls]
check("ensure_iso_install_tree: returns an http:// URL naming the hypervisor and the ISO",
      url == "http://hv1:8890/rhel-10.2-x86_64-dvd.iso/")
check("ensure_iso_install_tree: not-yet-mounted -> mkdir then a loop,ro mount",
      any("mkdir -p" in c and "/iso/.mounted-isos/rhel-10.2-x86_64-dvd.iso" in c for c in cmds)
      and any("mount -o loop,ro" in c and "/iso/rhel-10.2-x86_64-dvd.iso" in c for c in cmds))
check("ensure_iso_install_tree: persists the mount in /etc/fstab with nofail (dedup-checked append)",
      any("/etc/fstab" in c and "nofail" in c and "grep -qxF" in c for c in cmds))
check("ensure_iso_install_tree: not-yet-running -> writes and enables the shared HTTP-server systemd unit",
      any("install-iso-server.service" in c and "cat >" in c for c in cmds)
      and any("systemctl daemon-reload && systemctl enable --now install-iso-server.service" in c for c in cmds))
unit_write_call = next(c for c in cmds if "install-iso-server.service" in c and "cat >" in c)
check("ensure_iso_install_tree: the HTTP server's own WorkingDirectory is the SHARED "
      "mounted-isos root, not this one ISO's own subdirectory (one server for every ISO)",
      "WorkingDirectory=/iso/.mounted-isos" in unit_write_call
      and "WorkingDirectory=/iso/.mounted-isos/rhel-10.2-x86_64-dvd.iso" not in unit_write_call)

# Already mounted + server already running -> no mkdir/mount/unit-write calls at all,
# just the URL (idempotent) and the harmless fstab dedup check.
fake = FakeRun(responses=[
    ("mountpoint -q", FakeCompleted(returncode=0)),
    ("systemctl is-active install-iso-server.service", FakeCompleted(returncode=0)),
])
lc.subprocess.run = fake
url = lc.ensure_iso_install_tree("hv1", "/iso", "rhel-10.2-x86_64-dvd.iso")
cmds = [c[0] for c in fake.calls]
check("ensure_iso_install_tree: already mounted + server already running -> no mount/unit-write calls",
      not any("mount -o loop" in c for c in cmds) and not any("cat >" in c for c in cmds))
check("ensure_iso_install_tree: still returns the correct URL when everything was already in place",
      url == "http://hv1:8890/rhel-10.2-x86_64-dvd.iso/")

fake = FakeRun(responses=[
    ("mountpoint -q", FakeCompleted(returncode=1)),
    ("mount -o loop,ro", FakeCompleted(returncode=1, stderr="mount: wrong fs type")),
])
lc.subprocess.run = fake
died = False
try:
    lc.ensure_iso_install_tree("hv1", "/iso", "bogus.iso")
except SystemExit:
    died = True
check("ensure_iso_install_tree: a real mount failure dies with a clear error naming the ISO/host", died)


# ── prepare_virt_customize: /etc/hostname is written directly ─────────────────
# SLES 16 reads /etc/hostname, but libguestfs --hostname writes the legacy /etc/HOSTNAME. /etc/hostname is therefore written
# directly with --run-command, in addition to --hostname.
fake = FakeRun()
lc.subprocess.run = fake
with tempfile.TemporaryDirectory() as _tmp:
    id_rsa = Path(_tmp) / "id_rsa.pub"
    id_rsa.write_text("")  # prepare_virt_customize doesn't read this itself; just avoid IOError elsewhere
    lc.prepare_virt_customize(
        "hv1", "/var/lib/libvirt/images/venus.qcow2", "venus.mydemo.lab",
        "192.168.88.142", "24", "192.168.88.1", "192.168.88.73", "mydemo.lab",
        "aa:bb:cc:dd:ee:ff", "plain", "secret123", pubkey="ssh-rsa AAAA test@host",
    )
check("prepare_virt_customize: exactly one subprocess.run call (the piped hypervisor script)",
      len(fake.calls) == 1)
_vc_cmd, _vc_kwargs = fake.calls[0]
_vc_script = _vc_kwargs.get("input") or ""
if isinstance(_vc_script, bytes):
    _vc_script = _vc_script.decode()
check("prepare_virt_customize: the shipped hypervisor script is valid Python "
      "(catches syntax errors in the generated script)",
      __import__("ast").parse(_vc_script) is not None if _vc_script else False)
check("prepare_virt_customize: writes /etc/hostname directly via --run-command, not relying "
      "solely on --hostname (which libguestfs gets wrong for this image)",
      "echo {}.format(vmname)" in _vc_script.replace("\"", "").replace("'", "")
      or "> /etc/hostname\".format(vmname)" in _vc_script)
check("prepare_virt_customize: still passes --hostname too (belt and suspenders, not a "
      "replacement — some distros DO need the legacy file)",
      '"--hostname",vmname' in _vc_script.replace(" ", "")
      or '"--hostname", vmname' in _vc_script)


# ── ensure_swap(): a small cloud node's package install can get OOM-killed, so add swap first ──
_real_ssh_run = lc.ssh_run


class _FakeSwapResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_swap_calls = []


def _meminfo_result(total_mb):
    return _FakeSwapResult(stdout=str(total_mb * 1024))


lc.ssh_run = lambda hostname, cmd, **kw: _meminfo_result(4096)
check("ensure_swap: a node that already clears the threshold is left alone (no second ssh_run)",
      lc.ensure_swap("big.mydemo.lab", min_total_mb=2048) is True)

_swap_cmd = []


def _small_then_add(hostname, cmd, **kw):
    if "meminfo" in cmd:
        return _meminfo_result(900)
    _swap_cmd.append(cmd)
    return _FakeSwapResult()


lc.ssh_run = _small_then_add
check("ensure_swap: a node below the threshold gets a swapfile added",
      lc.ensure_swap("small.mydemo.lab", min_total_mb=2048, swap_mb=1024) is True)
check("ensure_swap: the swap command creates, activates and persists the swapfile",
      len(_swap_cmd) == 1 and "mkswap" in _swap_cmd[0] and "swapon" in _swap_cmd[0]
      and "/etc/fstab" in _swap_cmd[0] and "1024" in _swap_cmd[0])
check("ensure_swap: never uses fallocate — a sparse file can make swapon reject it "
      "with 'Invalid argument' (confirmed live on CentOS 7)", "fallocate" not in _swap_cmd[0])
check("ensure_swap: uses dd instead, which always writes real, contiguous blocks",
      "dd if=/dev/zero" in _swap_cmd[0])
check("ensure_swap: checks whether the swapfile is already active before anything else",
      _swap_cmd[0].strip().startswith("set -e") and
      _swap_cmd[0].split("\n")[1].startswith("swapon --show"))

# A swapfile that's already active must never be touched again: swapon on an ALREADY-active file
# fails too (EBUSY), same exit code as a genuinely bad one — telling those apart by swapon's exit
# code alone (instead of checking "already active" first) was a real bug: it removed a swapfile
# that was actively in use (confirmed live: "rm: cannot remove ...: Operation not permitted", rhel9).
_already_active_cmds = []


def _small_but_already_active(hostname, cmd, **kw):
    if "meminfo" in cmd:
        return _meminfo_result(900)
    _already_active_cmds.append(cmd)
    # The real remote script's own first line checks this and exits 0 immediately — simulate
    # that exact outcome rather than re-implementing the script's logic here.
    return _FakeSwapResult(returncode=0)


lc.ssh_run = _small_but_already_active
check("ensure_swap: still reports success when the remote script finds it already active",
      lc.ensure_swap("active.mydemo.lab", min_total_mb=2048) is True)
check("ensure_swap: sends exactly one remote command (the script decides on its own side, "
      "nothing here branches on 'already active' separately)", len(_already_active_cmds) == 1)

_warned = []
_real_warn = lc.warn
lc.warn = _warned.append


def _small_then_fail(hostname, cmd, **kw):
    if "meminfo" in cmd:
        return _meminfo_result(900)
    return _FakeSwapResult(returncode=1, stderr="no space left on device")


lc.ssh_run = _small_then_fail
check("ensure_swap: a failure to add swap warns (never dies) and reports False",
      lc.ensure_swap("tiny.mydemo.lab", min_total_mb=2048) is False
      and len(_warned) == 1 and "tiny.mydemo.lab" in _warned[0])

lc.ssh_run = lambda hostname, cmd, **kw: _FakeSwapResult(stdout="not-a-number")
check("ensure_swap: unparseable /proc/meminfo output warns and returns False, doesn't crash",
      lc.ensure_swap("weird.mydemo.lab") is False)

lc.ssh_run = _real_ssh_run
lc.warn = _real_warn

# ── ensure_centos7_vault_repos(): CentOS 7's mirrorlist.centos.org is gone for good post-EOL ──
_os_calls = []


def _os_release(hostname, cmd, **kw):
    _os_calls.append((hostname, cmd))
    if "os-release" in cmd:
        return _FakeSwapResult(stdout="centos|7|rhel fedora")
    return _FakeSwapResult()


lc.ssh_run = _os_release
check("ensure_centos7_vault_repos: a CentOS 7 node runs the vault repoint",
      lc.ensure_centos7_vault_repos("luna.mydemo.lab") is True
      and any("vault.centos.org" in cmd for _, cmd in _os_calls))

_os_calls.clear()


def _os_release_other(hostname, cmd, **kw):
    _os_calls.append((hostname, cmd))
    if "os-release" in cmd:
        return _FakeSwapResult(stdout="sles|15.6|suse")
    return _FakeSwapResult()


lc.ssh_run = _os_release_other
check("ensure_centos7_vault_repos: a no-op (and no second ssh_run at all) on anything but CentOS 7",
      lc.ensure_centos7_vault_repos("mercury.mydemo.lab") is True and len(_os_calls) == 1)

_warned = []
_real_warn = lc.warn
lc.warn = _warned.append


def _os_release_fails(hostname, cmd, **kw):
    if "os-release" in cmd:
        return _FakeSwapResult(stdout="centos|7|rhel fedora")
    return _FakeSwapResult(returncode=1, stderr="permission denied")


lc.ssh_run = _os_release_fails
check("ensure_centos7_vault_repos: a failure to patch the repos warns (never dies) and reports False",
      lc.ensure_centos7_vault_repos("broken.mydemo.lab") is False
      and len(_warned) == 1 and "broken.mydemo.lab" in _warned[0])

# ── a hand-written myip is held to the rules the allocator applies ───────────
# ipalloc.allocate() rules these out for an address it picks. An address named in the JSON used to be trusted on
# the grounds that the lab knows better, which is how two labs come to claim one address and the second one takes
# the first one's name down with it.
import ipalloc  # noqa: E402
from unittest import mock  # noqa: E402


def _preflight_messages(nodes, zone_text, pings=(), local=()):
    """The ERROR/WARN lines validate_lab_definition() produces for `nodes`, with the lab zone faked to `zone_text`."""
    issues = []
    with tempfile.TemporaryDirectory() as zone_dir:
        Path(zone_dir, "mydemo.lab.lan").write_text(zone_text)
        definition = _lab_def({"common": dict(base_common, mydomain="mydemo.lab", mygw="192.168.88.1",
                                              mymask="24", mydns="192.168.88.73"),
                               "nodes": nodes})
        lc.subprocess.run = img_check_ok
        with mock.patch.object(ipalloc, "NAMED_ZONE_DIR", Path(zone_dir)), \
                mock.patch.object(ipalloc, "local_ips", return_value=set(local)), \
                mock.patch.object(ipalloc, "answers_ping", side_effect=lambda ip: str(ip) in pings):
            lc.validate_lab_definition(definition, single_host_cfg, "/iso", "/lab", issues_out=issues)
    return [str(i) for i in issues]


ZONE = ("$TTL 300\n@ IN SOA ns admin ( 1 )\nnode9         IN  A       192.168.88.9\n")
_NODE = {"mymac": "aa:bb:cc:dd:ee:09", "config_method": "virt_customize"}

taken = _preflight_messages({"n1.mydemo.lab": dict(_NODE, myip="192.168.88.9")}, ZONE)
check("validate_lab_definition: an address the lab zone holds under another name is refused, not warned about",
      any("already has a DNS record under the name 'node9'" in m for m in taken))
check("validate_lab_definition: ...and the refusal says what to do about it",
      any("dns_sync.py --prune" in m for m in taken))

own = _preflight_messages({"n1.mydemo.lab": dict(_NODE, myip="192.168.88.73")}, ZONE, local={"192.168.88.73"})
check("validate_lab_definition: this automation node's own address is refused for a node, which would otherwise "
      "take the address every other node reaches DNS and the provisioning files on",
      any("this automation node's own address" in m for m in own))

rebuild = _preflight_messages({"node9.mydemo.lab": dict(_NODE, myip="192.168.88.9")}, ZONE,
                              pings={"192.168.88.9"})
check("validate_lab_definition: a node at the address the zone already holds for that same node is no clash, so "
      "a rerun over an existing lab still validates",
      not any("192.168.88.9" in m for m in rebuild))

stranger = _preflight_messages({"n1.mydemo.lab": dict(_NODE, myip="192.168.88.200")}, ZONE,
                               pings={"192.168.88.200"})
check("validate_lab_definition: an address nothing in the zone claims but something answers on stays a warning: "
      "it may be a machine nobody told us about, and that is not for the preflight to decide",
      any("responding to ping" in m for m in stranger))

lc.ssh_run = _real_ssh_run
lc.warn = _real_warn

# ── the documented hardware minimums (libs/sizing.py), as a preflight warning ──
# A node sized under what a product it runs documents for itself. Warnings never block, so what is asserted is
# that the message is printed, names the field and the product it came from, and that a node sized properly gets
# none. The cluster's own distribution is enough to trigger it: an add-on would also need its install script on
# this interpreter's addon path, which a test container has no reason to carry.
import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402


def _preflight_output(definition, **kwargs):
    """validate_lab_definition()'s whole report, for a test that cares what it said rather than what it returned."""
    lc.subprocess.run = img_check_ok
    buf = io.StringIO()
    with redirect_stdout(buf):
        lc.validate_lab_definition(definition, single_host_cfg, "/iso", "/lab", **kwargs)
    return buf.getvalue()


def _sized_lab(**common):
    return _lab_def({"common": dict(base_common, mydomain="mydemo.lab", **common),
                     "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                       "config_method": "virt_customize", "kcluster": "c1"}},
                     "kclusters": {"c1": {"clu_type": "rke2", "clu_rel": "stable",
                                          "mydomain": "mydemo.lab"}}})


_small = _sized_lab(VM_MEM="2048", VM_CPU="1", VM_DSK="40")
out = _preflight_output(_small, host_checks=False)
check("preflight: a node under its distribution's documented figures is warned about, naming the field",
      "vm1.VM_MEM" in out and "vm1.VM_CPU" in out and "RKE2" in out and "4096" in out)
check("preflight: the warning points at the page the figure came from", "docs.rke2.io" in out)
check("preflight: the disk falls back to this project's own floor, which no product documents",
      "vm1.VM_DSK" in out and "floor for a node image" in out)
check("preflight: it is a warning, so a lab that wants to try anyway still validates",
      lc.validate_lab_definition(_small, single_host_cfg, "/iso", "/lab", host_checks=False) is True)

out = _preflight_output(_sized_lab(VM_MEM="8192", VM_CPU="4", VM_DSK="80"), host_checks=False)
check("preflight: a node that meets every figure is not warned about at all", "asks for at least" not in out)

_plain = _lab_def({"common": dict(base_common, mydomain="mydemo.lab"),
                   "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                     "config_method": "virt_customize"}}})
out = _preflight_output(_plain, host_checks=False)
check("preflight: a lab running nothing with a documented requirement is left alone",
      "asks for at least" not in out)

_agent = _lab_def({"common": dict(base_common, VM_MEM="1024", VM_CPU="1", VM_DSK="60", mydomain="mydemo.lab"),
                   "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                     "config_method": "virt_customize", "kcluster": "c1",
                                     "INSTALL_RKE2_TYPE": "agent"}},
                   "kclusters": {"c1": {"clu_type": "k3s", "clu_rel": "stable",
                                        "mydomain": "mydemo.lab"}}})
out = _preflight_output(_agent, host_checks=False)
check("preflight: a K3s agent is held to the agent's own figures, the smallest in the table, so 1 GB passes",
      "asks for at least" not in out)

_server = _lab_def({"common": dict(base_common, VM_MEM="1024", VM_CPU="1", VM_DSK="60", mydomain="mydemo.lab"),
                    "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                      "config_method": "virt_customize", "kcluster": "c1",
                                      "INSTALL_RKE2_TYPE": "server"}},
                    "kclusters": {"c1": {"clu_type": "k3s", "clu_rel": "stable",
                                         "mydomain": "mydemo.lab"}}})
out = _preflight_output(_server, host_checks=False)
check("preflight: the same node as a K3s server is warned about, since a server asks for more",
      "K3s server" in out and "2048" in out)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab_creation core checks passed")
