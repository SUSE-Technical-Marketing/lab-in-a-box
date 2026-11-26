#!/usr/bin/env python3
# Unit tests for libs/backends.py's GCPBackend — every `gcloud` CLI call
# (subprocess.run) is mocked (no real GCP project available anywhere in this
# environment, and no `gcloud` CLI needs to even be installed to run these).
# Asserts command construction, the custom-machine-type rounding rules,
# config_method enforcement, and MAC handling — not real API behavior; see
# GCPBackend's own docstring for exactly what remains unverified. Run from
# 36_gcp_backend.sh, in its own container — see tests/run_tests.sh.
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


# ── resolve(): GCP_PROJECT + GCP_ZONE are mandatory; the service-account
#    activation call is optional but attempted when a key path is given ──
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.GCPBackend.resolve({}, "vm1", {}, False)
    except SystemExit:
        pass
check("resolve() dies without GCP_PROJECT/GCP_ZONE", any("GCP_PROJECT" in m for m in died))

with mock.patch.object(backends.subprocess, "run", return_value=_cp(0)) as m_run:
    resolved = backends.GCPBackend.resolve(
        {}, "vm1", {"GCP_PROJECT": "labproj", "GCP_ZONE": "us-central1-a",
                     "GCP_SERVICE_ACCOUNT_KEY": "/etc/gcp/key.json"}, False)
    check("resolve() activates the service account when a key path is given", m_run.call_count == 1)
    check("resolve() passes the real key-file path", "/etc/gcp/key.json" in m_run.call_args[0][0])
check("resolve() picks up project/zone", (resolved.project, resolved.zone) == ("labproj", "us-central1-a"))

with mock.patch.object(backends.subprocess, "run") as m_run:
    backends.GCPBackend.resolve({}, "vm1", {"GCP_PROJECT": "labproj", "GCP_ZONE": "us-central1-a"}, False)
    check("resolve() does NOT try to activate a service account when no key path is configured",
          m_run.call_count == 0)

resolved = backends.GCPBackend.resolve(
    {}, "vm1", {"GCP_PROJECT": "labproj", "GCP_ZONE": "us-central1-a",
                 "GCP_IMAGE_PROJECT": "debian-cloud", "GCP_NETWORK": "labnet", "GCP_SUBNET": "labsub"}, False)
check("resolve() picks up optional image-project/network/subnet fields",
      (resolved.image_project, resolved.network, resolved.subnet) == ("debian-cloud", "labnet", "labsub"))


backend = backends.GCPBackend("labproj", "us-central1-a")


# ── _normalize_custom_shape(): GCE's own 256MB / even-vCPU rounding rules ──
check("_normalize_custom_shape() leaves an already-valid shape untouched",
      backend._normalize_custom_shape(2, 4096) == (2, 4096))
check("_normalize_custom_shape() rounds memory UP to the next 256MB multiple",
      backend._normalize_custom_shape(2, 4000) == (2, 4096))
check("_normalize_custom_shape() rounds an odd vCPU count UP to the next even number",
      backend._normalize_custom_shape(3, 4096) == (4, 4096))
check("_normalize_custom_shape() leaves a single vCPU alone (1 is valid, doesn't round to 2)",
      backend._normalize_custom_shape(1, 4096) == (1, 4096))


# ── config_method / SOURCE_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("debian-12", "vm1", 40, config_method="")
    except SystemExit:
        pass
check("copy_vm_image() dies on config_method != cloud-init", any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty SOURCE_IMAGE", any("image" in m.lower() for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("debian-12", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real image name with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): validates the file exists, stashes nothing ──
with tempfile.TemporaryDirectory() as tempfile_dir:
    b2 = backends.GCPBackend("labproj", "us-central1-a", lab_setup_path=tempfile_dir)
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            b2.push_provisioning_files("vm1", config_method="cloud-init")
        except SystemExit:
            pass
    check("push_provisioning_files() dies when the cloud-init file doesn't exist yet",
          any("not found" in m for m in died))

    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\n")
    ok = [False]
    with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
        b2.push_provisioning_files("vm1", config_method="cloud-init")
        ok[0] = True
    check("push_provisioning_files() succeeds once the real file exists", ok[0])


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on GCE ─────
check("list_used_macs() returns empty (GCE has no MAC concept this backend uses)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to GCP)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── vm_exists() / _find_instance(): instances list --filter=name=<vm> ─────
with mock.patch.object(backends.subprocess, "run",
                        return_value=_cp(0, stdout=json.dumps([{"name": "vm1", "status": "RUNNING"}]))) as m_run:
    check("vm_exists() returns True when gcloud lists a match", backend.vm_exists("vm1") is True)
    called_args = m_run.call_args[0][0]
    check("vm_exists() filters by name", any("name=vm1" in a for a in called_args))

with mock.patch.object(backends.subprocess, "run", return_value=_cp(0, stdout=json.dumps([]))):
    check("vm_exists() returns False when gcloud lists nothing", backend.vm_exists("vm1") is False)


# ── get_ip(): natIP preferred, networkIP fallback, None if unassigned/nonexistent ──
_with_nat = _cp(0, stdout=json.dumps([{"name": "vm1", "networkInterfaces": [
    {"accessConfigs": [{"natIP": "203.0.113.21"}], "networkIP": "10.0.0.7"}]}]))
_internal_only = _cp(0, stdout=json.dumps([{"name": "vm1", "networkInterfaces": [
    {"accessConfigs": [], "networkIP": "10.0.0.7"}]}]))
_no_nics = _cp(0, stdout=json.dumps([{"name": "vm1", "networkInterfaces": []}]))

with mock.patch.object(backends.subprocess, "run", return_value=_with_nat):
    check("get_ip() prefers the external natIP when assigned", backend.get_ip("vm1") == "203.0.113.21")
with mock.patch.object(backends.subprocess, "run", return_value=_internal_only):
    check("get_ip() falls back to the internal networkIP when no accessConfig is present",
          backend.get_ip("vm1") == "10.0.0.7")
with mock.patch.object(backends.subprocess, "run", return_value=_no_nics):
    check("get_ip() returns None when the instance has no network interfaces yet",
          backend.get_ip("vm1") is None)
with mock.patch.object(backends.subprocess, "run", return_value=_cp(0, stdout=json.dumps([]))):
    check("get_ip() returns None when the instance doesn't exist", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backends.subprocess, "run", return_value=_cp(0, stdout=json.dumps([]))) as m_run:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() deletes no instance when nothing exists (it releases this VM's reserved address first)",
          not any(c[0][0][1:4] == ["compute", "instances", "delete"] for c in m_run.call_args_list))
    check("delete_vm() releases this VM's reserved address even when the instance is already gone, since a "
          "reserved address no instance uses is still billed",
          any(c[0][0][1:4] == ["compute", "addresses", "delete"] for c in m_run.call_args_list))


# ── create_vm(): real command construction, incl. the metadata-from-file path ─
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    userdata_file = cloud_init_dir / "vm1_user-data"
    userdata_file.write_text("#cloud-config\n")

    b3 = backends.GCPBackend("labproj", "us-central1-a", lab_setup_path=tempfile_dir)
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        if "list" in args:
            # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
            # natIP here so the poll succeeds on its first check, not a 180s timeout.
            return _cp(0, stdout=json.dumps([{
                "name": "vm1",
                "networkInterfaces": [{"accessConfigs": [{"natIP": "203.0.113.20"}], "networkIP": "10.0.0.6"}],
            }]))
        return _cp(0, stdout="")

    with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
        returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="debian-12")

    check("create_vm() returns the real IP once the instance is confirmed running", returned_ip == "203.0.113.20")
    create_call = next(c for c in calls if "create" in c)
    check("create_vm() builds a genuine custom machine type from vm_cpu/vm_mem",
          "e2-custom-2-4096" in create_call)
    check("create_vm() sends the real image name", "debian-12" in create_call)
    check("create_vm() maps vm_dsk_gb directly to --boot-disk-size", "40GB" in create_call)
    check("create_vm() points metadata-from-file at the real cloud-init file path",
          any("user-data={}".format(userdata_file) in a for a in create_call))
    check("create_vm() omits image-project/network/subnet flags when none were configured",
          "--image-project" not in create_call and "--network" not in create_call)

    b4 = backends.GCPBackend("labproj", "us-central1-a", image_project="debian-cloud",
                              network="labnet", subnet="labsub", lab_setup_path=tempfile_dir)
    calls = []
    with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
        b4.create_vm("vm1", 3, 4000, 40, None, config_method="cloud-init", iso_image="debian-12")
    create_call = next(c for c in calls if "create" in c)
    check("create_vm() rounds an odd vCPU + non-256-multiple memory before building the machine type",
          "e2-custom-4-4096" in create_call)
    check("create_vm() includes image-project/network/subnet when configured",
          "debian-cloud" in create_call and "labnet" in create_call and "labsub" in create_call)

    # ── cloud_instance_type: explicit override skips the custom-shape building entirely ──
    # GCP has no static sizing table to override via
    # config (its custom machine types are built live from vm_cpu/vm_mem), so cloud_instance_type
    # is the only override mechanism for this backend. See README's Compute backends table.
    b5 = backends.GCPBackend("labproj", "us-central1-a", lab_setup_path=tempfile_dir)
    calls = []
    with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
        # cpu/mem here would normally build "e2-custom-2-4096" — cloud_instance_type must win.
        b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="debian-12",
                      cloud_instance_type="n2-standard-8")
    create_call = next(c for c in calls if "create" in c)
    check("create_vm() uses cloud_instance_type verbatim, skipping the custom-shape build",
          "n2-standard-8" in create_call and "e2-custom-2-4096" not in create_call)


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


# ── ensure_stable_address() / release_stable_address(): promote in place ────
# Compute Engine can reserve the very address an instance already answers on, so nothing that already resolves
# to it changes. Only when a reservation exists holding some OTHER address does the instance have to be moved.
def _fake_run_addr(describe, calls, instance=None):
    def _run(args, **kwargs):
        calls.append(args)
        if "addresses" in args and "describe" in args:
            return _cp(1, stderr="was not found") if describe is None else _cp(0, stdout=json.dumps(describe))
        if "instances" in args and "list" in args:
            return _cp(0, stdout=json.dumps([instance] if instance else []))
        return _cp(0, stdout="{}")
    return _run


addr_calls = []
b_addr = backends.GCPBackend("labproj", "us-central1-a")
_INSTANCE = {"networkInterfaces": [{"accessConfigs": [{"name": "External NAT", "natIP": "203.0.113.5"}]}]}
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_addr(None, addr_calls, _INSTANCE)):
    addr = b_addr.ensure_stable_address("vm1")
create_call = next((c for c in addr_calls if "addresses" in c and "create" in c), [])
check("ensure_stable_address() reserves the address the instance already answers on, rather than a new one",
      "--addresses" in create_call and "203.0.113.5" in create_call)
check("ensure_stable_address() reserves it in the region the zone belongs to",
      "--region" in create_call and "us-central1" in create_call)
check("ensure_stable_address() returns that same address, so nothing resolving to it has to change",
      addr == "203.0.113.5")
check("ensure_stable_address() leaves the instance's access config alone when the address does not change",
      not any("add-access-config" in c for c in addr_calls))

addr_calls = []
with mock.patch.object(backends.subprocess, "run",
                       side_effect=_fake_run_addr({"address": "203.0.113.5"}, addr_calls, _INSTANCE)):
    addr = b_addr.ensure_stable_address("vm1")
check("ensure_stable_address() is idempotent: an existing reservation holding the instance's own address is "
      "returned without creating another", addr == "203.0.113.5"
      and not any("addresses" in c and "create" in c for c in addr_calls))

addr_calls = []
with mock.patch.object(backends.subprocess, "run",
                       side_effect=_fake_run_addr({"address": "198.51.100.9"}, addr_calls, _INSTANCE)):
    addr = b_addr.ensure_stable_address("vm1")
add_call = next((c for c in addr_calls if "add-access-config" in c), [])
check("ensure_stable_address() moves the instance onto a reservation that holds another address, which needs "
      "its access config replaced", any("delete-access-config" in c for c in addr_calls)
      and "--address" in add_call and "198.51.100.9" in add_call)
check("ensure_stable_address() reuses the access config's own name rather than assuming one",
      "External NAT" in add_call)
check("ensure_stable_address() returns the reserved address it moved the instance onto", addr == "198.51.100.9")

addr_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_addr(None, addr_calls, _INSTANCE)):
    b_addr.release_stable_address("vm1")
check("release_stable_address() deletes the reserved address, which is billed while it is held",
      any(c[1:4] == ("compute", "addresses", "delete") or list(c[1:4]) == ["compute", "addresses", "delete"]
          for c in [tuple(x) for x in addr_calls]))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all gcp_backend checks passed")
