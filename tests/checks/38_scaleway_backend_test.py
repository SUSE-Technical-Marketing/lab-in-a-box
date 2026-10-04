#!/usr/bin/env python3
# Unit tests for libs/backends.py's ScalewayBackend — every HTTP call
# (urllib.request.urlopen) is mocked (no real Scaleway account/project
# available anywhere in this environment). Asserts request shapes, MAC/image
# handling, config_method enforcement, server_type sizing, and the separate
# cloud-init PATCH call — not real API behavior; see ScalewayBackend's own
# docstring for exactly what remains unverified (the cloud-init PATCH
# mechanism in particular). Run from 38_scaleway_backend.sh, in its own
# container — see tests/run_tests.sh.
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
    "SCALEWAY_SECRET_KEY": "secretkey", "SCALEWAY_PROJECT_ID": "proj-1", "SCALEWAY_ZONE": "fr-par-1",
}


# ── resolve(): all three config keys are mandatory, individually ──────────
for missing_key in _FULL_CONFIG:
    partial = {k: v for k, v in _FULL_CONFIG.items() if k != missing_key}
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.ScalewayBackend.resolve({}, "vm1", partial, False)
        except SystemExit:
            pass
    check("resolve() dies when {} is missing".format(missing_key), any(missing_key in m for m in died))

resolved = backends.ScalewayBackend.resolve({}, "vm1", _FULL_CONFIG, False)
check("resolve() picks up all three required fields",
      (resolved.secret_key, resolved.project_id, resolved.zone) == ("secretkey", "proj-1", "fr-par-1"))


backend = backends.ScalewayBackend("secretkey", "proj-1", "fr-par-1")


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("img-1", "vm1", 40, config_method="")
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
    backend.copy_vm_image("img-1", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real image ID with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes the real file content ──────────────
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.ScalewayBackend("secretkey", "proj-1", "fr-par-1", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on Scaleway ─
check("list_used_macs() returns empty (Scaleway has no MAC concept)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to Scaleway)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_server_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_server_type() picks the smallest sufficient SKU (2 vCPU / 2048 MiB)",
      backend._pick_server_type(2, 2048, "vm1") == "DEV1-S")
check("_pick_server_type() steps up when memory needs more than cores would suggest",
      backend._pick_server_type(2, 16384, "vm1") == "GP1-S")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_server_type(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_server_type() dies clearly when nothing in the table is big enough",
      any("SERVER_TYPES" in m for m in died))


# ── vm_exists() / _find_server(): GET /servers?name=... ────────────────────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": [{"id": "s-1", "state": "running"}]})) as m_open:
    check("vm_exists() returns True when the API lists a matching server", backend.vm_exists("vm1") is True)
    called_url = m_open.call_args[0][0].full_url
    check("vm_exists() queries /servers?name=<vm>", "/servers?name=vm1" in called_url)
    check("vm_exists() scopes the request to the configured zone", "/zones/fr-par-1/" in called_url)

with mock.patch.object(backends.urllib.request, "urlopen", return_value=_FakeResponse({"servers": []})):
    check("vm_exists() returns False when the API lists no matching server", backend.vm_exists("vm1") is False)


# ── get_ip(): public_ip.address, None if unassigned/nonexistent ───────────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": [{"public_ip": {"address": "203.0.113.41"}}]})):
    check("get_ip() returns the real public IP", backend.get_ip("vm1") == "203.0.113.41")
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": [{"public_ip": None}]})):
    check("get_ip() returns None when no public IP is assigned yet", backend.get_ip("vm1") is None)
with mock.patch.object(backends.urllib.request, "urlopen", return_value=_FakeResponse({"servers": []})):
    check("get_ip() returns None when the server doesn't exist", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the server is already gone ────────────────
with mock.patch.object(backends.urllib.request, "urlopen", return_value=_FakeResponse({"servers": []})) as m_open:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls GET (no DELETE) when the server doesn't exist",
          m_open.call_count == 1)


# ── create_vm(): real request body shape + the separate cloud-init PATCH ──
b3 = backends.ScalewayBackend("secretkey", "proj-1", "fr-par-1")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"
requests_seen = []


def _fake_urlopen(req, timeout=30):
    requests_seen.append(req)
    if req.get_method() == "POST" and req.full_url.endswith("/servers"):
        return _FakeResponse({"server": {"id": "s-1"}})
    if req.get_method() == "PATCH":
        return _FakeResponse(None)
    if req.get_method() == "POST" and "/action" in req.full_url:
        return _FakeResponse(None)
    if req.get_method() == "GET" and "/servers?name=" in req.full_url:
        # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
        # public_ip here so the poll succeeds on its first check, not a 180s timeout.
        return _FakeResponse({"servers": [{"id": "s-1", "public_ip": {"address": "203.0.113.40"}}]})
    return _FakeResponse(None)


with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="img-1")

check("create_vm() returns the real IP once the instance is confirmed running", returned_ip == "203.0.113.40")
create_req = next(r for r in requests_seen if r.get_method() == "POST" and r.full_url.endswith("/servers"))
create_body = json.loads(create_req.data.decode("utf-8"))
check("create_vm() sends the real image ID", create_body.get("image") == "img-1")
check("create_vm() picks a real server_type", create_body.get("commercial_type") == "DEV1-M")
check("create_vm() sends the real project ID", create_body.get("project") == "proj-1")
check("create_vm() does NOT put user_data in the create body (it's a separate PATCH)",
      "user_data" not in create_body)
check("create_vm() sends the real X-Auth-Token header", create_req.headers.get("X-auth-token") == "secretkey")

patch_req = next((r for r in requests_seen if r.get_method() == "PATCH"), None)
check("create_vm() issues a separate PATCH to set cloud-init user_data", patch_req is not None)
if patch_req is not None:
    check("the PATCH targets the real server's user_data/cloud-init endpoint",
          "/servers/s-1/user_data/cloud-init" in patch_req.full_url)
    check("the PATCH sends the real stashed cloud-init content as the raw body",
          patch_req.data.decode("utf-8") == "#cloud-config\n")

poweron_req = next((r for r in requests_seen if r.get_method() == "POST" and "/action" in r.full_url), None)
check("create_vm() powers the server on after creation", poweron_req is not None)
if poweron_req is not None:
    check("the poweron action body is correct",
          json.loads(poweron_req.data.decode("utf-8")).get("action") == "poweron")


# ── cloud_instance_type: explicit override bypasses _pick_server_type() entirely ──
# No provider's sizing catalog is a hardcoded ceiling. See _parse_sku_table()'s docstring and
# README's Compute backends table.
b5 = backends.ScalewayBackend("secretkey", "proj-1", "fr-par-1")
b5._user_data_by_vm["vm1"] = ""
requests_seen = []
with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    # cpu/mem here would normally pick "DEV1-M" — cloud_instance_type must win regardless.
    b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="img-1",
                  cloud_instance_type="GP1-XL")
create_req = next(r for r in requests_seen if r.get_method() == "POST" and r.full_url.endswith("/servers"))
create_body = json.loads(create_req.data.decode("utf-8"))
check("create_vm() uses cloud_instance_type verbatim, bypassing _pick_server_type()",
      create_body.get("commercial_type") == "GP1-XL")


# ── SCALEWAY_SERVER_TYPES: resolve() parses the config override into server_types ──
_cfg = dict(_FULL_CONFIG, SCALEWAY_SERVER_TYPES="tiny:1:2,huge:16:64")
resolved = backends.ScalewayBackend.resolve({}, "vm1", _cfg, False)
check("resolve() parses SCALEWAY_SERVER_TYPES into resolved.server_types",
      resolved.server_types == [("tiny", 1, 2.0), ("huge", 16, 64.0)])

# ── _pick_server_type(): an overridden table actually replaces SERVER_TYPES, not merges ──
b6 = backends.ScalewayBackend("secretkey", "proj-1", "fr-par-1",
                               server_types=[("tiny", 1, 2.0), ("huge", 16, 64.0)])
check("_pick_server_type() picks from the overridden table when server_types is set",
      b6._pick_server_type(1, 2048, "vm1") == "tiny")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        b6._pick_server_type(32, 4096, "vm1")  # too big for this override's max (16 cores)
    except SystemExit:
        pass
check("_pick_server_type() does NOT fall back to the built-in SERVER_TYPES once overridden "
      "(full replacement, not a merge)", any("SCALEWAY_SERVER_TYPES" in m for m in died))


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all scaleway_backend checks passed")
