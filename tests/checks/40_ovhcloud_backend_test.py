#!/usr/bin/env python3
# Unit tests for OVHcloudBackend in libs/backends.py. Every HTTP call (urllib.request.urlopen) is mocked. The tests check the request
# signing, the /auth/time call, flavor-list sizing, the config_method rule and the MAC handling. They do not test real API behaviour.
# Run from 40_ovhcloud_backend.sh, in its own container (see tests/run_tests.sh).
import hashlib
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
        if isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode("utf-8") if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_FULL_CONFIG = {
    "OVH_APPLICATION_KEY": "appkey", "OVH_APPLICATION_SECRET": "appsecret",
    "OVH_CONSUMER_KEY": "consumerkey", "OVH_SERVICE_NAME": "proj-1", "OVH_REGION": "GRA7",
}


# ── resolve(): all five config keys are mandatory, individually ───────────
for missing_key in _FULL_CONFIG:
    partial = {k: v for k, v in _FULL_CONFIG.items() if k != missing_key}
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.OVHcloudBackend.resolve({}, "vm1", partial, False)
        except SystemExit:
            pass
    check("resolve() dies when {} is missing".format(missing_key), any(missing_key in m for m in died))

resolved = backends.OVHcloudBackend.resolve({}, "vm1", _FULL_CONFIG, False)
check("resolve() picks up all five required fields",
      (resolved.application_key, resolved.application_secret, resolved.consumer_key,
       resolved.service_name, resolved.region) ==
      ("appkey", "appsecret", "consumerkey", "proj-1", "GRA7"))
check("resolve() defaults the endpoint to the EU root", resolved.endpoint == "https://eu.api.ovh.com/1.0")

resolved2 = backends.OVHcloudBackend.resolve(
    {}, "vm1", dict(_FULL_CONFIG, OVH_ENDPOINT="https://ca.api.ovh.com/1.0"), False)
check("resolve() picks up a non-default endpoint", resolved2.endpoint == "https://ca.api.ovh.com/1.0")


backend = backends.OVHcloudBackend("appkey", "appsecret", "consumerkey", "proj-1", "GRA7")


# ── _sign(): the real, documented OVH signature algorithm ─────────────────
expected = "$1$" + hashlib.sha1(
    "appsecret+consumerkey+GET+https://eu.api.ovh.com/1.0/cloud/project/proj-1/instance+".encode("utf-8")
    + b"+1700000000"
).hexdigest()
# build the exact same "+".join() the implementation uses, to avoid a hand-rolled mismatch
to_sign = "+".join(["appsecret", "consumerkey", "GET",
                     "https://eu.api.ovh.com/1.0/cloud/project/proj-1/instance", "", "1700000000"])
expected = "$1$" + hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
actual = backend._sign("GET", "https://eu.api.ovh.com/1.0/cloud/project/proj-1/instance", "", 1700000000)
check("_sign() implements the real $1$+SHA1(secret+consumer+method+url+body+timestamp) scheme",
      actual == expected)
check("_sign() output starts with the real OVH '$1$' version prefix", actual.startswith("$1$"))


# ── _server_timestamp(): pulled from OVH's own /auth/time endpoint ────────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse(b"1700000123")) as m_open:
    ts = backend._server_timestamp()
    check("_server_timestamp() returns the real integer from /auth/time", ts == 1700000123)
    check("_server_timestamp() calls the unauthenticated /auth/time endpoint",
          m_open.call_args[0][0].full_url.endswith("/auth/time"))

with mock.patch.object(backends.urllib.request, "urlopen", side_effect=backends.urllib.error.URLError("down")):
    ts = backend._server_timestamp()
    check("_server_timestamp() falls back to local time when /auth/time is unreachable",
          isinstance(ts, int) and ts > 0)


# ── every real API call carries the real signature headers ────────────────
with mock.patch.object(backend, "_server_timestamp", return_value=1700000000):
    with mock.patch.object(backends.urllib.request, "urlopen",
                            return_value=_FakeResponse([])) as m_open:
        backend.vm_exists("vm1")
        sent_req = m_open.call_args[0][0]
        check("requests carry X-Ovh-Application", sent_req.headers.get("X-ovh-application") == "appkey")
        check("requests carry X-Ovh-Consumer", sent_req.headers.get("X-ovh-consumer") == "consumerkey")
        check("requests carry X-Ovh-Timestamp", sent_req.headers.get("X-ovh-timestamp") == "1700000000")
        check("requests carry a real X-Ovh-Signature", sent_req.headers.get("X-ovh-signature", "").startswith("$1$"))


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("img-uuid", "vm1", 40, config_method="")
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
    backend.copy_vm_image("img-uuid", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real imageId with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes the real file content ──────────────
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.OVHcloudBackend("appkey", "appsecret", "consumerkey", "proj-1", "GRA7",
                                   lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on OVHcloud ─
check("list_used_macs() returns empty (OVHcloud has no MAC concept)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): a cloud backend has no MAC concept, so the value is passed through unchanged.
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to OVHcloud)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_flavor(): no static table — a real flavor-list call, smallest match ─
_flavors = [
    {"id": "flavor-small", "vcpus": 1, "ram": 2048},
    {"id": "flavor-medium", "vcpus": 2, "ram": 4096},
    {"id": "flavor-large", "vcpus": 4, "ram": 16384},
]
with mock.patch.object(backend, "_api", return_value=_flavors) as m_api:
    check("_pick_flavor() picks the smallest sufficient flavor (2 vCPU / 4096 MiB)",
          backend._pick_flavor(2, 4096, "vm1") == "flavor-medium")
    call_path = m_api.call_args[0][1]
    check("_pick_flavor() lists flavors scoped to the configured region",
          "flavor?region=GRA7" in call_path)

with mock.patch.object(backend, "_api", return_value=_flavors):
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            backend._pick_flavor(999, 999999, "vm1")
        except SystemExit:
            pass
    check("_pick_flavor() dies clearly when no flavor in the region is big enough",
          any("no flavor" in m.lower() for m in died))


# ── vm_exists() / _find_instance(): name-matched over the full instance list ─
with mock.patch.object(backend, "_api",
                        return_value=[{"id": "i-1", "name": "vm1", "status": "ACTIVE"}]) as m_api:
    check("vm_exists() returns True when the instance list contains a matching name",
          backend.vm_exists("vm1") is True)
    check("vm_exists() lists instances under the real service/project",
          "instance" in m_api.call_args[0][1] and "proj-1" in m_api.call_args[0][1])

with mock.patch.object(backend, "_api", return_value=[]):
    check("vm_exists() returns False when nothing in the list matches", backend.vm_exists("vm1") is False)


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backend, "_api", return_value=[]) as m_api:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls the list check (no DELETE) when nothing exists",
          m_api.call_count == 1)


# ── create_vm(): real request body shape ───────────────────────────────────
b3 = backends.OVHcloudBackend("appkey", "appsecret", "consumerkey", "proj-1", "GRA7")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"

def _fake_api(method, path, body=None):
    if method == "POST":
        return {"id": "i-1"}
    # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
    # public IPv4 here so the poll succeeds on its first check, not a 180s timeout.
    return [{"id": "i-1", "name": "vm1", "ipAddresses": [{"ip": "203.0.113.60", "type": "public", "version": 4}]}]


with mock.patch.object(b3, "_pick_flavor", return_value="flavor-medium") as m_pick:
    with mock.patch.object(b3, "_api", side_effect=_fake_api) as m_api:
        returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="img-uuid")
    check("create_vm() returns the real IP once the instance is confirmed running (2026-09-09 "
          "contract)", returned_ip == "203.0.113.60")
    check("create_vm() sizes via _pick_flavor(), not a static table", m_pick.called)
    create_call = next(c for c in m_api.call_args_list if c[0][0] == "POST")
    check("create_vm() POSTs to the real instance-create endpoint",
          "proj-1/instance" in create_call[0][1])
    body = create_call[0][2]
    check("create_vm() sends the real imageId", body.get("imageId") == "img-uuid")
    check("create_vm() sends the picked flavorId", body.get("flavorId") == "flavor-medium")
    check("create_vm() sends the real region", body.get("region") == "GRA7")
    check("create_vm() sends the stashed userData", body.get("userData") == "#cloud-config\n")


# ── cloud_instance_type: explicit override skips the flavor API call ─────────
# OVHcloud flavors come from a live API call. cloud_instance_type is the only override, and it skips that call.
b5 = backends.OVHcloudBackend("appkey", "appsecret", "consumerkey", "proj-1", "GRA7")
b5._user_data_by_vm["vm1"] = ""
with mock.patch.object(b5, "_pick_flavor", return_value="flavor-medium") as m_pick2:
    with mock.patch.object(b5, "_api", side_effect=_fake_api) as m_api2:
        b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="img-uuid",
                      cloud_instance_type="flavor-huge")
    check("create_vm() skips _pick_flavor() entirely when cloud_instance_type is given",
          not m_pick2.called)
    create_call = next(c for c in m_api2.call_args_list if c[0][0] == "POST")
    check("create_vm() uses cloud_instance_type verbatim as the flavorId",
          create_call[0][2].get("flavorId") == "flavor-huge")


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ovhcloud_backend checks passed")
