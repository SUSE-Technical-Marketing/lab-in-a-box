#!/usr/bin/env python3
# Part of lab-in-a-box — USB-delivery pipeline (deliver a completed lab on a
# USB stick, standalone-bootable on other hardware).
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/lab_usb.py — supporting logic for scripts/build_lab_usb.py.

Design (see /root/.claude/plans/wiggly-zooming-pretzel.md for the full
write-up): create one ordinary VM (the "lab-host VM") using this project's
existing VM-creation pipeline, bootstrap it with the SAME already-tested
NAT-mode automation-VM flow (setup_kvm_node.py + setup_lab_automation.sh,
_network_mode=nat) with no changes, then run the lab's own setup_lab.py, unchanged, ON that
nested automation VM against a version of the lab definition whose node
addresses have been remapped into the internal NAT range. Shut the lab-host
VM down and its own disk (raw, not QCOW2 — see backends.LibvirtBackend.
create_vm's disk_format param) is a complete, self-contained, bootable
image of the whole lab.
"""

import copy
import ipaddress


def remap_lab_definition_to_nat(definition, nat_cidr="192.168.150.0/24",
                                 nested_automation_ip=None, start_offset=10):
    """
    Returns a NEW lab definition (deep copy — the original is never
    mutated) with every node's myip remapped into nat_cidr, and common's
    mygw/mymask/mydns updated to match that internal network. mymac is left
    untouched (a MAC doesn't need to be "in range" the way an IP does — no
    reason to touch it).

    Per this project's own read/write convention
    ([[feedback_config_file_read_write_convention]]): this is a pure,
    in-memory transformation. It never writes anything to disk and never
    touches whatever file the original `definition` was loaded from — the
    caller decides separately whether/where to persist the result (e.g.
    pushing it onto the nested automation VM to run setup_lab.py against),
    and primary.save_definition() (format-transparent, writes to a
    .system_modified.<fmt> sibling, never the source file) is the only
    thing that should ever do so, and only for debugging.

    Args:
        definition           : the original lab definition (any dict-like
                                object — a plain dict or a
                                primary.LabDefinition both work; only
                                .get()/[...] access is used).
        nat_cidr              : the internal NAT network's own CIDR — must
                                match whatever _nat_network_cidr the lab-host
                                VM's own bootstrap (setup_kvm_node.py,
                                _network_mode=nat) is configured with.
        nested_automation_ip : the nested automation VM's own static IP
                                inside nat_cidr (it runs the lab's DNS
                                server — see the NAT+port-forwarding
                                feature's own design) — every remapped
                                node's mydns points here. Required.
        start_offset          : remapped nodes start at nat_cidr's network
                                address + this many hosts in (default 10),
                                leaving room for the gateway (host 1) and
                                the nested automation VM's own static IP
                                below that offset.

    Raises ValueError if nat_cidr doesn't have enough addresses for every
    node in definition["nodes"] starting at start_offset.
    """
    if not nested_automation_ip:
        raise ValueError("remap_lab_definition_to_nat: nested_automation_ip is required")

    remapped = copy.deepcopy(dict(definition))
    network = ipaddress.ip_network(nat_cidr, strict=False)
    available = [str(h) for h in network.hosts()][start_offset - 1:]

    nodes = remapped.get("nodes", {}) or {}
    if len(nodes) > len(available):
        raise ValueError(
            "NAT network {} has only {} usable addresses from offset {} onward — "
            "not enough for {} node(s)".format(nat_cidr, len(available), start_offset, len(nodes))
        )

    for (node_name, node_cfg), ip in zip(nodes.items(), available):
        # `node_cfg or {}` builds a THROWAWAY dict when node_cfg is falsy
        # (an empty {} or null node entry, e.g. a minimal node relying
        # entirely on inherited common defaults) — mutating that instead
        # of the real per-node dict silently drops myip for that node
        # (a node whose entry is a plain `{}` would otherwise get no myip in the remapped output).
        # Replace the falsy entry with a real dict in `nodes` itself first.
        if not node_cfg:
            node_cfg = nodes[node_name] = {}
        node_cfg["myip"] = ip

    common = remapped.setdefault("common", {})
    common["mygw"] = str(list(network.hosts())[0])
    common["mymask"] = str(network.prefixlen)
    common["mydns"] = nested_automation_ip

    return remapped
