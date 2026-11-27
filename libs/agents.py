#!/usr/bin/env python3
"""
The coding agents on a lab node, and how an add-on connects a service to them.

Several add-ons install a service and then have to make the node's own agents able to drive it. The mechanism is
each agent's own documented one, never a configuration file written behind its back, so this holds the small
amount that is common: running a command as the account an agent's configuration belongs to, reaching a Claude
Code the PATH of a non-interactive shell does not carry, and registering an MCP server through the CLI that owns
the file it lives in.

Nothing here knows about any particular service. An add-on says which agents and which servers; this says how.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import shlex

from lab_creation import ssh_run

# The agents an add-on can connect a server to, by the name a lab writes, with the command that has to be on the
# node for that agent to count. An agent absent from a node is reported and skipped, so a lab listing several
# still gets the ones it has.
AGENT_COMMANDS = {
    "claude-code": "claude",
    "copilot-cli": "copilot",
    "gemini-cli": "gemini",
    "codex": "codex",
    "cursor": "cursor-agent",
    "opencode": "opencode",
}


def as_user(user: str, command: str) -> str:
    """`command` run as `user`, which for root is the command itself since every ssh_run is already root."""
    if user == "root":
        return command
    return "su - {} -s /bin/bash -c {}".format(shlex.quote(user), shlex.quote(command))


def claude_command(args: str) -> str:
    """`claude` with `args`, reaching a native install whose directory no non-interactive shell has on its PATH."""
    return 'PATH="$HOME/.local/bin:$PATH" claude {}'.format(args)


def has_command(host: str, command: str, user: str = "root") -> bool:
    """Whether `command` is on `host` for `user`, which is how an agent's presence is decided."""
    found = ssh_run(host, as_user(user, "command -v {}".format(shlex.quote(command))),
                    check=False, capture=True)
    return found.returncode == 0


def register_mcp_servers(host: str, user: str, servers: dict) -> None:
    """
    Register `servers` in `user`'s own MCP scope through Claude Code itself, which owns the file they live in.

    Each name is removed first, so a changed definition replaces the one a previous run left. A definition reaches
    the node as an argument of this command, and is visible in the node's process list while it runs: a server
    that needs a secret takes it from the environment, not from here.
    """
    for name, definition in sorted((servers or {}).items()):
        ssh_run(host, as_user(user, claude_command("mcp remove {} --scope user".format(shlex.quote(str(name))))),
                check=False, capture=True)
        print("- Registering the '{}' MCP server for '{}' on '{}'".format(name, user, host))
        ssh_run(host, as_user(user, claude_command("mcp add-json {} {} --scope user".format(
            shlex.quote(str(name)), shlex.quote(json.dumps(definition))))))
