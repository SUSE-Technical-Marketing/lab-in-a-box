#!/usr/bin/env python3
# Unit tests for libs/backends.py's ExoscaleBackend — every `exo` CLI call
# (subprocess.run) is mocked (no real Exoscale account available anywhere
# in this environment, and no `exo` CLI needs to even be installed to run
# these). Asserts command construction, the simplified cloud-init-by-path
# delivery, config_method enforcement, and MAC handling — not real API
# behavior; see ExoscaleBackend's own docstring for exactly what remains
# unverified (the instance-type size table in particular). Run from
# 41_exoscale_backend.sh, in its own container — see tests/run_tests.sh.
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
    "EXOSCALE_API_KEY": "exokey", "EXOSCALE_API_SECRET": "exosecret", "EXOSCALE_ZONE": "ch-gva-2",
}


# ── resolve(): all three config keys are mandatory, individually ──────────
for missing_key in _FULL_CONFIG:
    partial = {k: v for k, v in _FULL_CONFIG.items() if k != missing_key}
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.ExoscaleBackend.resolve({}, "vm1", partial, False)
        except SystemExit:
            pass
    check("resolve() dies when {} is missing".format(missing_key), any(missing_key in m for m in died))

resolved = backends.ExoscaleBackend.resolve({}, "vm1", _FULL_CONFIG, False)
check("resolve() picks up all three required fields",
      (resolved.api_key, resolved.api_secret, resolved.zone) == ("exokey", "exosecret", "ch-gva-2"))


backend = backends.ExoscaleBackend("exokey", "exosecret", "ch-gva-2")


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("tpl-1", "vm1", 40, config_method="")
    except SystemExit:
        pass
check("copy_vm_image() dies on config_method != cloud-init", any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty ISO_IMAGE", any("ISO_IMAGE" in m for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("tpl-1", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real template ID with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes the real file PATH, not its content ─
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    userdata_file = cloud_init_dir / "vm1_user-data"
    userdata_file.write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.ExoscaleBackend("exokey", "exosecret", "ch-gva-2", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file path (exo reads it directly)",
          b2._userdata_path_by_vm.get("vm1") == str(userdata_file))

    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            b2.push_provisioning_files("vm-nofile", config_method="cloud-init")
        except SystemExit:
            pass
    check("push_provisioning_files() dies when the cloud-init file doesn't exist yet",
          any("not found" in m for m in died))


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on Exoscale ─
check("list_used_macs() returns empty (Exoscale has no MAC concept)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to Exoscale)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_instance_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_instance_type() picks the smallest sufficient SKU (1 vCPU / 1024 MiB)",
      backend._pick_instance_type(1, 1024, "vm1") == "standard.tiny")
check("_pick_instance_type() steps up when memory needs more than cores would suggest",
      backend._pick_instance_type(1, 16384, "vm1") == "standard.extra-large")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_instance_type(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_instance_type() dies clearly when nothing in the table is big enough",
      any("INSTANCE_TYPES" in m for m in died))


# ── vm_exists() / _find_instance(): exo compute instance list, name-matched ─
_list_one = _cp(0, stdout=json.dumps([{"id": "i-1", "name": "vm1", "state": "running"}]))
_list_none = _cp(0, stdout=json.dumps([]))

with mock.patch.object(backends.subprocess, "run", return_value=_list_one) as m_run:
    check("vm_exists() returns True when the list contains a matching name", backend.vm_exists("vm1") is True)
    called_args = m_run.call_args[0][0]
    check("vm_exists() calls exo compute instance list, scoped to the configured zone",
          "compute" in called_args and "list" in called_args and "ch-gva-2" in called_args)

with mock.patch.object(backends.subprocess, "run", return_value=_list_none):
    check("vm_exists() returns False when nothing in the list matches", backend.vm_exists("vm1") is False)


# ── get_ip(): the "public-ip" field, None if unassigned/nonexistent ───────
_with_ip = _cp(0, stdout=json.dumps([{"id": "i-1", "name": "vm1", "public-ip": "203.0.113.71"}]))
_no_ip_yet = _cp(0, stdout=json.dumps([{"id": "i-1", "name": "vm1", "public-ip": None}]))

with mock.patch.object(backends.subprocess, "run", return_value=_with_ip):
    check("get_ip() returns the real public-ip", backend.get_ip("vm1") == "203.0.113.71")
with mock.patch.object(backends.subprocess, "run", return_value=_no_ip_yet):
    check("get_ip() returns None when no public-ip is assigned yet", backend.get_ip("vm1") is None)
with mock.patch.object(backends.subprocess, "run", return_value=_list_none):
    check("get_ip() returns None when the instance doesn't exist", backend.get_ip("vm1") is None)


# ── env vars: the real API key/secret are passed to the subprocess, not the OS env ─
with mock.patch.object(backends.subprocess, "run", return_value=_list_none) as m_run:
    backend.vm_exists("vm1")
    passed_env = m_run.call_args[1].get("env", {})
    check("the real EXOSCALE_API_KEY is passed via subprocess env", passed_env.get("EXOSCALE_API_KEY") == "exokey")
    check("the real EXOSCALE_API_SECRET is passed via subprocess env",
          passed_env.get("EXOSCALE_API_SECRET") == "exosecret")


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backends.subprocess, "run", return_value=_list_none) as m_run:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls list (no delete) when nothing exists", m_run.call_count == 1)


# ── create_vm(): real command construction, incl. the plain --cloud-init path ─
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    userdata_file = cloud_init_dir / "vm1_user-data"
    userdata_file.write_text("#cloud-config\n")

    b3 = backends.ExoscaleBackend("exokey", "exosecret", "ch-gva-2", lab_setup_path=tempfile_dir)
    b3.push_provisioning_files("vm1", config_method="cloud-init")
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        if "list" in args:
            # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
            # public-ip here so the poll succeeds on its first check, not a 180s timeout.
            return _cp(0, stdout=json.dumps([{"id": "i-1", "name": "vm1", "public-ip": "203.0.113.70"}]))
        return _cp(0, stdout="")

    with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
        returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="tpl-1")

    check("create_vm() returns the real IP once the instance is confirmed running", returned_ip == "203.0.113.70")
    create_call = next(c for c in calls if "create" in c)
    check("create_vm() sends the real template ID", "tpl-1" in create_call)
    check("create_vm() picks a real instance-type", "standard.medium" in create_call)
    check("create_vm() maps vm_dsk_gb directly via --disk-size",
          "--disk-size" in create_call and "40" in create_call)
    check("create_vm() points --cloud-init at the real file path directly (no stash/encode step)",
          "--cloud-init" in create_call and str(userdata_file) in create_call)
    check("create_vm() uses the real vm_name as the instance name", "vm1" in create_call)

    # ── cloud_instance_type: explicit override bypasses _pick_instance_type() entirely ──
    # No provider's sizing catalog is a hardcoded ceiling. See _parse_sku_table()'s docstring and
    # README's Compute backends
    # table.
    b5 = backends.ExoscaleBackend("exokey", "exosecret", "ch-gva-2", lab_setup_path=tempfile_dir)
    b5.push_provisioning_files("vm1", config_method="cloud-init")
    calls = []
    with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
        # cpu/mem here would normally pick "standard.medium" — cloud_instance_type must win.
        b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="tpl-1",
                      cloud_instance_type="standard.huge")
    create_call = next(c for c in calls if "create" in c)
    check("create_vm() uses cloud_instance_type verbatim, bypassing _pick_instance_type()",
          "standard.huge" in create_call and "standard.medium" not in create_call)


# ── EXOSCALE_INSTANCE_TYPES: resolve() parses the config override into instance_types ──
_cfg = dict(_FULL_CONFIG, EXOSCALE_INSTANCE_TYPES="tiny:1:2,huge:16:64")
resolved = backends.ExoscaleBackend.resolve({}, "vm1", _cfg, False)
check("resolve() parses EXOSCALE_INSTANCE_TYPES into resolved.instance_types",
      resolved.instance_types == [("tiny", 1, 2.0), ("huge", 16, 64.0)])

# ── _pick_instance_type(): an overridden table actually replaces INSTANCE_TYPES, not merges ──
b6 = backends.ExoscaleBackend("exokey", "exosecret", "ch-gva-2",
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
      "(full replacement, not a merge)", any("EXOSCALE_INSTANCE_TYPES" in m for m in died))


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all exoscale_backend checks passed")
