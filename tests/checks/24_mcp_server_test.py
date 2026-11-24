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
    def __init__(self, name, token_verifier=None, auth=None):
        self.name = name
        self.tools = {}
        self.token_verifier = token_verifier
        self.auth = auth

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


# The SDK's own auth surface mcp_server.py uses: a TokenVerifier returns an AccessToken, AuthSettings carries
# the issuer and required scopes, and get_access_token() is the contextvar accessor that makes the authenticated
# client readable anywhere in request handling. The fakes mirror the real shapes (mcp 2.1.1) closely enough to
# assert what mcp_server.py does with them; real MCP-protocol auth interop needs a real client.
class _FakeAccessToken:
    def __init__(self, token, client_id, scopes, **kw):
        self.token = token
        self.client_id = client_id
        self.scopes = scopes


class _FakeAuthSettings:
    def __init__(self, issuer_url=None, required_scopes=None, **kw):
        self.issuer_url = issuer_url
        self.required_scopes = required_scopes


_fake_current_token = {"token": None}


def _fake_get_access_token():
    return _fake_current_token["token"]


_fake_fastmcp_mod = types.ModuleType("mcp.server.mcpserver")
_fake_fastmcp_mod.MCPServer = _FakeMCPServer
_fake_transport_security_mod = types.ModuleType("mcp.server.transport_security")
_fake_transport_security_mod.TransportSecuritySettings = _FakeTransportSecuritySettings
_fake_provider_mod = types.ModuleType("mcp.server.auth.provider")
_fake_provider_mod.AccessToken = _FakeAccessToken
_fake_settings_mod = types.ModuleType("mcp.server.auth.settings")
_fake_settings_mod.AuthSettings = _FakeAuthSettings
_fake_auth_context_mod = types.ModuleType("mcp.server.auth.middleware.auth_context")
_fake_auth_context_mod.get_access_token = _fake_get_access_token
_fake_mcp_pkg = types.ModuleType("mcp")
_fake_mcp_server_pkg = types.ModuleType("mcp.server")
sys.modules["mcp"] = _fake_mcp_pkg
sys.modules["mcp.server"] = _fake_mcp_server_pkg
sys.modules["mcp.server.mcpserver"] = _fake_fastmcp_mod
sys.modules["mcp.server.transport_security"] = _fake_transport_security_mod
sys.modules["mcp.server.auth"] = types.ModuleType("mcp.server.auth")
sys.modules["mcp.server.auth.provider"] = _fake_provider_mod
sys.modules["mcp.server.auth.settings"] = _fake_settings_mod
sys.modules["mcp.server.auth.middleware"] = types.ModuleType("mcp.server.auth.middleware")
sys.modules["mcp.server.auth.middleware.auth_context"] = _fake_auth_context_mod

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
        mcp_server._require_confirm("deploy_lab", "yes", "mylab.json", {"lab_file": "mylab.json"})
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


# Importing the module must not need a configured host: the auth decision is made at construction time, from a
# config file that does not exist in this container. Reaching this line at all proves it, since the import above
# would otherwise have died.
check("mcp_server imports on a host with no /etc/lab_creation.cfg", mcp_server._startup_config() == {}
      or isinstance(mcp_server._startup_config(), dict))
check("with no configuration, no token is required and the endpoint behaves as before",
      mcp_server._auth is None and mcp_server._verifier is None)


# ── per-client authentication and authorization ─────────────────────────────
# mTLS says a client holds a certificate this CA signed, which is all it says: every cert is equivalent to every
# other, so nothing names the caller. A per-client bearer token over that same connection supplies identity and
# scope. Asserted here: what the registry parses to, what the verifier answers, what the write scope refuses,
# and that the audit log names the client.
import asyncio  # noqa: E402

_clients_dir = tempfile.mkdtemp()
_clients_file = Path(_clients_dir) / "clients.json"
_RO_TOKEN = "ro-token-value"
_RW_TOKEN = "rw-token-value"
_ro_digest = __import__("hashlib").sha256(_RO_TOKEN.encode()).hexdigest()
_rw_digest = __import__("hashlib").sha256(_RW_TOKEN.encode()).hexdigest()
_clients_file.write_text(json.dumps({"schema_version": "1.0", "clients": [
    {"name": "agent-ro", "token_sha256": _ro_digest, "scopes": ["read"]},
    {"name": "agent-rw", "token_sha256": _rw_digest, "scopes": ["read", "write"]},
    {"name": "", "token_sha256": "deadbeef", "scopes": ["read"]},
    {"name": "no-digest", "scopes": ["read"]},
]}))
_cfg_with_clients = {"MCP_CLIENTS": str(_clients_file)}

_loaded = mcp_server.load_clients(_cfg_with_clients)
check("load_clients: keyed by the token's SHA-256, not the token",
      _ro_digest in _loaded and _RO_TOKEN not in _loaded)
check("load_clients: carries the client's name and scopes",
      _loaded[_ro_digest] == {"name": "agent-ro", "scopes": ["read"]})
check("load_clients: an entry with no name is skipped", not any(e["name"] == "" for e in _loaded.values()))
check("load_clients: an entry with no token digest is skipped",
      not any(e["name"] == "no-digest" for e in _loaded.values()))
check("load_clients: two clients were read", len(_loaded) == 2)
check("load_clients: a registry that does not exist is empty, never an error",
      mcp_server.load_clients({"MCP_CLIENTS": str(Path(_clients_dir) / "absent.json")}) == {})
_bad = Path(_clients_dir) / "bad.json"
_bad.write_text("{not json")
check("load_clients: a malformed registry is empty, never an error",
      mcp_server.load_clients({"MCP_CLIENTS": str(_bad)}) == {})

_verify = mcp_server.ClientTokenVerifier(_cfg_with_clients).verify_token
_tok = asyncio.get_event_loop().run_until_complete(_verify(_RW_TOKEN))
check("verify_token: a registered token names its client", _tok is not None and _tok.client_id == "agent-rw")
check("verify_token: and carries its scopes", _tok.scopes == ["read", "write"])
check("verify_token: an unknown token is refused",
      asyncio.get_event_loop().run_until_complete(_verify("not-a-real-token")) is None)
check("verify_token: the stored digest is not accepted as the token",
      asyncio.get_event_loop().run_until_complete(_verify(_rw_digest)) is None)

# acting_client()/client_scopes() read the SDK's own contextvar, so a tool and the audit log see the same caller.
_fake_current_token["token"] = None
check("acting_client: no token means no named client", mcp_server.acting_client() == "-")
check("client_scopes: no token means no scopes", mcp_server.client_scopes() == [])
_fake_current_token["token"] = _FakeAccessToken(_RO_TOKEN, "agent-ro", ["read"])
check("acting_client: the authenticated client's name", mcp_server.acting_client() == "agent-ro")
check("client_scopes: the authenticated client's scopes", mcp_server.client_scopes() == ["read"])

# The audit log names the client, which is the whole point: mTLS could not.
_audit_file = Path(_clients_dir) / "audit.log"
with mock.patch.object(mcp_server, "_cfg", return_value={"MCP_AUDIT_LOG": str(_audit_file)}):
    mcp_server._audit("destroy_lab", {}, "lab.json", accepted=True, outcome="ok")
_line = json.loads(_audit_file.read_text().strip().split("\n")[-1])
check("_audit: the record names the client that asked", _line.get("client") == "agent-ro")

# The write scope: MCP_ALLOW_MUTATIONS says the SERVER may mutate, this says the CLIENT may.
_refusals = []
with mock.patch.object(mcp_server, "_audit", side_effect=lambda *a, **kw: _refusals.append((a, kw))):
    try:
        mcp_server._require_write_scope("destroy_lab", "lab.json", {})
        _denied = ""
    except ValueError as e:
        _denied = str(e)
check("a read-only client may not run a mutating tool", _denied != "")
check("the refusal names the client and the scope it lacks",
      "agent-ro" in _denied and "write" in _denied)
check("the refusal says how to grant it", "--allow-mutations" in _denied)
check("the refusal names the tool that was refused", "destroy_lab" in _denied)
check("the refusal is audited, so the attempt is visible",
      _refusals and _refusals[0][1].get("accepted") is False)

_fake_current_token["token"] = _FakeAccessToken(_RW_TOKEN, "agent-rw", ["read", "write"])
mcp_server._require_write_scope("destroy_lab", "lab.json", {})
check("a client with the write scope is allowed through", True)

_fake_current_token["token"] = None
mcp_server._require_write_scope("destroy_lab", "lab.json", {})
check("a server that requires no tokens is left to MCP_ALLOW_MUTATIONS alone, so a read-only "
      "deployment is unchanged", True)

# _run_mutation checks the scope BEFORE doing any work, so a refused call changes nothing.
_fake_current_token["token"] = _FakeAccessToken(_RO_TOKEN, "agent-ro", ["read"])
_ran = []
with mock.patch.object(mcp_server, "_audit", side_effect=lambda *a, **kw: None):
    try:
        mcp_server._run_mutation("destroy_lab", "lab.json", {}, lambda: _ran.append("did it"))
    except ValueError:
        pass
check("_run_mutation: a client without the write scope never reaches the work", _ran == [])
_fake_current_token["token"] = None

# ── when tokens are required at all ─────────────────────────────────────────
# Exactly when MCP_ALLOW_MUTATIONS is on: that is when a caller can destroy a lab. A read-only server keeps
# working on mTLS alone, so an existing deployment is unchanged until it enables mutations.
_v, _a = mcp_server._auth_components({"MCP_ALLOW_MUTATIONS": "false", "MCP_CLIENTS": str(_clients_file)})
check("mutations off: no token is required", (_v, _a) == (None, None))
_v, _a = mcp_server._auth_components({"MCP_ALLOW_MUTATIONS": "true",
                                      "MCP_CLIENTS": str(Path(_clients_dir) / "absent.json")})
check("mutations on with no registered client: the module still imports, main() refuses to start",
      (_v, _a) == (None, None))
_v, _a = mcp_server._auth_components({"MCP_ALLOW_MUTATIONS": "true", "MCP_CLIENTS": str(_clients_file),
                                      "MCP_PORT": "8843"})
check("mutations on with a registry: a verifier and auth settings are configured", _v is not None and _a is not None)
check("the route requires the read scope, so every call carries a token", _a.required_scopes == ["read"])
check("the issuer is this server's own HTTPS URL, never fetched by the SDK",
      str(_a.issuer_url).startswith("https://"))
_v, _a = mcp_server._auth_components({"MCP_ALLOW_MUTATIONS": "true", "MCP_CLIENTS": str(_clients_file),
                                      "MCP_ISSUER_URL": "https://mcp.example.com"})
check("the issuer can be set explicitly", str(_a.issuer_url) == "https://mcp.example.com")

# ── the issuing script stores no usable credential ──────────────────────────
_issue = (_REPO / "mcp" / "lab-mcp-issue-client-cert").read_text()
check("the issuing script grants the write scope only with --allow-mutations",
      '--allow-mutations' in _issue and '["read", "write"]' in _issue and '_scopes=\'["read"]\'' in _issue)
check("it stores a SHA-256, not the token", "openssl dgst -sha256" in _issue and "token_sha256" in _issue)
check("the registry is root-only", 'chmod 0600 "${_clients}"' in _issue)
check("reissuing a name replaces its token instead of leaving two that work",
      'c.get("name") != os.environ["MCP_NAME"]' in _issue)
check("it validates the client name rather than writing whatever it is given",
      "^[A-Za-z0-9][A-Za-z0-9._-]*$" in _issue)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mcp_server checks passed")
