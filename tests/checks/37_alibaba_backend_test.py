#!/usr/bin/env python3
# Unit tests for libs/backends.py's AlibabaBackend — every `aliyun` CLI call
# (subprocess.run) is mocked (no real Alibaba Cloud account available
# anywhere in this environment, and no `aliyun` CLI needs to even be
# installed to run these). Asserts command construction, the mandatory
# networking fields, Base64 UserData encoding, config_method enforcement,
# and MAC handling — not real API behavior; see AlibabaBackend's own
# docstring for exactly what remains unverified. Run from
# 37_alibaba_backend.sh, in its own container — see tests/run_tests.sh.
import base64
import json
import sys
import tempfile
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


def _cp(rc, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


_FULL_CONFIG = {
    "ALIBABA_ACCESS_KEY_ID": "keyid", "ALIBABA_ACCESS_KEY_SECRET": "keysecret",
    "ALIBABA_REGION": "cn-hangzhou", "ALIBABA_SECURITY_GROUP_ID": "sg-1", "ALIBABA_VSWITCH_ID": "vsw-1",
}


# ── resolve(): all five config keys are mandatory, individually ───────────
for missing_key in _FULL_CONFIG:
    partial = {k: v for k, v in _FULL_CONFIG.items() if k != missing_key}
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.AlibabaBackend.resolve({}, "vm1", partial, False)
        except SystemExit:
            pass
    check("resolve() dies when {} is missing".format(missing_key), any(missing_key in m for m in died))

resolved = backends.AlibabaBackend.resolve({}, "vm1", _FULL_CONFIG, False)
check("resolve() picks up all five required fields",
      (resolved.access_key_id, resolved.access_key_secret, resolved.region,
       resolved.security_group_id, resolved.vswitch_id) ==
      ("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1"))


backend = backends.AlibabaBackend("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1")


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("m-123", "vm1", 40, config_method="")
    except SystemExit:
        pass
check("copy_vm_image() dies on config_method != cloud-init", any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty ISO_IMAGE", any("ImageId" in m for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("m-0123456789", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real ImageId with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): Base64-encodes the real file content ───────
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.AlibabaBackend("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    decoded = base64.b64decode(b2._user_data_by_vm.get("vm1", "")).decode("utf-8")
    check("push_provisioning_files() Base64-encodes the real file content (round-trips correctly)",
          decoded == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on ECS ─────
check("list_used_macs() returns empty (ECS has no MAC concept this backend uses)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to Alibaba Cloud)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_instance_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_instance_type() picks the smallest sufficient SKU (2 vCPU / 4096 MiB)",
      backend._pick_instance_type(2, 4096, "vm1") == "ecs.g6.large")
check("_pick_instance_type() steps up when memory needs more than cores would suggest",
      backend._pick_instance_type(2, 32768, "vm1") == "ecs.g6.2xlarge")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_instance_type(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_instance_type() dies clearly when nothing in the table is big enough",
      any("INSTANCE_TYPES" in m for m in died))


# ── vm_exists() / _find_instance(): DescribeInstances --InstanceName ──────
_describe_one = _cp(0, stdout=json.dumps(
    {"Instances": {"Instance": [{"InstanceId": "i-1", "Status": "Running"}]}}))
_describe_none = _cp(0, stdout=json.dumps({"Instances": {"Instance": []}}))

with mock.patch.object(backends.subprocess, "run", return_value=_describe_one) as m_run:
    check("vm_exists() returns True when DescribeInstances lists a match", backend.vm_exists("vm1") is True)
    called_args = m_run.call_args[0][0]
    check("vm_exists() filters by --InstanceName",
          "--InstanceName" in called_args and "vm1" in called_args)

with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("vm_exists() returns False when DescribeInstances lists nothing", backend.vm_exists("vm1") is False)


# ── get_ip(): PublicIpAddress preferred, VPC PrivateIpAddress fallback ─────
_with_public_ip = _cp(0, stdout=json.dumps({"Instances": {"Instance": [
    {"InstanceId": "i-1", "PublicIpAddress": {"IpAddress": ["203.0.113.31"]}}]}}))
_private_only_ip = _cp(0, stdout=json.dumps({"Instances": {"Instance": [
    {"InstanceId": "i-1", "PublicIpAddress": {"IpAddress": []},
     "VpcAttributes": {"PrivateIpAddress": {"IpAddress": ["10.0.0.8"]}}}]}}))

with mock.patch.object(backends.subprocess, "run", return_value=_with_public_ip):
    check("get_ip() prefers the public IP when assigned", backend.get_ip("vm1") == "203.0.113.31")
with mock.patch.object(backends.subprocess, "run", return_value=_private_only_ip):
    check("get_ip() falls back to the VPC private IP when no public one is assigned",
          backend.get_ip("vm1") == "10.0.0.8")
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("get_ip() returns None when the instance doesn't exist", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none) as m_run:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls DescribeInstances (no DeleteInstance) when nothing exists",
          m_run.call_count == 1)


# ── create_vm(): real command construction ─────────────────────────────────
b3 = backends.AlibabaBackend("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1")
b3._user_data_by_vm["vm1"] = "ZW5jb2RlZA=="
calls = []


def _fake_run(args, **kwargs):
    calls.append(args)
    if "DescribeInstances" in args:
        # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
        # PublicIpAddress here so the poll succeeds on its first check, not a 180s timeout.
        return _cp(0, stdout=json.dumps({"Instances": {"Instance": [
            {"InstanceId": "i-new", "PublicIpAddress": {"IpAddress": ["203.0.113.30"]}}]}}))
    return _cp(0, stdout="")


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="m-0123456789")

check("create_vm() returns the real IP once the instance is confirmed running", returned_ip == "203.0.113.30")
run_call = next(c for c in calls if "RunInstances" in c)
check("create_vm() sends the real ImageId", "m-0123456789" in run_call)
check("create_vm() picks a real InstanceType", "ecs.g6.large" in run_call)
check("create_vm() uses the real native InstanceName field", "vm1" in run_call)
check("create_vm() sends both mandatory networking IDs", "sg-1" in run_call and "vsw-1" in run_call)
check("create_vm() maps vm_dsk_gb directly via --SystemDisk.Size",
      "--SystemDisk.Size" in run_call and "40" in run_call)
check("create_vm() sends the already Base64-encoded UserData as-is",
      "ZW5jb2RlZA==" in run_call)


# ── cloud_instance_type: explicit override bypasses _pick_instance_type() entirely ──
# No provider's sizing catalog is a hardcoded ceiling. See _parse_sku_table()'s docstring and
# README's Compute backends table.
b5 = backends.AlibabaBackend("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1")
b5._user_data_by_vm["vm1"] = ""
calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    # cpu/mem here would normally pick "ecs.g6.large" — cloud_instance_type must win regardless.
    b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="m-0123456789",
                  cloud_instance_type="ecs.g6.4xlarge")
run_call = next(c for c in calls if "RunInstances" in c)
check("create_vm() uses cloud_instance_type verbatim, bypassing _pick_instance_type()",
      "ecs.g6.4xlarge" in run_call and "ecs.g6.large" not in run_call)


# ── ALIBABA_INSTANCE_TYPES: resolve() parses the config override into instance_types ──
_cfg = dict(_FULL_CONFIG, ALIBABA_INSTANCE_TYPES="tiny:1:2,huge:16:64")
resolved = backends.AlibabaBackend.resolve({}, "vm1", _cfg, False)
check("resolve() parses ALIBABA_INSTANCE_TYPES into resolved.instance_types",
      resolved.instance_types == [("tiny", 1, 2.0), ("huge", 16, 64.0)])

# ── _pick_instance_type(): an overridden table actually replaces INSTANCE_TYPES, not merges ──
b6 = backends.AlibabaBackend("keyid", "keysecret", "cn-hangzhou", "sg-1", "vsw-1",
                              instance_types=[("tiny", 1, 2.0), ("huge", 16, 64.0)])
check("_pick_instance_type() picks from the overridden table when instance_types is set",
      b6._pick_instance_type(1, 2048, "vm1") == "tiny")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        b6._pick_instance_type(32, 4096, "vm1")  # too big for this override's max (16 cores)
    except SystemExit:
        pass
check("_pick_instance_type() does NOT fall back to the built-in INSTANCE_TYPES once overridden "
      "(full replacement, not a merge)", any("ALIBABA_INSTANCE_TYPES" in m for m in died))


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all alibaba_backend checks passed")
