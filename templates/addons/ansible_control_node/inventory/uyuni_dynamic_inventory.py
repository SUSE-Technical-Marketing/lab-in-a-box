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

Configuration — environment variables only, never hardcoded credentials
in this file or the inventory config:
    UYUNI_HOST          Server FQDN, e.g. "sol.mydemo.lab" (required)
    UYUNI_USER          spacecmd-capable login (required)
    UYUNI_PASS          its password (required)
    UYUNI_VERIFY_SSL    "false" to skip TLS verification (default: true —
                         set to "false" for this lab's own self-signed
                         cert, same reality install_smlm.py itself works
                         around elsewhere in this project)

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
"""
import json
import os
import ssl
import sys
import xmlrpc.client


def _die(msg):
    print("ERROR: {}".format(msg), file=sys.stderr)
    sys.exit(1)


def _server_proxy():
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
            members = proxy.systemgroup.listSystemsMinimal(session, group_name)
            member_names = [m.get("name") for m in members if m.get("name")]
            inventory[group_name] = {"hosts": member_names}
            inventory["all"]["children"].append(group_name)
            grouped_names.update(member_names)

        for system in systems:
            name = system.get("name")
            if not name:
                continue
            hostvars[name] = {"uyuni_system_id": system.get("id")}
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
