#!/usr/bin/env python3.11
# Part of lab-in-a-box — MCP (Model Context Protocol) endpoint.
# Author/s: Raul Mahiques
# License: GPLv3
"""
mcp_server.py — lets an MCP client (an LLM agent) drive lab-in-a-box.

Runs as its own root-owned service DIRECTLY on the automation VM (a plain
systemd unit / lab-mcp-ctl-managed process — see
install_automation_node_scripts.sh), NOT containerized, and separate from
the webui's Apache-user CGI (webui/cgi-bin/labbuilder.py's own header
comment is explicit that it "never builds labs or runs anything
privileged" — this server does the opposite on purpose, so it must not
share that process/user). Containerizing it was the original design and
was tried first — reverted after live-testing (2026-08-29) showed its
mutating tools need full, unsandboxed host access that a container's
isolated namespace can't reasonably provide: virsh/virt-install (via
libvirt's qemu+ssh transport) and, more fundamentally, DNS record
management, which reads/writes BIND's local zone files and restarts the
LOCAL named service directly — there's no remote/SSH equivalent for that
since BIND already runs on this exact host. Its own third-party Python
deps (mcp/uvicorn, the only third-party deps anywhere in this otherwise
stdlib-only project) still live in a dedicated venv
(/etc/lab-mcp/venv), so they stay isolated from the rest of the system's
Python environment even without a container.

Read-only tools (list_components/get_schema/get_base_schema/get_status/
validate_lab/save_lab) are thin wrappers around webui/lib/api.py's
dispatch() — the exact same operation set the webui itself uses, reused not
reimplemented.

Mutating tools (deploy_lab/rebuild_lab/destroy_lab/destroy_vm) call
setup_lab.py/destroy_lab.py/destroy_vm.py's own already-importable functions
in-process — never a subprocess. They are only REGISTERED (visible to a
client at all) when /etc/lab_creation.cfg's MCP_ALLOW_MUTATIONS is true;
default is read-only-only. Every mutating call requires a `confirm` string
that must exactly equal the target identifier being acted on (not a plain
"yes"/true) — see _require_confirm() — and every call, accepted or
rejected, is appended to MCP_AUDIT_LOG.

deploy_lab vs. rebuild_lab: deploy_lab always runs with keep=True semantics
(never destroys/recreates an existing VM matching a node name). Only
rebuild_lab can destroy+recreate matching VMs — a separate, unmistakably-
named tool rather than a keep=False parameter on deploy_lab, so an agent has
to deliberately choose the destructive one.

Transport: MCP Python SDK v2's streamable_http_app(), served over mTLS via
uvicorn (ssl_cert_reqs=CERT_REQUIRED against a local CA — see
lab-mcp-issue-client-cert and install_automation_node_scripts.sh's cert
generation, which mirrors the existing self-signed-cert pattern already
used for the webui's own HTTPS). A request without a client cert signed by
that CA is rejected at the TLS layer, before any tool dispatch happens.

NOT verified against a real MCP client or a real lab — this needs a real
end-to-end round trip (a real MCP client over mTLS, a real deploy/destroy
against a disposable lab on nuc6.mydemo.lab) before being trusted in
production. Mocked tests cover the confirm-gate/audit-log/registration
logic and that the mutating tools call through with the exact args — not
protocol-level interop or real infrastructure.
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import socket
import ssl
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

# Bare-host production layout (see install_automation_node_scripts.sh) —
# the same locations every other deployed script/lib in this project
# already lives in, so this needs no LABBUILDER_*_DIR env overrides (unlike
# the containerized layout this replaced, which had to flatten webui/lib to
# stay one directory shallower). Falls back to the repo-relative layout for
# local dev/testing, same multi-candidate sys.path pattern every other
# script here already uses (production path first, repo-relative fallback
# second).
for _candidate in ("/usr/local/lib/lab_creation", str(_SCRIPT_DIR.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
for _candidate in ("/usr/local/bin", str(_SCRIPT_DIR.parent / "scripts")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
for _candidate in ("/srv/www/lab-builder/lib", str(_SCRIPT_DIR.parent / "webui" / "lib")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation as lc  # noqa: E402
import api  # noqa: E402  (webui/lib/api.py's dispatch())
from setup_lab import setup_lab as _setup_lab  # noqa: E402
from destroy_lab import destroy_lab as _destroy_lab  # noqa: E402
from destroy_vm import destroy_vm as _destroy_vm  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402  (pip install mcp)
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.server.auth.middleware.auth_context import get_access_token  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402

# ── which client is calling, and what it may do ───────────────────────────────
# mTLS says a client holds a certificate this host's CA signed, which is all it says: every client cert is
# equivalent to every other, so nothing distinguishes one caller from another and MCP_AUDIT_LOG can record what was
# done but not by whom. A per-client bearer token, carried over that same mTLS connection, supplies both. It is the
# SDK's own mechanism (MCPServer(token_verifier=..., auth=...)), so BearerAuthBackend reads the header,
# RequireAuthMiddleware enforces the route's scopes and get_access_token() makes the caller readable anywhere in
# request handling, including _audit(). Not a replacement for mTLS: a caller needs the certificate AND the token.
#
# The registry holds only a SHA-256 of each token, so the file cannot hand anyone a working credential.
MCP_CLIENTS_PATH = "/etc/lab-mcp/clients.json"

# What a client may do. "read" is every tool; "write" is additionally the mutating ones, and is granted only to a
# client issued with it. MCP_ALLOW_MUTATIONS stays the server-wide switch: both it and the client's own scope have
# to allow a mutation.
SCOPE_READ = "read"
SCOPE_WRITE = "write"


def clients_path(cfg=None):
    """Where the client registry lives: MCP_CLIENTS in /etc/lab_creation.cfg, else MCP_CLIENTS_PATH."""
    return (cfg or {}).get("MCP_CLIENTS") or MCP_CLIENTS_PATH


def load_clients(cfg=None):
    """
    The client registry as {token_sha256: {"name": str, "scopes": [str]}}, or {} when there is none.

    Shape of the file (schema config:mcp-clients.json), written by lab-mcp-issue-client-cert:
        {"schema_version": "1.0",
         "clients": [{"name": "agent-a", "token_sha256": "<hex>", "scopes": ["read"]}]}
    Unreadable, malformed or empty all give {}: the caller decides what that means, and for a server with
    mutations enabled it is a refusal to start rather than an open door.
    """
    try:
        with open(clients_path(cfg)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in (data.get("clients") or []):
        digest = str(entry.get("token_sha256") or "").strip().lower()
        name = str(entry.get("name") or "").strip()
        if digest and name:
            out[digest] = {"name": name, "scopes": list(entry.get("scopes") or [SCOPE_READ])}
    return out


class ClientTokenVerifier:
    """
    The SDK's TokenVerifier for this server's own per-client tokens: it looks a presented token's SHA-256 up in
    the registry and answers with that client's name and scopes, or None for anything unknown.

    The registry is read on every call rather than cached, so issuing or revoking a client takes effect without
    restarting the service.
    """

    def __init__(self, cfg=None):
        self._cfg = cfg

    async def verify_token(self, token: str):
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        entry = load_clients(self._cfg).get(digest)
        if not entry:
            return None
        return AccessToken(token=token, client_id=entry["name"], scopes=entry["scopes"])


def acting_client() -> str:
    """The name of the client this request authenticated as, or "-" when the server is not requiring tokens."""
    try:
        token = get_access_token()
    except LookupError:
        return "-"
    return (token.client_id if token else None) or "-"


def client_scopes() -> list:
    """The scopes of the client this request authenticated as, or [] when there is no token."""
    try:
        token = get_access_token()
    except LookupError:
        return []
    return list(token.scopes) if token else []


def _auth_components(cfg):
    """
    (token_verifier, AuthSettings) for MCPServer, or (None, None) when this server does not require tokens.

    Tokens are required exactly when MCP_ALLOW_MUTATIONS is on: that is when a caller can destroy a lab, so that
    is when knowing which caller it was stops being optional. A read-only server keeps working on mTLS alone, so
    an existing deployment is unchanged until it enables mutations.

    Returns (None, None) when mutations are on but the registry is empty, so the module still imports; main()
    refuses to start in that case rather than serving mutating tools to any certificate holder.
    """
    if str(cfg.get("MCP_ALLOW_MUTATIONS", "")).strip().lower() not in ("1", "true", "yes", "on"):
        return None, None
    if not load_clients(cfg):
        return None, None
    issuer = cfg.get("MCP_ISSUER_URL") or "https://{}:{}".format(
        socket.getfqdn(), cfg.get("MCP_PORT", "8843"))
    return ClientTokenVerifier(cfg), AuthSettings(issuer_url=issuer, required_scopes=[SCOPE_READ])


def _startup_config():
    """
    /etc/lab_creation.cfg for the construction-time auth decision, or {} when the host has none.

    primary.load_config() reports a missing configuration file with die(), which raises SystemExit, and this runs
    at import: the module has to stay importable on a host that is not a configured automation node (the test
    suite, man-page generation, the dead-function scan). Its report is kept off stderr too, since a missing file
    here is not an error. main() reads the configuration again for everything else, where it is.
    """
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return primary.load_config()
    except SystemExit:
        return {}


_verifier, _auth = _auth_components(_startup_config())
mcp = MCPServer("lab-in-a-box", token_verifier=_verifier, auth=_auth)

# streamable_http_app()'s default (host="127.0.0.1", transport_security=None)
# auto-enables Host/Origin "DNS rebinding protection" restricted to
# 127.0.0.1/localhost/::1 — confirmed live (2026-08-29) this rejects every
# real client, which connects via the server cert's actual CN
# (automation.mydemo.lab), with a 421. That check exists to stop a browser
# from being tricked into completing a same-origin request against a
# service it shouldn't reach; it's redundant here, since uvicorn's
# ssl_cert_reqs=CERT_REQUIRED already rejects any connection — browser or
# not — that lacks a client cert signed by our own CA, before a single HTTP
# request is parsed. Disable it explicitly rather than rely on the
# accidental-localhost auto-enable.
_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

_cfg_cache = None


def _cfg():
    """/etc/lab_creation.cfg, cached — server-level config (MCP_PORT/
    MCP_BIND/MCP_ALLOW_MUTATIONS/MCP_AUDIT_LOG), never a lab-JSON param."""
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = primary.load_config()
    return _cfg_cache


# ── audit log + confirmation gate ──────────────────────────────────────────────

def _audit(tool, params, target, accepted, outcome):
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "client": acting_client(),
        "tool": tool,
        "params": params,
        "target": target,
        "accepted": accepted,
        "outcome": outcome,
    }
    path = _cfg().get("MCP_AUDIT_LOG") or "/var/log/lab-mcp/audit.log"
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print("WARNING: could not write MCP audit log at {}: {}".format(path, e), file=sys.stderr)


def _require_confirm(tool, confirm, target, params):
    """
    Dies loudly (raises, so the MCP client sees a real tool error) unless
    confirm exactly equals target — the resource identifier being acted on,
    not a generic "true"/"yes". Every call is audited either way, so a
    rejected attempt is still visible in MCP_AUDIT_LOG.
    """
    if confirm != target:
        _audit(tool, params, target, accepted=False, outcome="confirm mismatch")
        raise ValueError(
            "confirm must exactly equal '{}' (the target of this operation) — "
            "this is a real, destructive infrastructure operation, not a plain "
            "yes/no confirmation".format(target)
        )


# ── read-only tools: thin wrappers around webui/lib/api.py's dispatch() ───────

def _dispatch(action, method, params=None, body=b""):
    status, obj = api.dispatch(action, method, params or {}, body)
    if status >= 400:
        raise ValueError("{}: {}".format(action, (obj or {}).get("error", obj)))
    return obj


@mcp.tool()
def list_components() -> dict:
    """List every install_<addon> component lab-in-a-box knows about, with its schema's field count."""
    return _dispatch("components", "GET")


@mcp.tool()
def get_schema(name: str) -> dict:
    """Get one addon's schema (fields, defaults, capabilities.layers/targets) by its install_<name> name."""
    return _dispatch("schema", "GET", {"name": [name]})


@mcp.tool()
def get_base_schema() -> dict:
    """Get the base lab-definition schema (common/nodes/kclusters sections)."""
    return _dispatch("base", "GET")


@mcp.tool()
def get_status() -> dict:
    """Get the cached hypervisor status snapshot (free CPU/RAM/disk per configured KVM host, images available)."""
    return _dispatch("status", "GET")


@mcp.tool()
def validate_lab(config: dict) -> dict:
    """Validate a lab definition dict against lab-in-a-box's structural rules, without deploying anything."""
    return _dispatch("validate", "POST", body=json.dumps({"config": config}).encode("utf-8"))


@mcp.tool()
def save_lab(filename: str, config: dict) -> dict:
    """Save a lab definition dict to a lab.json file on the automation VM, without deploying anything."""
    return _dispatch("save", "POST", body=json.dumps({"filename": filename, "config": config}).encode("utf-8"))


# ── mutating tools: real deploy/destroy, in-process, gated behind
#    MCP_ALLOW_MUTATIONS + the client's own "write" scope + a target-echoing
#    confirm on every call ─────────────────────────────────────────────────────

def _require_write_scope(tool, target, params):
    """
    Dies loudly (raises, so the MCP client sees a real tool error) unless the calling client was issued the
    "write" scope. MCP_ALLOW_MUTATIONS says this SERVER may mutate; this says this CLIENT may. A server that
    requires no tokens has no scopes to check and is left to MCP_ALLOW_MUTATIONS alone, which is the read-only
    deployment's behaviour.

    The refusal is audited, so an attempt by a read-only client is visible in MCP_AUDIT_LOG.
    """
    scopes = client_scopes()
    if not scopes or SCOPE_WRITE in scopes:
        return
    _audit(tool, params, target, accepted=False, outcome="refused: client has no '{}' scope".format(SCOPE_WRITE))
    raise ValueError(
        "client '{}' may read this lab but not change it: {} needs the '{}' scope and this client has {}. "
        "Reissue it with 'lab-mcp-issue-client-cert {} --allow-mutations' to grant it.".format(
            acting_client(), tool, SCOPE_WRITE, scopes, acting_client()))


def _run_mutation(tool_name, target, params, fn):
    """
    Run a mutating tool's core logic, converting ANY failure — including
    die()'s SystemExit — into a normal, audited tool error instead of
    letting it propagate.

    Why this matters: SystemExit is not a subclass of Exception, so the
    MCP framework's own error handling doesn't catch it — it propagates
    straight through and tears down the whole ASGI app/event loop.
    Confirmed live (2026-08-29): a single failed copy_vm_image() during a
    real deploy_lab call (die() deep inside setup_lab.py's call chain, a
    routine, expected failure mode — this whole codebase uses die() as its
    standard "report and stop" mechanism everywhere) killed the entire
    lab-mcp process, not just that one call — every other in-flight MCP
    session saw "Task group is not initialized" while systemd restarted
    it. die()'s message is never attached to the SystemExit it raises (it
    only ever printed to stderr, by design, for a CLI context) — point the
    caller at the service log rather than fabricating a message here.
    """
    _require_write_scope(tool_name, target, params)
    try:
        result = fn()
    except SystemExit as e:
        _audit(tool_name, params, target, accepted=True, outcome="failed (exit {})".format(e.code))
        raise RuntimeError(
            "{} failed for '{}' — check the lab-mcp service log "
            "(journalctl -u lab-mcp.service) for the real reason".format(tool_name, target)) from None
    except Exception as e:
        _audit(tool_name, params, target, accepted=True, outcome="failed: {}".format(e))
        raise
    _audit(tool_name, params, target, accepted=True, outcome="ok")
    return result


def _load_lab(json_file):
    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)
    iso_loc = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")
    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    if not lc.validate_lab_definition(definition, config, iso_loc, lab_setup_path, vm_img_loc=vm_img_loc):
        raise ValueError("'{}' failed preflight validation — see server logs".format(json_file))
    return definition, config, defaults


def deploy_lab(json_file: str, confirm: str) -> str:
    """
    Deploy the lab described by json_file (keep=True semantics: never
    destroys or recreates an existing VM matching a node name — creates
    only what's missing). Use rebuild_lab instead if you need to actually
    tear down and recreate matching VMs. confirm must exactly equal
    json_file's basename.
    """
    target = os.path.basename(json_file)
    params = {"json_file": json_file}
    _require_confirm("deploy_lab", confirm, target, params)

    def _do():
        definition, config, defaults = _load_lab(json_file)
        _setup_lab(definition, config, defaults, json_file, keep=True)

    _run_mutation("deploy_lab", target, params, _do)
    return "Deployed lab '{}' (keep=True — existing matching VMs were left alone)".format(target)


def rebuild_lab(json_file: str, confirm: str) -> str:
    """
    DESTRUCTIVE: deploy the lab described by json_file with keep=False
    semantics — every VM matching a node name is destroyed and recreated
    from scratch, even if it already exists and is healthy. Use deploy_lab
    instead if you only want to create what's missing. confirm must exactly
    equal json_file's basename.
    """
    target = os.path.basename(json_file)
    params = {"json_file": json_file}
    _require_confirm("rebuild_lab", confirm, target, params)

    def _do():
        definition, config, defaults = _load_lab(json_file)
        _setup_lab(definition, config, defaults, json_file, keep=False)

    _run_mutation("rebuild_lab", target, params, _do)
    return "Rebuilt lab '{}' (keep=False — every matching VM was destroyed and recreated)".format(target)


def destroy_lab_tool(json_file: str, confirm: str) -> str:
    """
    DESTRUCTIVE: tear down every VM in the lab described by json_file.
    confirm must exactly equal json_file's basename.
    """
    target = os.path.basename(json_file)
    params = {"json_file": json_file}
    _require_confirm("destroy_lab", confirm, target, params)

    def _do():
        defaults = primary.load_defaults()
        config = primary.load_config()
        definition = primary.load_definition(json_file)
        _destroy_lab(definition, config, defaults, json_file)

    _run_mutation("destroy_lab", target, params, _do)
    return "Destroyed lab '{}'".format(target)


def destroy_vm_tool(json_file: str, vm_name: str, confirm: str) -> str:
    """
    DESTRUCTIVE: tear down one VM (vm_name) from the lab described by
    json_file. confirm must exactly equal "<vm_name>@<json_file basename>".
    """
    target = "{}@{}".format(vm_name, os.path.basename(json_file))
    params = {"json_file": json_file, "vm_name": vm_name}
    _require_confirm("destroy_vm", confirm, target, params)

    def _do():
        defaults = primary.load_defaults()
        config = primary.load_config()
        definition = primary.load_definition(json_file)
        _destroy_vm(definition, config, defaults, vm_name)

    _run_mutation("destroy_vm", target, params, _do)
    return "Destroyed VM '{}' from lab '{}'".format(vm_name, os.path.basename(json_file))


def register_mutating_tools(server=mcp, cfg=None):
    """
    Registers deploy_lab/rebuild_lab/destroy_lab/destroy_vm on `server` only
    when MCP_ALLOW_MUTATIONS is true — they're plain functions above (not
    @mcp.tool()-decorated) specifically so registration can be conditional;
    MCPServer's decorator has no built-in "skip this one" switch, but
    programmatic add_tool() does. Split out from build_app() so tests can
    call this directly against a throwaway MCPServer instance instead of the
    module-level `mcp` singleton.
    """
    cfg = cfg if cfg is not None else _cfg()
    if str(cfg.get("MCP_ALLOW_MUTATIONS", "")).lower() not in ("1", "true", "yes"):
        return False
    server.add_tool(deploy_lab)
    server.add_tool(rebuild_lab, name="rebuild_lab")
    server.add_tool(destroy_lab_tool, name="destroy_lab")
    server.add_tool(destroy_vm_tool, name="destroy_vm")
    return True


def build_app():
    register_mutating_tools()
    return mcp.streamable_http_app(transport_security=_TRANSPORT_SECURITY)


def main():
    cfg = _cfg()
    mutations_on = register_mutating_tools(cfg=cfg)

    # A server that can destroy a lab must know which client asked. Refusing to start is the loud failure: the
    # alternative is serving mutating tools to any certificate holder, with nothing in the audit log naming the
    # caller. A read-only server needs no registry and is unaffected.
    if mutations_on and not load_clients(cfg):
        print("ERROR: MCP_ALLOW_MUTATIONS is on but no MCP client is registered in {} — a client that can "
              "destroy a lab has to be identifiable. Issue one with 'lab-mcp-issue-client-cert <name> "
              "--allow-mutations' (or '<name>' alone for a read-only client), or turn MCP_ALLOW_MUTATIONS off."
              .format(clients_path(cfg)), file=sys.stderr)
        sys.exit(1)
    print("lab-mcp: clients {}".format(
        "require a bearer token ({} registered)".format(len(load_clients(cfg))) if _auth
        else "authenticate with their TLS certificate only (no token required)"))
    print("lab-mcp: mutations {}".format("ENABLED (deploy_lab/rebuild_lab/destroy_lab/destroy_vm registered)"
                                          if mutations_on else "disabled (read-only tools only)"))

    app = mcp.streamable_http_app(transport_security=_TRANSPORT_SECURITY)

    tls_dir = Path("/etc/lab-mcp/tls")
    cert, key, ca = tls_dir / "server.crt", tls_dir / "server.key", tls_dir / "ca.crt"
    if not (cert.is_file() and key.is_file() and ca.is_file()):
        print("ERROR: {} missing server/ca cert — run the cert-generation step in "
              "install_automation_node_scripts.sh first".format(tls_dir), file=sys.stderr)
        sys.exit(1)

    import uvicorn
    uvicorn.run(
        app,
        host=cfg.get("MCP_BIND", "0.0.0.0"),
        port=int(cfg.get("MCP_PORT", "8843")),
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        ssl_ca_certs=str(ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )


if __name__ == "__main__":
    main()
