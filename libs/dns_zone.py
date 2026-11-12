#!/usr/bin/env python3
"""
The shape of a BIND zone this project manages: the file names, the header a new zone needs to load at all, the
named.conf stanza that declares it, and how two servers' zones are compared.

One module because the same zone is created in three places — the automation node's own BIND (libs/services.py), the
DNS VM a cloud lab gets (libs/backends.py's ensure_cloud_dns_vm) and the bootstrap that builds an automation node
(setup_demo_server/automation-node-lib.sh) — and a zone written in one shape and compared in another reads as drift
that is never resolved.

    forward_file(mydomain) / reverse_file(mynet_reverse)  -> the zone file's name, without a directory
    site_file(mydomain, site) / site_of_file(...)         -> a site view's forward zone file, and back
    node_record(short, address) / node_address(text, ..)  -> a node's own A line, and its address in a zone file
    forward_header(mydomain) / reverse_header(...)        -> the lines a new zone file starts with
    zone_dir(os_id, os_like) / zone_path(file_name, ...)  -> where that family's named reads a zone
    stanza(zone, zone_file)                               -> the named.conf declaration for it
    view_named_conf(...)                                  -> the whole named.conf of a server serving site views
    records(text)                                         -> the comparable records of a zone file, as a set
    pattern(record)                                       -> matches the file line any records() entry came from
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

from typing import List, Set, Tuple

__version__ = "__LABVERSION__"

# The serial of a zone this project creates. Every record is appended by rewriting the file and reloading named, so
# no secondary ever transfers the zone and the serial is never compared: it exists because BIND rejects a zone
# without one.
_SERIAL = 1

# Where each distribution family lets named READ a master zone, which is not the same everywhere. Debian and
# Ubuntu confine named with AppArmor to "/etc/bind/** r" for zones it is the origin of, so a file written anywhere
# else is denied and the zone never loads however correctly it is declared. /var/cache/bind is writable there but
# is for slave and stub data, which these zones are not. The SUSE and Fedora families keep them in /var/lib/named.
# A stanza names the file by its absolute path, because BIND otherwise resolves it against its own "directory"
# option, which differs again.
ZONE_DIR_DEBIAN = "/etc/bind"
ZONE_DIR_DEFAULT = "/var/lib/named"

# os-release IDs of the Debian family, matched before falling back to ID_LIKE, as libs/db_common.py does.
_DEBIAN_IDS = ("debian", "ubuntu", "raspbian", "linuxmint", "devuan", "pop")


def zone_dir(os_id: str = "", os_like: str = "") -> str:
    """
    The directory a host of this os-release ID (or ID_LIKE family) serves a master zone from.

    Both empty gives the default, which is what a caller that cannot detect the family gets.
    """
    os_id = (os_id or "").lower()
    os_like = (os_like or "").lower()
    if os_id in _DEBIAN_IDS or "debian" in os_like or "ubuntu" in os_like:
        return ZONE_DIR_DEBIAN
    return ZONE_DIR_DEFAULT


def forward_file(mydomain: str) -> str:
    """The forward zone's file name for `mydomain`, as named.conf's "file" and as the name under /var/lib/named."""
    return "{}.lan".format(mydomain)


def site_file(mydomain: str, site: str) -> str:
    """
    The forward zone file of `mydomain` as `site` sees it: that site's own nodes at their private address, every other
    node at its public one. forward_file() is the view everyone else sees.
    """
    return "{}.site-{}.lan".format(mydomain, site)


def site_of_file(mydomain: str, file_name: str) -> str:
    """The site a site_file() of `mydomain` belongs to; "" for any other file name."""
    prefix, suffix = "{}.site-".format(mydomain), ".lan"
    if file_name.startswith(prefix) and file_name.endswith(suffix) and len(file_name) > len(prefix) + len(suffix):
        return file_name[len(prefix):-len(suffix)]
    return ""


def node_record(short: str, address: str) -> str:
    """A node's own A line, in the layout every zone file here uses for one, distinct from a service record's."""
    return "{}         IN  A       {}".format(short, address)


def node_address(text: str, short: str) -> str:
    """The address zone file `text` gives node `short` in its own A line (node_record()); "" when it has none."""
    for line in (text or "").splitlines():
        fields = line.split()
        if line.startswith(short + " ") and fields[1:3] == ["IN", "A"] and len(fields) == 4:
            return fields[3]
    return ""


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


def local_named_conf(os_id: str = "", os_like: str = "") -> str:
    """
    The file this host declares a zone in: named.conf.local on Debian and Ubuntu, which their own named.conf
    already includes, and named.conf itself elsewhere, which is where an automation node's zones already are.
    """
    if zone_dir(os_id, os_like) == ZONE_DIR_DEBIAN:
        return "/etc/bind/named.conf.local"
    return "/etc/named.conf"


def remote_named_conf(os_id: str = "", os_like: str = "") -> str:
    """
    The file a zone is declared in on a server this project configures: named.conf.local on Debian and Ubuntu, and
    a dedicated include file elsewhere, so the stanzas never sit in a file the distribution itself rewrites.

    Where it is a separate file, whoever writes it is responsible for the include in main_named_conf().
    """
    if zone_dir(os_id, os_like) == ZONE_DIR_DEBIAN:
        return "/etc/bind/named.conf.local"
    return "/etc/named.lab.conf"


def main_named_conf(os_id: str = "", os_like: str = "") -> str:
    """The distribution's own named.conf, which has to include remote_named_conf() where that is a separate file."""
    if zone_dir(os_id, os_like) == ZONE_DIR_DEBIAN:
        return "/etc/bind/named.conf"
    return "/etc/named.conf"


def include_needed(os_id: str = "", os_like: str = "") -> bool:
    """
    Whether main_named_conf() has to be given an include for remote_named_conf().

    False on Debian and Ubuntu, whose named.conf already includes named.conf.local: adding it again would be a
    duplicate include, which named refuses.
    """
    return remote_named_conf(os_id, os_like) != "/etc/bind/named.conf.local"


def zone_path(file_name: str, os_id: str = "", os_like: str = "") -> str:
    """The absolute path of a zone file on a host of this family, which is how a stanza must always name it."""
    return "{}/{}".format(zone_dir(os_id, os_like), file_name)


# The site a view master serves its site view for, one line naming it. Its presence is what makes a server a view
# master (view_named_conf()); a server without it serves forward_file() alone, as every master did before sites.
SITE_MARKER = "/etc/lab-dns-site"


def site_named_conf(os_id: str = "", os_like: str = "") -> str:
    """The file a view master declares its site view's zones in, beside remote_named_conf() for the external view."""
    if zone_dir(os_id, os_like) == ZONE_DIR_DEBIAN:
        return "/etc/bind/named.conf.lab-site"
    return "/etc/named.lab-site.conf"


# The distribution's own working and managed-keys directories for named, which it must be able to write, unlike the
# zone directory. Fedora-family IDs are matched before ID_LIKE, as _DEBIAN_IDS is.
_FEDORA_IDS = ("fedora", "rhel", "centos", "rocky", "almalinux", "amzn", "ol")


def named_dirs(os_id: str = "", os_like: str = "") -> Tuple[str, str]:
    """named's ("directory", "managed-keys-directory") on a host of this family, as its own package lays them out."""
    os_id, os_like = (os_id or "").lower(), (os_like or "").lower()
    if zone_dir(os_id, os_like) == ZONE_DIR_DEBIAN:
        return "/var/cache/bind", "/var/cache/bind"
    if os_id in _FEDORA_IDS or "fedora" in os_like or "rhel" in os_like:
        return "/var/named", "/var/named/dynamic"
    return "/var/lib/named", "/var/lib/named/dyn"


def view_named_conf(options: List[str], site_clients: List[str], os_id: str = "", os_like: str = "") -> str:
    """
    The whole named.conf of a view master: `options` (one statement per line) in its options block, then a "site"
    view for `site_clients` (address match list elements) with the zones of site_named_conf(), and an "external"
    view for everyone else with the zones of remote_named_conf(), which answers authoritatively and never recurses.

    BIND refuses a zone outside a view once any view exists, which is why the file is written whole rather than
    patched into the distribution's own: the default zones it ships are left out and BIND's built-in ones serve.
    """
    directory, keys_directory = named_dirs(os_id, os_like)
    lines = ["options {", '\tdirectory "{}";'.format(directory), '\tmanaged-keys-directory "{}";'.format(keys_directory)]
    lines += ["\t" + option.strip() for option in options]
    lines += [
        "};",
        'view "site" {',
        "\tmatch-clients {{ {} }};".format(" ".join("{};".format(c) for c in site_clients)),
        '\tinclude "{}";'.format(site_named_conf(os_id, os_like)),
        "};",
        'view "external" {',
        "\tmatch-clients { any; };",
        "\trecursion no;",
        "\tallow-recursion { none; };",
        "\tallow-query-cache { none; };",
        '\tinclude "{}";'.format(remote_named_conf(os_id, os_like)),
        "};",
    ]
    return "\n".join(lines) + "\n"


def stanza(zone: str, zone_file: str) -> str:
    """The named.conf declaration making this server a master of `zone`, served from `zone_file`."""
    return 'zone "{z}" {{ type master; file "{f}"; allow-update {{ none; }}; }};'.format(z=zone, f=zone_file)


def has_header(text: str) -> bool:
    """
    Whether zone file `text` has the SOA BIND needs to load it at all. Without one BIND rejects the whole zone, so
    every record in the file goes unanswered.
    """
    return any("SOA" in line.split() for line in text.splitlines())


def with_header(text: str, header: List[str]) -> str:
    """Zone file `text` with `header` in front of it, keeping every record it already holds."""
    return "\n".join(header) + "\n" + text.lstrip("\n")


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
