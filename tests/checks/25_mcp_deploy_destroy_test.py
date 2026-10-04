#!/usr/bin/env python3
# Mocked unit tests for mcp_server.py's mutating tools. setup_lab()/
# destroy_lab()/destroy_vm() and primary's loaders are all monkeypatched —
# no real SSH/libvirt anywhere. Run from 25_mcp_deploy_destroy.sh, in its
# own container — see tests/run_tests.sh.
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
sys.modules["mcp"] = types.ModuleType("mcp")
sys.modules["mcp.server"] = types.ModuleType("mcp.server")
sys.modules["mcp.server.mcpserver"] = _fake_fastmcp_mod
sys.modules["mcp.server.transport_security"] = _fake_transport_security_mod

import mcp_server  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


_tmp_audit = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False).name
_cfg_patch = mock.patch.object(mcp_server, "_cfg", return_value={"MCP_AUDIT_LOG": _tmp_audit})
_load_lab_patch = mock.patch.object(
    mcp_server, "_load_lab", return_value=({"nodes": {}}, {}, {}))


def _reset():
    calls = {"setup_lab": [], "destroy_lab": [], "destroy_vm": []}
    p1 = mock.patch.object(mcp_server, "_setup_lab", side_effect=lambda *a, **kw: calls["setup_lab"].append((a, kw)))
    p2 = mock.patch.object(mcp_server, "_destroy_lab", side_effect=lambda *a, **kw: calls["destroy_lab"].append((a, kw)))
    p3 = mock.patch.object(mcp_server, "_destroy_vm", side_effect=lambda *a, **kw: calls["destroy_vm"].append((a, kw)))
    return calls, p1, p2, p3


# ── deploy_lab: correct confirm calls through with keep=True ──────────────────
calls, p1, p2, p3 = _reset()
with _cfg_patch, _load_lab_patch, p1, p2, p3:
    msg = mcp_server.deploy_lab("/tmp/mylab.json", confirm="mylab.json")
check("deploy_lab with a correct confirm calls _setup_lab with keep=True",
      len(calls["setup_lab"]) == 1 and calls["setup_lab"][0][1].get("keep") is True)
check("deploy_lab returns a confirmation message naming the lab", "mylab.json" in msg)

# ── deploy_lab: wrong confirm never reaches _setup_lab ────────────────────────
calls, p1, p2, p3 = _reset()
with _cfg_patch, _load_lab_patch, p1, p2, p3:
    raised = False
    try:
        mcp_server.deploy_lab("/tmp/mylab.json", confirm="nope")
    except ValueError:
        raised = True
check("deploy_lab with a wrong confirm raises", raised)
check("deploy_lab with a wrong confirm never calls _setup_lab at all", calls["setup_lab"] == [])

# ── rebuild_lab: correct confirm calls through with keep=False ────────────────
calls, p1, p2, p3 = _reset()
with _cfg_patch, _load_lab_patch, p1, p2, p3:
    mcp_server.rebuild_lab("/tmp/mylab.json", confirm="mylab.json")
check("rebuild_lab with a correct confirm calls _setup_lab with keep=False",
      len(calls["setup_lab"]) == 1 and calls["setup_lab"][0][1].get("keep") is False)

# ── rebuild_lab: wrong confirm never reaches _setup_lab ───────────────────────
calls, p1, p2, p3 = _reset()
with _cfg_patch, _load_lab_patch, p1, p2, p3:
    try:
        mcp_server.rebuild_lab("/tmp/mylab.json", confirm="mylab.json.bak")
    except ValueError:
        pass
check("rebuild_lab with a wrong confirm never calls _setup_lab", calls["setup_lab"] == [])

# ── destroy_lab_tool: correct/wrong confirm ────────────────────────────────────
calls, p1, p2, p3 = _reset()
with _cfg_patch, p1, p2, p3, \
     mock.patch.object(mcp_server.primary, "load_defaults", return_value={}), \
     mock.patch.object(mcp_server.primary, "load_config", return_value={}), \
     mock.patch.object(mcp_server.primary, "load_definition", return_value={"nodes": {}}):
    mcp_server.destroy_lab_tool("/tmp/mylab.json", confirm="mylab.json")
    check("destroy_lab_tool with a correct confirm calls _destroy_lab", len(calls["destroy_lab"]) == 1)

    calls["destroy_lab"] = []
    try:
        mcp_server.destroy_lab_tool("/tmp/mylab.json", confirm="wrong")
    except ValueError:
        pass
    check("destroy_lab_tool with a wrong confirm never calls _destroy_lab", calls["destroy_lab"] == [])

# ── destroy_vm_tool: target is "<vm_name>@<json basename>", correct/wrong ────
calls, p1, p2, p3 = _reset()
with _cfg_patch, p1, p2, p3, \
     mock.patch.object(mcp_server.primary, "load_defaults", return_value={}), \
     mock.patch.object(mcp_server.primary, "load_config", return_value={}), \
     mock.patch.object(mcp_server.primary, "load_definition", return_value={"nodes": {"vm1": {}}}):
    raised = False
    try:
        mcp_server.destroy_vm_tool("/tmp/mylab.json", "vm1", confirm="vm1")  # missing @mylab.json
    except ValueError:
        raised = True
    check("destroy_vm_tool requires the full '<vm_name>@<json basename>' target, not just vm_name", raised)
    check("destroy_vm_tool with an incomplete confirm never calls _destroy_vm", calls["destroy_vm"] == [])

    mcp_server.destroy_vm_tool("/tmp/mylab.json", "vm1", confirm="vm1@mylab.json")
    check("destroy_vm_tool with the correct full target confirm calls _destroy_vm",
          len(calls["destroy_vm"]) == 1)


# ── _run_mutation: die()'s SystemExit is contained, not left to propagate ────
# Regression test: SystemExit isn't a
# subclass of Exception, so it passed straight through the MCP framework's
# own error handling and crashed the whole ASGI server on a single expected
# failure (e.g. a normal die() deep in setup_lab.py's call chain) — not just
# failing that one call. Every mutating tool must convert it into a plain
# raised exception instead.
calls, p1, p2, p3 = _reset()
_die_patch = mock.patch.object(
    mcp_server, "_setup_lab", side_effect=lambda *a, **kw: (_ for _ in ()).throw(SystemExit(1)))
with _cfg_patch, _load_lab_patch, _die_patch, p2, p3:
    raised_type = None
    try:
        mcp_server.deploy_lab("/tmp/mylab.json", confirm="mylab.json")
    except SystemExit:
        raised_type = SystemExit
    except Exception as e:
        raised_type = type(e)
    check("deploy_lab: a SystemExit from _setup_lab is caught, never re-raised as SystemExit",
          raised_type is not None and raised_type is not SystemExit)

with open(_tmp_audit) as f:
    audit_lines = [json.loads(line) for line in f if line.strip()]
last_deploy_entries = [e for e in audit_lines if e["tool"] == "deploy_lab" and e["target"] == "mylab.json"]
check("deploy_lab: the SystemExit failure is still audited (not silently swallowed)",
      any("failed" in e["outcome"] for e in last_deploy_entries))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mcp_deploy_destroy checks passed")
