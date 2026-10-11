#!/usr/bin/env python3
# Mocked unit tests for mcp/mcp_server.py's tool
# registration gating, confirm/audit logic, and read-only-tool dispatch.
# The real `mcp` package (pip) isn't installed in this test image — a
# minimal fake mcp.server.mcpserver.MCPServer is injected into sys.modules
# before importing mcp_server, providing just enough surface (.tool()
# decorator, .add_tool(), .streamable_http_app()) for these unit tests;
# real MCP-protocol interop is explicitly NOT covered here (needs a real
# client, see mcp_server.py's own module docstring). Run from
# 24_mcp_server.sh, in its own container — see tests/run_tests.sh.
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))
sys.path.insert(0, str(_REPO / "mcp"))


class _FakeMCPServer:
    def __init__(self, name):
        self.name = name
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def add_tool(self, fn, name=None):
        self.tools[name or fn.__name__] = fn

    def streamable_http_app(self, transport_security=None):
        return "FAKE_ASGI_APP"


class _FakeTransportSecuritySettings:
    def __init__(self, enable_dns_rebinding_protection=True, **kw):
        self.enable_dns_rebinding_protection = enable_dns_rebinding_protection


_fake_fastmcp_mod = types.ModuleType("mcp.server.mcpserver")
_fake_fastmcp_mod.MCPServer = _FakeMCPServer
_fake_transport_security_mod = types.ModuleType("mcp.server.transport_security")
_fake_transport_security_mod.TransportSecuritySettings = _FakeTransportSecuritySettings
_fake_mcp_pkg = types.ModuleType("mcp")
_fake_mcp_server_pkg = types.ModuleType("mcp.server")
sys.modules["mcp"] = _fake_mcp_pkg
sys.modules["mcp.server"] = _fake_mcp_server_pkg
sys.modules["mcp.server.mcpserver"] = _fake_fastmcp_mod
sys.modules["mcp.server.transport_security"] = _fake_transport_security_mod

import mcp_server  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── read-only tools are always registered ─────────────────────────────────────
check("read-only tools are registered on the module-level mcp instance", set(mcp_server.mcp.tools) == {
    "list_components", "get_schema", "get_base_schema", "get_status", "validate_lab", "save_lab",
})


# ── register_mutating_tools(): gated on MCP_ALLOW_MUTATIONS ──────────────────
server = _FakeMCPServer("test")
registered = mcp_server.register_mutating_tools(server=server, cfg={"MCP_ALLOW_MUTATIONS": ""})
check("register_mutating_tools returns False and registers nothing when unset",
      registered is False and server.tools == {})

server = _FakeMCPServer("test")
registered = mcp_server.register_mutating_tools(server=server, cfg={"MCP_ALLOW_MUTATIONS": "false"})
check("register_mutating_tools returns False for an explicit 'false'", registered is False)

server = _FakeMCPServer("test")
registered = mcp_server.register_mutating_tools(server=server, cfg={"MCP_ALLOW_MUTATIONS": "true"})
check("register_mutating_tools returns True and registers all four mutating tools when enabled",
      registered is True and set(server.tools) == {"deploy_lab", "rebuild_lab", "destroy_lab", "destroy_vm"})


# ── _require_confirm(): exact target match only ───────────────────────────────
_tmp_audit = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False).name
with mock.patch.object(mcp_server, "_cfg", return_value={"MCP_AUDIT_LOG": _tmp_audit}):
    raised = False
    try:
        mcp_server._require_confirm("deploy_lab", "yes", "mylab.json", {"json_file": "mylab.json"})
    except ValueError:
        raised = True
    check("_require_confirm rejects a generic 'yes' instead of the real target", raised)

    raised = False
    try:
        mcp_server._require_confirm("deploy_lab", "MyLab.json", "mylab.json", {})
    except ValueError:
        raised = True
    check("_require_confirm is case-sensitive (rejects a near-miss)", raised)

    try:
        mcp_server._require_confirm("deploy_lab", "mylab.json", "mylab.json", {})
        ok = True
    except ValueError:
        ok = False
    check("_require_confirm accepts an exact match", ok)

    # _require_confirm itself only audits REJECTIONS (2 above) — an accepted
    # confirm is audited by the calling tool afterwards, once the real work
    # it gates has actually happened (see 25_mcp_deploy_destroy_test.py),
    # not by _require_confirm itself.
    entries = [json.loads(line) for line in Path(_tmp_audit).read_text().splitlines()]
    check("every rejected confirm is audited", len(entries) == 2)
    check("a rejected call is audited with accepted=False and no 'ok' outcome",
          all(e["accepted"] is False and e["outcome"] != "ok" for e in entries))


# ── read-only tools call through to api.dispatch() with the right args ───────
with mock.patch.object(mcp_server.api, "dispatch", return_value=(200, {"ok": True})) as d:
    out = mcp_server.get_schema("install_mariadb")
    check("get_schema calls api.dispatch('schema', 'GET', {'name': [...]})",
          d.call_args[0][:2] == ("schema", "GET") and d.call_args[0][2] == {"name": ["install_mariadb"]})
    check("get_schema returns dispatch's obj on success", out == {"ok": True})

with mock.patch.object(mcp_server.api, "dispatch", return_value=(404, {"error": "nope"})):
    raised = False
    try:
        mcp_server.get_schema("install_bogus")
    except ValueError:
        raised = True
    check("a dispatch error status (>=400) is surfaced as a ValueError, not returned silently", raised)

with mock.patch.object(mcp_server.api, "dispatch", return_value=(200, {"count": 0, "components": []})) as d:
    mcp_server.list_components()
    check("list_components calls api.dispatch('components', 'GET')", d.call_args[0][:2] == ("components", "GET"))



# ── _cfg() caching and _load_lab() preflight ───────────────────────────────────────────────────────────────────────
with mock.patch.object(mcp_server, "_cfg_cache", None), \
        mock.patch.object(mcp_server.primary, "load_config", return_value={"MCP_PORT": "1"}) as lc_cfg:
    first, second = mcp_server._cfg(), mcp_server._cfg()
check("_cfg: loads lab_creation.cfg once and caches it", first == second == {"MCP_PORT": "1"} and lc_cfg.call_count == 1)

with mock.patch.object(mcp_server.primary, "load_defaults", return_value={"ISO_LOC": "/iso", "VM_IMG_LOC": "/img/"}), \
        mock.patch.object(mcp_server.primary, "load_config", return_value={"K": "v"}), \
        mock.patch.object(mcp_server.primary, "load_definition", return_value={"nodes": {}}), \
        mock.patch.object(mcp_server.lc, "validate_lab_definition", return_value=True) as vld:
    loaded = mcp_server._load_lab("lab.json")
check("_load_lab: returns (definition, config, defaults)",
      loaded == ({"nodes": {}}, {"K": "v"}, {"ISO_LOC": "/iso", "VM_IMG_LOC": "/img/"}))
check("_load_lab: validates with the defaults' paths, VM_IMG_LOC without its trailing slash",
      vld.call_args[0][2:] == ("/iso", "/srv/www/htdocs/lab_creation") and vld.call_args[1] == {"vm_img_loc": "/img"})
with mock.patch.object(mcp_server.primary, "load_defaults", return_value={}), \
        mock.patch.object(mcp_server.primary, "load_config", return_value={}), \
        mock.patch.object(mcp_server.primary, "load_definition", return_value={}), \
        mock.patch.object(mcp_server.lc, "validate_lab_definition", return_value=False):
    try:
        mcp_server._load_lab("bad.json")
        raised = ""
    except ValueError as e:
        raised = str(e)
check("_load_lab: a failed preflight raises ValueError naming the file", "bad.json" in raised)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mcp_server checks passed")
