#!/usr/bin/env python3
"""
The shape of a BIND zone this project manages: the file names, the header a new zone needs to load at all, the
named.conf stanza that declares it, and how two servers' zones are compared.

One module because the same zone is created in three places — the automation node's own BIND (libs/services.py), the
DNS VM a cloud lab gets (libs/backends.py's ensure_cloud_dns_vm) and the bootstrap that builds an automation node
(setup_demo_server/automation-node-lib.sh) — and a zone written in one shape and compared in another reads as drift
that is never resolved.

    forward_file(mydomain) / reverse_file(mynet_reverse)  -> the zone file's name, without a directory
    forward_header(mydomain) / reverse_header(...)        -> the lines a new zone file starts with
    stanza(zone, zone_file)                               -> the named.conf declaration for it
    records(text)                                         -> the comparable records of a zone file, as a set
    pattern(record)                                       -> matches the file line any records() entry came from
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

from typing import List, Set

__version__ = "__LABVERSION__"

# The serial of a zone this project creates. Every record is appended by rewriting the file and reloading named, so
# no secondary ever transfers the zone and the serial is never compared: it exists because BIND rejects a zone
# without one.
_SERIAL = 1


def forward_file(mydomain: str) -> str:
    """The forward zone's file name for `mydomain`, as named.conf's "file" and as the name under /var/lib/named."""
    return "{}.lan".format(mydomain)


def reverse_file(mynet_reverse: str) -> str:
    """The reverse zone's file name for `mynet_reverse` ("88.168.192"), the counterpart of forward_file()."""
    return "{}.db".format(mynet_reverse)


def reverse_zone(mynet_reverse: str) -> str:
    """The reverse zone's name, which is the reversed network with the in-addr.arpa suffix."""
    return "{}.in-addr.arpa".format(mynet_reverse)


def forward_header(mydomain: str, ttl: int = 86400) -> List[str]:
    """
    The lines a new forward zone for `mydomain` starts with: a TTL, an SOA, an NS and that NS's own address. BIND
    refuses to load a zone missing any of the first three, and refuses an NS it cannot resolve inside the zone.

    `ttl` is lowered for a zone whose addresses change often, such as a cloud lab's, where a resolver holding an
    old answer for a day would outlast the node it names.
    """
    return [
        "$TTL {}".format(ttl),
        "@ IN SOA ns.{d}. admin.{d}. ( {s} 3600 900 604800 300 )".format(d=mydomain, s=_SERIAL),
        "@ IN NS ns.{d}.".format(d=mydomain),
        "ns IN A 127.0.0.1",
    ]


def reverse_header(mydomain: str, mynet_reverse: str, ttl: int = 86400) -> List[str]:
    """The reverse-zone counterpart of forward_header(), whose NS is the forward zone's."""
    return [
        "$TTL {}".format(ttl),
        "@ IN SOA ns.{d}. admin.{d}. ( {s} 3600 900 604800 300 )".format(d=mydomain, s=_SERIAL),
        "@ IN NS ns.{d}.".format(d=mydomain),
    ]


def stanza(zone: str, zone_file: str) -> str:
    """The named.conf declaration making this server a master of `zone`, served from `zone_file`."""
    return 'zone "{z}" {{ type master; file "{f}"; allow-update {{ none; }}; }};'.format(z=zone, f=zone_file)


def records(text: str) -> Set[str]:
    """
    The records of a zone file, as a set of whitespace-collapsed lines, for comparing one server's zone with
    another's.

    What a zone holds and what two masters of it should agree on are not the same thing: the header is per server
    (its own serial, its own NS address), continuation lines of a multi-line SOA carry no record, and the order
    records were appended in is an artefact of when each node was created. All of those are dropped, so a
    difference in the result is a difference in what the two servers actually answer.
    """
    out = set()
    for line in (text or "").splitlines():
        line = line.split(";")[0].strip()
        if not line or line.startswith("$") or line.startswith("@") or line.startswith(")"):
            continue
        fields = line.split()
        # A header's own NS/SOA/MX lines start with the type rather than a name, and "ns" is the in-zone name of
        # the server's own NS address, which is per server. Neither is a record a master is expected to match.
        if fields[0] in ("IN", "SOA", "NS", "MX", "ns"):
            continue
        # An SOA's continuation lines are a bare number, while a reverse zone's PTR records start with an octet, so
        # the field count is what separates them.
        if fields[0].isdigit() and len(fields) < 3:
            continue
        out.add(" ".join(fields))
    return out


def pattern(record: str) -> str:
    """
    A POSIX extended regular expression matching exactly the zone-file lines whose records() form is `record`,
    for removing one with sed or re.

    records() collapses the whitespace a zone file separates its fields with, so a record read back from it no
    longer matches the line it came from literally. Each run of whitespace is matched instead, and a dot is
    escaped so an address cannot match a different one.
    """
    fields = [field.replace("\\", "\\\\").replace(".", "\\.") for field in record.split()]
    return "^[[:space:]]*" + "[[:space:]]+".join(fields) + "[[:space:]]*$"
