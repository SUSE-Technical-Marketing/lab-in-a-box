#!/usr/bin/env python3
# Unit tests for scripts/install_buffer — mocked ssh_run, no real SSH and nothing reaching buffer.com. What is
# asserted is that the endpoints are the ones Buffer publishes, that no agent is connected unless the lab names
# one, that an API key never reaches a command line, and what --check compares. Run from 154_buffer.sh, in its
# own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import agents  # noqa: E402
import apps  # noqa: E402

bf = apps.load_addon_module("install_buffer", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeSSH(object):
    """Records every command, and answers "command -v" as the test asks."""

    def __init__(self, present=()):
        self.calls = []
        self.present = set(present)

    def __call__(self, host, command, input_text=None, check=True, capture=False, **kwargs):
        self.calls.append((host, command, input_text))
        rc = 0
        if "command -v" in command:
            rc = 0 if any(c in command for c in self.present) else 1
        import subprocess
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr="")


# ── the endpoints are Buffer's own, not invented ──────────────────────────────
check("the MCP endpoint is the one Buffer publishes", bf.MCP_URL == "https://mcp.buffer.com/mcp")
check("the API endpoint is the one Buffer publishes", bf.API_URL == "https://api.buffer.com")
check("the MCP server is registered as a remote HTTP endpoint",
      bf.mcp_definition({}) == {"type": "http", "url": "https://mcp.buffer.com/mcp"})
check("no credential goes into the server definition, because Buffer's MCP clients use OAuth",
      not any(k in bf.mcp_definition({"buffer_api_key": "secret"}) for k in ("headers", "env", "token")))
check("an installation behind its own proxy can point it elsewhere",
      bf.mcp_definition({"buffer_mcp_url": "https://proxy.lab/mcp"})["url"] == "https://proxy.lab/mcp")

# ── which agents, and the default of none ─────────────────────────────────────
check("naming nothing connects nothing, because these tools publish publicly", bf.agents_of({}) == [])
check("an empty value is the same as nothing", bf.agents_of({"buffer_agents": []}) == [])
check("one agent named is one agent", bf.agents_of({"buffer_agents": ["claude-code"]}) == ["claude-code"])
check("a bare string is accepted as one name", bf.agents_of({"buffer_agents": "claude-code"}) == ["claude-code"])
check("\"all\" is every agent this add-on knows",
      bf.agents_of({"buffer_agents": "all"}) == list(agents.AGENT_COMMANDS))
check("the order is the add-on's own, not the order the lab happened to write",
      bf.agents_of({"buffer_agents": ["opencode", "claude-code"]})
      == [n for n in agents.AGENT_COMMANDS if n in ("opencode", "claude-code")])
check("an unknown name is reported rather than silently dropped",
      bf.unknown_agents({"buffer_agents": ["claude-code", "nonsense"]}) == ["nonsense"])
check("root is the account when the lab names none", bf.users_of({}) == ["root"])

# ── validation ────────────────────────────────────────────────────────────────
import addon_common as ac  # noqa: E402


def findings(section):
    v = ac.Validator({"buffer": section})
    bf._validate(v)
    return v.errors


errors = findings({})
check("a lab that names neither an agent nor a key is not an error: the run says so instead", not errors)
errors = findings({"buffer_agents": ["nonsense"]})
check("an unknown agent is an error naming it and the real ones",
      any("nonsense" in e and "claude-code" in e for e in errors))
errors = findings({"buffer_mcp_url": "mcp.buffer.com"})
check("a URL without a scheme is an error", any("buffer_mcp_url" in e for e in errors))
errors = findings({"buffer_users": 7})
check("users has to be a list of accounts", any("buffer_users" in e for e in errors))
check("naming an agent is enough on its own, with no key", not findings({"buffer_agents": ["claude-code"]}))

# ── the key never reaches a command line ──────────────────────────────────────
ssh = FakeSSH(present=["claude"])
bf.ssh_run = ssh
agents.ssh_run = ssh
bf.agents.ssh_run = ssh
bf.setup_buffer("n1.lab", {"buffer_agents": ["claude-code"], "buffer_api_key": "bfr-secret-123"})
commands = [c for _h, c, _i in ssh.calls]
check("the API key is never in a command, where the node's process list would show it",
      not any("bfr-secret-123" in c for c in commands))
check("it reaches the node on stdin instead",
      any((i or "").strip() == "BUFFER_TOKEN=bfr-secret-123" for _h, _c, i in ssh.calls))
check("it is written with a umask that keeps it to one account",
      any("umask 077" in c for c in commands))
check("the MCP server is registered through Claude Code itself",
      any("mcp add-json" in c and "buffer" in c for c in commands))
check("a changed definition replaces the one a previous run left",
      any("mcp remove" in c for c in commands))
check("the endpoint registered is the published one",
      any("mcp.buffer.com/mcp" in c for c in commands))
check("the key is sourced from an interactive shell by default",
      any(".bashrc" in c for c in commands))

ssh = FakeSSH(present=[])
bf.ssh_run = ssh
agents.ssh_run = ssh
bf.setup_buffer("n1.lab", {"buffer_agents": ["claude-code"]})
check("an agent that is not on the node is skipped rather than failing the lab",
      not any("mcp add-json" in c for _h, c, _i in ssh.calls))

ssh = FakeSSH(present=["claude"])
bf.ssh_run = ssh
agents.ssh_run = ssh
bf.setup_buffer("n1.lab", {})
check("a lab that asked for nothing does nothing at all", ssh.calls == [])

# ── the add-on declares itself honestly ───────────────────────────────────────
check("it installs nothing in a cluster, since there is nothing to install",
      bf.PLUGIN["targets"] == ["vm", "baremetal"] and bf.PLUGIN["layers"] == ["os-native"])
check("it has a --check of its own", bf.PLUGIN["check"] is True)
source = (_REPO / "scripts" / "install_buffer").read_text()
check("the header says plainly that nothing is self-hosted", "NOTHING IS INSTALLED ON THE NODE" in source)
check("and says the tools act on real accounts", "act on real published" in source
      or "real social accounts" in source)
check("the published documentation it was written from is cited",
      "developers.buffer.com/guides/integrations/mcp.html" in source)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all buffer checks passed")
