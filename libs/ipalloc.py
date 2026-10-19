#!/usr/bin/env python3
# Part of lab-in-a-box — deterministic IP address allocation for a lab's nodes.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/ipalloc.py: picks the IP address of a node whose lab definition does not give one.

A node with no "myip" gets one from the network its own lab already describes: the network of common.mygw with
common.mymask. There is no pool field to keep in step with the gateway, the mask and the DNS server, because every
one of those is already in the definition.

The choice is deterministic: the node's name and its network decide where in the network the search starts, so the
same definition always produces the same addresses. A rebuilt lab keeps its DNS records, its PXE entries and its
kubeconfigs, and two automation nodes handed the same definition agree without talking to each other.

What an address must not be, in the order the allocator rules them out:

    the network and broadcast addresses, the gateway, the lab's DNS server, this automation node's own addresses
    an address another node of this same definition uses
    an address the lab DNS already has a record for, which is the ledger of what this automation node handed out
    an address the backend reports in use (libvirt's leases and its hosts' neighbour tables)
    an address that answers a ping, which is the last gate and the only one that can see a stranger on the LAN

An address that answers is refused rather than warned about: the preflight's own ping (lab_creation.py) warns on an
address the lab asked for by name, where the lab is presumed to know better. Here nobody asked, so a reply is reason
enough to move on to the next candidate.
"""

import hashlib
import ipaddress
import re
import socket
import subprocess
from pathlib import Path

NAMED_ZONE_DIR = Path("/var/lib/named")

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def lab_network(common):
    """
    The network a lab's nodes live on, from common.mygw and common.mymask, or None when the definition does not say
    (a lab whose nodes are all on a cloud backend, for one).
    """
    gateway = str((common or {}).get("mygw") or "").strip()
    mask = str((common or {}).get("mymask") or "").strip()
    if not gateway or not mask:
        return None
    try:
        return ipaddress.ip_network("{}/{}".format(gateway, mask), strict=False)
    except ValueError:
        return None


def definition_ips(definition):
    """Every address the definition itself already pins down: its nodes' and its common section's."""
    used = set()
    common = definition.get("common", {}) or {}
    for key in ("mygw", "mydns"):
        for value in str(common.get(key) or "").replace(",", " ").split():
            used.add(value.strip())
    for node_cfg in (definition.get("nodes") or {}).values():
        myip = str((node_cfg or {}).get("myip") or "").strip()
        if myip:
            used.add(myip)
    return {ip for ip in used if _IPV4.fullmatch(ip)}


def dns_ips(mydomain, zone_dir=NAMED_ZONE_DIR):
    """
    Every address the lab DNS has a record for, read from the forward zone of `mydomain`. This is the ledger: the
    record is written before a VM is created, so an address in the zone is taken even when nothing answers on it.
    """
    if not mydomain:
        return set()
    zone = Path(zone_dir) / "{}.lan".format(mydomain)
    try:
        text = zone.read_text(errors="replace")
    except OSError:
        return set()
    found = set()
    for line in text.splitlines():
        if re.search(r"\bIN\s+A\b", line):
            found.update(_IPV4.findall(line))
    return found


def ips_in_text(text):
    """Every IPv4 address in `text`, for reading an address out of a tool's output (libvirt leases, a neighbour
    table) without parsing its format."""
    return set(_IPV4.findall(text or ""))


def dns_address(vm_name, mydomain, zone_dir=NAMED_ZONE_DIR):
    """
    The address the lab DNS holds for `vm_name`, or "". Read from the forward zone of `mydomain`, falling back to
    whatever the resolver answers. This is how an address the lab definition never wrote down is recovered: it was
    registered before the VM was created, so the zone knows it even when the file does not.
    """
    short = str(vm_name or "").split(".")[0]
    if short and mydomain:
        zone = Path(zone_dir) / "{}.lan".format(mydomain)
        try:
            for line in zone.read_text(errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[0] == short and fields[-2] == "A":
                    return fields[-1]
        except OSError:
            pass
    try:
        return socket.gethostbyname(vm_name)
    except OSError:
        return ""


def local_ips():
    """This automation node's own addresses, which no VM may be given."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    except OSError:
        return set()
    return {m.split("/")[0] for m in re.findall(r"inet (\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})", out)}


def answers_ping(ip):
    """True when something at `ip` replies. The last gate, and the only one that can see a machine nobody told us
    about."""
    try:
        return subprocess.run(["ping", "-c1", "-W1", str(ip)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def candidates(vm_name, network):
    """
    Every usable address of `network`, ordered so that the one derived from `vm_name` comes first and the rest follow
    it in order, wrapping around. The name and the network are the only inputs, so the order never changes.
    """
    hosts = list(network.hosts())
    if not hosts:
        return []
    digest = hashlib.sha256("{}@{}".format(vm_name, network).encode("utf-8")).digest()
    start = int.from_bytes(digest[:8], "big") % len(hosts)
    return [str(ip) for ip in hosts[start:] + hosts[:start]]


def allocate(vm_name, definition, used=(), mydomain="", probe=True, zone_dir=NAMED_ZONE_DIR):
    """
    The address `vm_name` should have, or None when the lab's own network cannot be worked out or every address in it
    is taken.

    `used` is what the backend reports in use, which it alone can see. Everything else comes from the definition, the
    lab DNS and this node. `probe` runs the ping gate, and is turned off where a reply cannot be waited for, such as
    the web UI asking what an address would be.
    """
    common = definition.get("common", {}) or {}
    network = lab_network(common)
    if network is None:
        return None

    mydomain = mydomain or str(common.get("mydomain") or "")

    # This node's own record, if the lab DNS already has one, IS its address: a second run of a lab that never wrote
    # the address down must reach the same answer, and without this its own record would be read as a conflict and
    # push it somewhere else.
    registered = dns_address(vm_name, mydomain, zone_dir=zone_dir)
    if registered and ipaddress.ip_address(registered) in network:
        return registered

    taken = set(used) | definition_ips(definition) | dns_ips(mydomain, zone_dir=zone_dir) | local_ips()
    taken.discard(str((definition.get("nodes") or {}).get(vm_name, {}).get("myip") or ""))

    for candidate in candidates(vm_name, network):
        if candidate in taken:
            continue
        if probe and answers_ping(candidate):
            continue
        return candidate
    return None
