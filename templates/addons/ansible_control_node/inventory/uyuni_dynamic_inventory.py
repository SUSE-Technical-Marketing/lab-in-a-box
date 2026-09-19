#!/usr/bin/env python3
"""
uyuni_dynamic_inventory.py — a real Ansible dynamic inventory SCRIPT (the
classic executable --list/--host protocol, not a YAML-config inventory
plugin/collection — no extra Ansible collection to install) that builds
its inventory live from a SUSE Multi-Linux Manager/Uyuni server's own
system list and system groups, via the server's real XML-RPC API
(stdlib xmlrpc.client only — no extra Python package needed).

Ground-truthed against documentation.suse.com/multi-linux-manager's own
API reference, 2026-09-18: system.listSystems (every visible system),
systemgroup.listAllGroups + systemgroup.listSystemsMinimal (group
membership) — real, confirmed methods, not guessed.

Configuration — environment variables, never hardcoded credentials in
this file:
    UYUNI_HOST          Server FQDN, e.g. "sol.mydemo.lab" (required)
    UYUNI_USER          spacecmd-capable login (required)
    UYUNI_PASS          its password (required)
    UYUNI_VERIFY_SSL    "false" to skip TLS verification (default: true —
                         set to "false" for this lab's own self-signed
                         cert, same reality install_smlm.py itself works
                         around elsewhere in this project)

Any of these not already present in the process environment are filled in
from _ENV_FILE_PATH (/etc/ansible/uyuni_inventory.env — a plain KEY=VALUE
file, written by install_ansible_control_node.py's own
setup_ansible_control_node() when a smlm/uyuni addon node exists in the
same lab) if it exists — added 2026-09-18 after a real, live-reported
failure: SMLM's own "Ansible > Schedule Playbook" feature invokes this
script via a Salt state running under salt-minion's own process
environment, which never inherits anything an operator `export`ed in
their own interactive shell, so the original environment-variables-only
design broke every time SMLM itself (rather than a human at a terminal)
ran it. An operator's own already-exported environment variables always
take priority over the file, so nothing changes for a manual run.

Usage (standard Ansible dynamic inventory contract):
    ansible-playbook -i uyuni_dynamic_inventory.py ping.yml
    ./uyuni_dynamic_inventory.py --list      # what Ansible actually calls
    ./uyuni_dynamic_inventory.py --host x    # per-host vars fallback —
                                              # always {} here since --list
                                              # already returns _meta.hostvars
                                              # for every host (avoids one
                                              # XML-RPC round trip per host)

Every system's own numeric Uyuni id is exposed as hostvar
"uyuni_system_id" — the same id this project's own smlm_ansible_paths/
smlm_ansible_control_nodes JSON fields need, handy for cross-referencing
without a second lookup. Systems in no group at all still appear, under
the synthetic "ungrouped" group — Ansible's own inventory convention.

SMLM/Uyuni system group names are sanitized into valid Ansible group names
(letters/digits/underscore only — see _sanitize_group_name()) before use,
since Ansible group names may not contain hyphens and this lab's own real
system groups do (e.g. "galilean-moons").

The control node itself is normally ALSO a registered SMLM system (it has
to be, to hold the Ansible Control Node entitlement) and so naturally
appears in this inventory too — but install_ansible_control_node.py only
ever installs its own SSH key on every OTHER lab node, never on itself
(the control node has no reason to SSH to itself under normal use). A
real, live-reported failure (2026-09-19): SMLM's own "Schedule Playbook"
run against the FULL inventory tried to SSH to the control node as just
another target and failed outright ("Permission denied"). Fixed the
standard Ansible way: whichever inventory host's name matches this
script's OWN local FQDN (socket.getfqdn()) gets "ansible_connection":
"local" in its hostvars, so Ansible runs tasks against it directly
instead of over SSH — confirmed live to match the real SMLM system name
exactly (both "charon.mydemo.lab").
"""
import json
import os
import re
import socket
import ssl
import sys
import xmlrpc.client

_RESERVED_GROUP_NAMES = {"all", "ungrouped", "_meta"}


def _sanitize_group_name(name):
    """
    Ansible group names may only contain letters, digits, and underscores.
    A real, live-reported bug (2026-09-19): this lab's own SMLM system
    groups include several with hyphens (e.g. "galilean-moons",
    "gas-giants", "terrestrial-planets") — using them verbatim as Ansible
    group names triggers Ansible's own "[WARNING]: Invalid characters were
    found in group names but not replaced" on stderr. Harmless for a plain
    `ansible-playbook` run, but SMLM's own "Ansible > Schedule Playbook"
    Salt-state wrapper treats ANY stderr from the inventory script as a
    hard failure — confirmed live, the whole playbook run was reported as
    failed even though the inventory itself parsed and returned correct
    data. Sanitizing here means nothing is ever emitted for Ansible to
    warn about in the first place.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if sanitized in _RESERVED_GROUP_NAMES:
        sanitized = "{}_group".format(sanitized)
    return sanitized

_ENV_FILE_PATH = "/etc/ansible/uyuni_inventory.env"


def _die(msg):
    print("ERROR: {}".format(msg), file=sys.stderr)
    sys.exit(1)


def _load_env_file():
    """Fills in any of UYUNI_HOST/USER/PASS/VERIFY_SSL not already present
    in the process environment from _ENV_FILE_PATH, if it exists — see
    module docstring. Silently a no-op if the file is missing (e.g. no
    smlm/uyuni addon node in this lab, or an operator who prefers setting
    real env vars by hand). Already-set environment variables always win."""
    try:
        with open(_ENV_FILE_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


def _server_proxy():
    _load_env_file()
    host = os.environ.get("UYUNI_HOST")
    user = os.environ.get("UYUNI_USER")
    password = os.environ.get("UYUNI_PASS")
    if not host or not user or not password:
        _die("UYUNI_HOST, UYUNI_USER and UYUNI_PASS must all be set in the environment")

    verify_ssl = os.environ.get("UYUNI_VERIFY_SSL", "true").strip().lower() != "false"
    context = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()
    proxy = xmlrpc.client.ServerProxy("https://{}/rpc/api".format(host), context=context)
    return proxy, user, password


def build_inventory():
    proxy, user, password = _server_proxy()
    session = proxy.auth.login(user, password)
    try:
        systems = proxy.system.listSystems(session)
        groups = proxy.systemgroup.listAllGroups(session)

        hostvars = {}
        inventory = {"_meta": {"hostvars": hostvars}, "all": {"children": ["ungrouped"]}, "ungrouped": {"hosts": []}}
        grouped_names = set()

        for group in groups:
            group_name = group.get("name")
            if not group_name:
                continue
            ansible_group_name = _sanitize_group_name(group_name)
            members = proxy.systemgroup.listSystemsMinimal(session, group_name)
            member_names = [m.get("name") for m in members if m.get("name")]
            if ansible_group_name in inventory:
                # Two different SMLM group names sanitized to the same Ansible
                # group name (e.g. "gas-giants" and "gas_giants") -> merge
                # their hosts rather than silently dropping one.
                inventory[ansible_group_name]["hosts"] = sorted(
                    set(inventory[ansible_group_name]["hosts"]) | set(member_names))
            else:
                inventory[ansible_group_name] = {"hosts": member_names}
                inventory["all"]["children"].append(ansible_group_name)
            grouped_names.update(member_names)

        local_fqdn = socket.getfqdn()
        for system in systems:
            name = system.get("name")
            if not name:
                continue
            hv = {"uyuni_system_id": system.get("id")}
            if name == local_fqdn:
                hv["ansible_connection"] = "local"
            hostvars[name] = hv
            if name not in grouped_names:
                inventory["ungrouped"]["hosts"].append(name)

        return inventory
    finally:
        # Always logs out, even if a listSystems/listAllGroups call above
        # raised — an abandoned session is a real (if minor) server-side
        # resource leak otherwise.
        try:
            proxy.auth.logout(session)
        except Exception:
            pass


def main():
    if "--list" in sys.argv:
        print(json.dumps(build_inventory()))
    elif "--host" in sys.argv:
        # _meta.hostvars in --list already covers every host, so Ansible
        # should never actually call this — real per-host implementation
        # only for tools that don't honour _meta (correct, empty fallback).
        print(json.dumps({}))
    else:
        _die("Usage: {} --list | --host <name>".format(sys.argv[0]))


if __name__ == "__main__":
    main()
