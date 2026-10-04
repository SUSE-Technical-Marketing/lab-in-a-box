#!/usr/bin/env python3
# Unit tests for libs/backends.py's UpCloudBackend — every HTTP call
# (urllib.request.urlopen) is mocked (no real UpCloud account available
# anywhere in this environment). Asserts request shapes (incl. HTTP Basic
# Auth), MAC/image handling, config_method enforcement, and plan sizing —
# not real API behavior; see UpCloudBackend's own docstring for exactly
# what remains unverified (the inline user_data field in particular). Run
# from 39_upcloud_backend.sh, in its own container — see tests/run_tests.sh.
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


class _FakeResponse(object):
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8") if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_FULL_CONFIG = {
    "UPCLOUD_USERNAME": "labuser", "UPCLOUD_PASSWORD": "labpass", "UPCLOUD_ZONE": "fi-hel1",
}


# ── resolve(): all three config keys are mandatory, individually ──────────
for missing_key in _FULL_CONFIG:
    partial = {k: v for k, v in _FULL_CONFIG.items() if k != missing_key}
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.UpCloudBackend.resolve({}, "vm1", partial, False)
        except SystemExit:
            pass
    check("resolve() dies when {} is missing".format(missing_key), any(missing_key in m for m in died))

resolved = backends.UpCloudBackend.resolve({}, "vm1", _FULL_CONFIG, False)
check("resolve() picks up all three required fields",
      (resolved.username, resolved.password, resolved.zone) == ("labuser", "labpass", "fi-hel1"))


backend = backends.UpCloudBackend("labuser", "labpass", "fi-hel1")


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
check("copy_vm_image() accepts a real storage/template UUID with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes the real file content ──────────────
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.UpCloudBackend("labuser", "labpass", "fi-hel1", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on UpCloud ─
check("list_used_macs() returns empty (UpCloud has no MAC concept)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to UpCloud)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_plan(): smallest plan that satisfies both cores and memory ──────
check("_pick_plan() picks the smallest sufficient plan (1 vCPU / 2048 MiB)",
      backend._pick_plan(1, 2048, "vm1") == "1xCPU-2GB")
check("_pick_plan() steps up when memory needs more than cores would suggest",
      backend._pick_plan(1, 16384, "vm1") == "6xCPU-16GB")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_plan(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_plan() dies clearly when nothing in the table is big enough",
      any("PLANS" in m for m in died))


# ── auth: every request carries the real HTTP Basic Auth header ───────────
expected_auth = "Basic " + base64.b64encode(b"labuser:labpass").decode("ascii")
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": {"server": []}})) as m_open:
    backend.vm_exists("vm1")
    sent_req = m_open.call_args[0][0]
    check("requests carry the real HTTP Basic Auth header",
          sent_req.headers.get("Authorization") == expected_auth)


# ── vm_exists() / _find_server(): client-side filter over GET /server ─────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse(
                            {"servers": {"server": [{"uuid": "u-1", "title": "vm1", "state": "started"}]}})) as m_open:
    check("vm_exists() returns True when the full list contains a matching title",
          backend.vm_exists("vm1") is True)
    called_url = m_open.call_args[0][0].full_url
    check("vm_exists() calls the plain /server list endpoint", called_url.endswith("/server"))

with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": {"server": []}})):
    check("vm_exists() returns False when nothing in the list matches", backend.vm_exists("vm1") is False)


# ── get_ip(): public access preferred, private fallback, None if unassigned ─
_with_public = _FakeResponse({"servers": {"server": [{"title": "vm1", "ip_addresses": {"ip_address": [
    {"access": "private", "address": "10.0.0.9"}, {"access": "public", "address": "203.0.113.51"}]}}]}})
_private_only = _FakeResponse({"servers": {"server": [{"title": "vm1", "ip_addresses": {"ip_address": [
    {"access": "private", "address": "10.0.0.9"}]}}]}})

with mock.patch.object(backends.urllib.request, "urlopen", return_value=_with_public):
    check("get_ip() prefers the public address when both are present", backend.get_ip("vm1") == "203.0.113.51")
with mock.patch.object(backends.urllib.request, "urlopen", return_value=_private_only):
    check("get_ip() falls back to the private address when no public one is present",
          backend.get_ip("vm1") == "10.0.0.9")
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": {"server": []}})):
    check("get_ip() returns None when the server doesn't exist", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the server is already gone ────────────────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": {"server": []}})) as m_open:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls the list check (no DELETE) when nothing exists",
          m_open.call_count == 1)


# ── create_vm(): real request body shape ───────────────────────────────────
b3 = backends.UpCloudBackend("labuser", "labpass", "fi-hel1")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"
captured = {}


def _fake_urlopen(req, timeout=30):
    if req.get_method() == "GET":
        # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
        # public IP here so the poll succeeds on its first check, not a 180s timeout.
        return _FakeResponse({"servers": {"server": [
            {"uuid": "u-1", "title": "vm1",
             "ip_addresses": {"ip_address": [{"access": "public", "address": "203.0.113.50"}]}}]}})
    captured["body"] = json.loads(req.data.decode("utf-8"))
    captured["headers"] = dict(req.headers)
    captured["url"] = req.full_url
    return _FakeResponse({"server": {"uuid": "u-1"}})


with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    returned_ip = b3.create_vm("vm1", 1, 2048, 40, None, config_method="cloud-init", iso_image="tpl-1")

check("create_vm() returns the real IP once the instance is confirmed running", returned_ip == "203.0.113.50")
server_body = captured["body"]["server"]
check("create_vm() sends the real title/hostname", server_body.get("title") == "vm1" and server_body.get("hostname") == "vm1")
check("create_vm() picks a real plan", server_body.get("plan") == "1xCPU-2GB")
check("create_vm() sends the stashed user_data", server_body.get("user_data") == "#cloud-config\n")
check("create_vm() sends the configured zone", server_body.get("zone") == "fi-hel1")
disk = server_body["storage_devices"]["storage_device"][0]
check("create_vm() clones from the real storage/template UUID", disk.get("storage") == "tpl-1")
check("create_vm() maps vm_dsk_gb directly to the storage device's size", disk.get("size") == 40)
check("create_vm() sends the real HTTP Basic Auth header",
      captured["headers"].get("Authorization") == expected_auth)


# ── cloud_instance_type: explicit override bypasses _pick_plan() entirely ─────
# No provider's sizing catalog is a hardcoded ceiling. See _parse_sku_table()'s docstring and
# README's Compute backends table.
b5 = backends.UpCloudBackend("labuser", "labpass", "fi-hel1")
b5._user_data_by_vm["vm1"] = ""
captured = {}
with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    # cpu/mem here would normally pick "1xCPU-2GB" — cloud_instance_type must win regardless.
    b5.create_vm("vm1", 1, 2048, 40, None, config_method="cloud-init", iso_image="tpl-1",
                  cloud_instance_type="8xCPU-32GB")
check("create_vm() uses cloud_instance_type verbatim, bypassing _pick_plan()",
      captured["body"]["server"].get("plan") == "8xCPU-32GB")


# ── UPCLOUD_PLANS: resolve() parses the config override into plans ────────────
_cfg = dict(_FULL_CONFIG, UPCLOUD_PLANS="tiny:1:2,huge:16:64")
resolved = backends.UpCloudBackend.resolve({}, "vm1", _cfg, False)
check("resolve() parses UPCLOUD_PLANS into resolved.plans",
      resolved.plans == [("tiny", 1, 2.0), ("huge", 16, 64.0)])

# ── _pick_plan(): an overridden table actually replaces PLANS, not merges ─────
b6 = backends.UpCloudBackend("labuser", "labpass", "fi-hel1",
                              plans=[("tiny", 1, 2.0), ("huge", 16, 64.0)])
check("_pick_plan() picks from the overridden table when plans is set",
      b6._pick_plan(1, 2048, "vm1") == "tiny")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        b6._pick_plan(32, 4096, "vm1")  # too big for this override's max (16 cores)
    except SystemExit:
        pass
check("_pick_plan() does NOT fall back to the built-in PLANS once overridden "
      "(full replacement, not a merge)", any("UPCLOUD_PLANS" in m for m in died))


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all upcloud_backend checks passed")
