#!/usr/bin/env python3
# Part of lab-in-a-box — auxiliary services (DNS, HTTP, PXE/TFTP/DHCP).
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/services.py — lab-support services that run on the automation VM.

This is new ground (no bash equivalent beyond the DNS record-management
functions this module absorbs from lab_creation.py) — see MIGRATION_TODO.md
Phase 5 §5.4 for the full design write-up and the reasoning behind every
choice below.

Services:
  - DNSService  — record management only (add/remove A+PTR records, cluster
    service records). BIND itself is provisioned by the bash bootstrap
    (setup_lab_automation.sh) before any of this ever runs, so install()/
    configure()/enable() are no-ops here — there is nothing this layer needs
    to set up. Bodies moved unchanged from lab_creation.py; the public
    functions stay as thin back-compat wrappers there (9 addon scripts import
    them directly).
  - HTTPService — formalizes the existing /srv/www/htdocs/lab_creation share.
    Same story as DNS: already provisioned by the bootstrap, install()/
    configure() are no-ops; is_active() does a real check.
  - PXEService  — NEW capability, no prior behavior to preserve. Bundles
    TFTP + PXE boot-file serving + optional DHCP into ONE small container
    (dnsmasq does all three natively — no reason to run three daemons for
    one job), run via a podman-systemd Quadlet unit so it survives reboots.
    DHCP is a three-way choice, not a boolean, because "off" without a
    fallback would leave PXE clients unable to find a boot server at all
    if the network's real DHCP server doesn't already hand out PXE options:
      - "off"   — no DHCP role at all; the network's own DHCP server must
                  already supply next-server/boot-filename options itself.
      - "proxy" — dnsmasq runs as a PXE proxyDHCP server: leases/IPs still
                  come from the network's real DHCP server, dnsmasq only
                  answers the PXE-specific options. This is the recommended
                  default for "don't take over DHCP" while keeping PXE work.
      - "full"  — dnsmasq is the DHCP server (needs pxe_dhcp_range_start/end).
    Podman is already an automation-VM package (see
    setup_demo_server/setup_lab_automation.sh's zypper install line), so this
    adds no new dependency.
"""

import re
import shlex
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import dns_zone
from lab_creation import log, warn, die, ssh_run, clean_ssh_keys, ssh_reachable, detect_os
from kvm_host_profiles import read_os_release
from lab_schema_base import field_default

# This host's own BIND layout, which depends on its distribution family exactly as a remote master's does: the
# directory its named is allowed to read a zone from, and the file a zone is declared in.
_LOCAL_OS = read_os_release()
NAMED_ZONE_DIR = Path(dns_zone.zone_dir(_LOCAL_OS.get("ID", ""), _LOCAL_OS.get("ID_LIKE", "")))
NAMED_CONF = Path(dns_zone.local_named_conf(_LOCAL_OS.get("ID", ""), _LOCAL_OS.get("ID_LIKE", "")))

# Shell-quoting for the zone lines and named.conf stanzas written over SSH, which carry quotes and braces.
_quote = shlex.quote


def _header_command(path: str, header: list) -> str:
    """
    The shell command putting `header` in front of zone file `path` on a remote server when the file has no SOA,
    keeping its records and creating it when missing. It exits 0 when it added the header and 3 when the file already
    had one, inside a subshell so its exit stops no command it is chained with. dns_zone.has_header() is the same
    rule on this host: a line with SOA as one of its fields.
    """
    lines = " ".join(_quote(line) for line in header)
    return ("(grep -qw SOA {p} 2>/dev/null && exit 3; "
            "{{ printf '%s\\n' {lines}; cat {p} 2>/dev/null || true; }} > {p}.new && cat {p}.new > {p} && rm -f {p}.new)"
            ).format(p=_quote(path), lines=lines)

# Run on a server by DNSService.serve_site_view(), as one script: it writes `text` as named.conf, gives the site
# view the external view's zones when it has none, and restores the previous named.conf when named refuses the new
# one. Exits 0 when the server serves the site view, printing "changed" when it had to be configured for it.
_SERVE_SITE_VIEW = """set -u
conf={conf} ext={ext} site_conf={site_conf} marker={marker} site={site}
new=$(mktemp)
cat > "$new" <<'LAB_NAMED_CONF'
{text}LAB_NAMED_CONF
touch "$ext"
[ -f "$site_conf" ] || cp "$ext" "$site_conf"
if cmp -s "$new" "$conf" && [ "$(cat "$marker" 2>/dev/null)" = "$site" ]; then rm -f "$new"; exit 0; fi
named-checkconf "$new" || {{ rm -f "$new"; exit 2; }}
unit=named; systemctl cat named >/dev/null 2>&1 || unit=bind9
cp -p "$conf" "$conf.lab-previous"
cat "$new" > "$conf"; rm -f "$new"
printf '%s\\n' "$site" > "$marker"
if systemctl restart "$unit" && systemctl is-active --quiet "$unit"; then echo changed; exit 0; fi
cat "$conf.lab-previous" > "$conf"; rm -f "$marker"; systemctl restart "$unit"; exit 3
"""
_SERVE_SITE_VIEW_ERRORS = {2: "named-checkconf refused the configuration",
                           3: "named did not start with it, so the previous one was restored"}

# Serializes every DNS zone-file mutation (add_to_dns, del_from_dns, add_service_dns and add_dns_to_named_rr). The helpers read the
# whole zone file, change it and write it back, so two nodes writing the same zone file would lose an entry. One process-wide lock
# is enough, because this class runs in a single process.
_dns_lock = threading.Lock()


class AuxService(object):
    """
    Interface every lab-support service implements. Takes lab_setup_path
    uniformly (even services that don't need it, e.g. DNSService) so
    services.get() can construct any of them the same way.
    """

    name = None

    def __init__(self, lab_setup_path="/srv/www/htdocs/lab_creation"):
        self.lab_setup_path = lab_setup_path

    def install(self):
        raise NotImplementedError

    def configure(self, definition, config):
        raise NotImplementedError

    def enable(self):
        raise NotImplementedError

    def is_active(self):
        raise NotImplementedError


# ── DNS ──────────────────────────────────────────────────────────────────────

class DNSService(AuxService):
    """
    Record management for the BIND server already running on the automation
    VM (provisioned by the bash bootstrap, not by this class). See the
    module docstring for why install()/configure()/enable() are no-ops.
    """

    name = "dns"

    def install(self):
        pass

    def configure(self, definition, config):
        pass

    def enable(self):
        pass

    def is_active(self):
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "named"],
        ).returncode == 0

    # ── record management (moved from lab_creation.py) ──────────────────────

    def _remote(self, hostname, cmd, check=True, capture=False):
        return ssh_run(hostname, cmd, check=check, capture=capture)

    def _remote_dns_add(self, server, zone_file, record):
        # check=False: a secondary DNS server being unreachable must not
        # abort VM provisioning — mirrors bash's add_to_dns, whose remote
        # ssh calls (libs/lab_creation.bash) have no `||`/`set -e` and so
        # already tolerate this. The local zone file (below, in
        # add_to_dns/del_from_dns) plus local named restart remain the
        # authoritative, must-succeed operations.
        self._remote(server,
                      "grep -qF '{}' {} 2>/dev/null || echo '{}' >> {}".format(
                          record, zone_file, record, zone_file),
                      check=False)

    # ── masters: which DNS servers a write reaches ──────────────────────────

    # Whether each remote master answered, probed once per process. A master that is paused or destroyed would
    # otherwise cost an SSH connect per record, per node, on every write.
    _reachable_cache = {}

    # How hard the gate tries before giving up on a master. ssh_reachable() retries only a failure to connect, so
    # this waits for a server that is still booting or behind a momentary fault without waiting on one that is
    # paused or destroyed: with ConnectTimeout=5, an address nothing answers at all costs about 20 seconds, once
    # per process rather than once per record. A master down for longer than that is what report_zone() and
    # dns_sync.py are for — retrying is not a substitute for them.
    _REACHABLE_ATTEMPTS = 3
    _REACHABLE_DELAY = 2

    def _reachable(self, server: str) -> bool:
        """
        Whether `server` answers SSH, probed once per process and reported once when it does not.

        A server that does not answer is retried, since a cloud lab's DNS VM is routinely still booting when the
        first record is written. Once the retries are spent, the host's known_hosts entry is dropped and it is
        probed once more: a DNS VM recreated under a reused address otherwise fails every write without saying so,
        and that failure is a refusal rather than a timeout, so no amount of retrying reaches it.
        """
        if server not in self._reachable_cache:
            def probe(host):
                return self._remote(host, "true", check=False, capture=True)

            reachable = ssh_reachable(server, attempts=self._REACHABLE_ATTEMPTS,
                                      delay=self._REACHABLE_DELAY, probe=probe)
            if not reachable:
                clean_ssh_keys(server, server)
                reachable = ssh_reachable(server, attempts=1, probe=probe)
            self._reachable_cache[server] = reachable
            if not reachable:
                warn("- DNS server '{}' is not reachable: its zone is left untouched and will be brought back in "
                     "step by the next run that reaches it".format(server))
        return self._reachable_cache[server]

    # Each master's os-release, resolved once per operation and dropped afterwards, so a server recreated under a
    # different operating system is detected again by the next one. An SSH round trip per operation is cheap next
    # to writing a zone where that server's named cannot read it.
    _os_info = None

    def _resolve_families(self, servers) -> None:
        """Read the os-release of each of `servers` for the operation about to run."""
        self._os_info = {server: detect_os(server) for server in servers if server is not None}

    def _zone_path(self, server, file_name: str) -> str:
        """
        The absolute path of `file_name` on `server` (None for this host), which differs per distribution family.

        A family not resolved by this operation is read now rather than assumed, since the wrong directory means a
        zone that is declared, written and never answered.
        """
        if server is None:
            return str(NAMED_ZONE_DIR / file_name)
        info = (self._os_info or {}).get(server)
        if info is None:
            info = detect_os(server)
            if self._os_info is not None:
                self._os_info[server] = info
        return dns_zone.zone_path(file_name, info.get("id", ""), info.get("like", ""))

    def masters(self, remote_dns_servers=None) -> list:
        """
        The remote DNS masters a write should reach: those of `remote_dns_servers` that answer.

        The caller's list is built per run, never stored, so a cloud lab's own DNS VM joins it at its current
        address and simply is not in it while it does not exist.
        """
        return [s for s in (remote_dns_servers or []) if self._reachable(s)]

    # ── zones: a master holds one before any record is written to it ────────

    def _zone_declared(self, server, zone: str) -> bool:
        """Whether `server` (None for this host) already declares `zone` as a master."""
        pattern = 'zone "{}"'.format(zone)
        if server is None:
            return NAMED_CONF.exists() and pattern in NAMED_CONF.read_text()
        found = self._remote(server, "grep -rqF {} /etc/named.conf /etc/named.lab.conf /etc/bind/named.conf.local "
                                     "2>/dev/null".format(_quote(pattern)), check=False)
        return found.returncode == 0

    def ensure_zone(self, server, mydomain: str, mynet_reverse: str = "", sites=()) -> bool:
        """
        Make `server` (None for this host) a master of `mydomain`, and of its reverse zone when `mynet_reverse` is
        given, creating the zone file with the header BIND needs and declaring it in named.conf.

        `server` also holds the view file of each of `sites` (dns_zone.site_file()); one it lacks starts as a copy of
        its forward zone file, where every node is at its public address. A view master (view_site()) holds its own
        site's file too and serves it in its site view, together with the reverse zone.

        Returns True when a zone had to be created, which tells the caller this master holds none of the lab's
        records yet and needs sync_zone() rather than one appended line. Returns False when every zone was already
        there, which is the normal path and does nothing.

        A record appended to a zone that is not declared, or to a file with no SOA, is silently never answered:
        this is what stops that happening. A zone already declared over a file with no SOA, which BIND rejects
        whole, gets its header added in front of the records it holds and named is restarted; that alone does not
        count as created, since the records are already there.
        """
        created = False
        repaired = False
        wanted = [(mydomain, dns_zone.forward_file(mydomain), dns_zone.forward_header(mydomain))]
        if mynet_reverse:
            wanted.append((dns_zone.reverse_zone(mynet_reverse), dns_zone.reverse_file(mynet_reverse),
                           dns_zone.reverse_header(mydomain, mynet_reverse)))
        for zone, file_name, header in wanted:
            if self._zone_declared(server, zone):
                if self._add_missing_header(server, file_name, header):
                    log("- DNS server '{}' declares '{}' over a file with no SOA, which BIND rejects: added its "
                        "header".format(server or "this host", zone))
                    repaired = True
                continue
            log("- DNS server '{}' is not a master of '{}' yet: creating the zone".format(server or "this host", zone))
            if server is None:
                self._create_local_zone(zone, file_name, header)
            else:
                self._create_remote_zone(server, zone, file_name, header)
            created = True
        own = self.view_site(server)
        for site in sorted(set(sites) | ({own} if own else set())):
            created = self._ensure_site_file(server, mydomain, site) or created
        if own:
            served = [(mydomain, dns_zone.site_file(mydomain, own))]
            if mynet_reverse:
                served.append((dns_zone.reverse_zone(mynet_reverse), dns_zone.reverse_file(mynet_reverse)))
            for zone, file_name in served:
                created = self._declare_site_zone(server, zone, file_name) or created
        if created or repaired:
            self.restart_named(remote_servers=[server] if server else None)
        return created

    def _ensure_site_file(self, server, mydomain: str, site: str) -> bool:
        """
        Create `server`'s (None for this host) view file of `mydomain` for `site` as a copy of its forward zone file,
        when it has none. Returns True when it did.
        """
        forward, own = dns_zone.forward_file(mydomain), dns_zone.site_file(mydomain, site)
        if server is None:
            path = NAMED_ZONE_DIR / own
            if path.exists():
                return False
            path.write_text(self._local_text(forward))
            path.chmod(0o644)
        else:
            result = self._remote(server, "[ -f {s} ] && exit 3; cp {f} {s}".format(
                f=_quote(self._zone_path(server, forward)), s=_quote(self._zone_path(server, own))),
                check=False, capture=True)
            if result.returncode != 0:
                return False
        log("- DNS server '{}' has no view of '{}' for site '{}' yet: created it from the external one".format(
            server or "this host", mydomain, site))
        return True

    def _declare_site_zone(self, server: str, zone: str, file_name: str) -> bool:
        """
        Declare `zone` in view master `server`'s site view, served from `file_name`, replacing a declaration of it
        from any other file. Returns True when it changed the declaration.
        """
        info = (self._os_info or {}).get(server) or detect_os(server)
        conf = _quote(dns_zone.site_named_conf(info.get("id", ""), info.get("like", "")))
        line = _quote(dns_zone.stanza(zone, self._zone_path(server, file_name)))
        result = self._remote(server, (
            "grep -qxF {l} {c} && exit 3; grep -vF {z} {c} > {c}.new; printf '%s\\n' {l} >> {c}.new; "
            "cat {c}.new > {c}; rm -f {c}.new").format(l=line, c=conf, z=_quote('zone "{}" '.format(zone))),
            check=False, capture=True)
        return result.returncode == 0

    def _add_missing_header(self, server, file_name: str, header: list) -> bool:
        """
        Put `header` in front of `server`'s zone file `file_name` (None for this host) when the file has no SOA,
        keeping every record in it. Returns True when it added one.
        """
        if server is None:
            path = NAMED_ZONE_DIR / file_name
            text = path.read_text() if path.exists() else ""
            if dns_zone.has_header(text):
                return False
            path.write_text(dns_zone.with_header(text, header))
            return True
        result = self._remote(server, _header_command(self._zone_path(server, file_name), header),
                              check=False, capture=True)
        return result.returncode == 0

    def _create_local_zone(self, zone: str, file_name: str, header: list) -> None:
        """Write the zone file under NAMED_ZONE_DIR and declare it in this host's named.conf."""
        path = NAMED_ZONE_DIR / file_name
        if not path.exists():
            path.write_text("")
            path.chmod(0o644)
        self._add_missing_header(None, file_name, header)
        with NAMED_CONF.open("a") as conf:
            conf.write("\n" + dns_zone.stanza(zone, str(NAMED_ZONE_DIR / file_name)) + "\n")

    def _create_remote_zone(self, server: str, zone: str, file_name: str, header: list) -> None:
        """
        The counterpart of _create_local_zone() over SSH, writing the zone where that server's own named is
        allowed to read it and declaring it in the file that server's distribution keeps local zones in.

        The family comes from the server's own os-release, so a master is never given the layout of a different
        distribution: a zone written to a directory its named cannot read is declared, written, and silently never
        answered.
        """
        info = (self._os_info or {}).get(server) or detect_os(server)
        os_id, os_like = info.get("id", ""), info.get("like", "")
        path = dns_zone.zone_path(file_name, os_id, os_like)
        conf = dns_zone.remote_named_conf(os_id, os_like)
        main = dns_zone.main_named_conf(os_id, os_like)
        commands = [
            "mkdir -p {}".format(_quote(dns_zone.zone_dir(os_id, os_like))),
            _header_command(path, header),
            "printf '%s\\n' {stanza} >> {conf}".format(
                stanza=_quote(dns_zone.stanza(zone, path)), conf=_quote(conf)),
        ]
        # Where the stanzas go in a file of this project's own, that file has to be included from the
        # distribution's named.conf; where they go in the distribution's own local file, it already includes it and
        # a second include is one named refuses.
        if dns_zone.include_needed(os_id, os_like):
            commands.append("grep -qF {c} {m} || echo 'include \"{f}\";' >> {m}".format(
                c=_quote(conf), m=_quote(main), f=conf))
        self._remote(server, "; ".join(commands), check=False)

    # ── sites: one forward zone file per view ───────────────────────────────

    # The site each remote master serves a site view for (dns_zone.SITE_MARKER), read once per process; "" for a
    # master that serves the external view alone.
    _view_sites = {}

    def view_site(self, server) -> str:
        """The site `server` serves a site view for; "" for this host and for a master without views."""
        if server is None:
            return ""
        if server not in self._view_sites:
            result = self._remote(server, "cat {} 2>/dev/null".format(dns_zone.SITE_MARKER), check=False,
                                  capture=True)
            self._view_sites[server] = (result.stdout or "").strip() if result.returncode == 0 else ""
        return self._view_sites[server]

    def sites(self, mydomain: str, servers=(), extra=()) -> list:
        """
        Every site with a view file of `mydomain`: those this host holds, those the view masters among `servers`
        serve, and `extra`. Every master holds the file of each, whether or not it serves it.
        """
        held = {dns_zone.site_of_file(mydomain, path.name)
                for path in NAMED_ZONE_DIR.glob(dns_zone.site_file(mydomain, "*"))}
        held |= {self.view_site(server) for server in servers}
        held |= set(extra)
        return sorted(site for site in held if site)

    @staticmethod
    def _forward_files(mydomain: str, sites) -> list:
        """The forward zone files of `mydomain`: the external view's, then one per site of `sites`."""
        return [dns_zone.forward_file(mydomain)] + [dns_zone.site_file(mydomain, site) for site in sites]

    @staticmethod
    def _local_text(file_name: str) -> str:
        """This host's copy of zone file `file_name`; empty when it has none."""
        path = NAMED_ZONE_DIR / file_name
        return path.read_text() if path.exists() else ""

    def serve_site_view(self, server: str, site: str, site_clients, options) -> bool:
        """
        Make `server` a view master for `site`: its named.conf becomes dns_zone.view_named_conf() with `options` and
        `site_clients`, and dns_zone.SITE_MARKER names `site`. A site view it has no zones for yet starts with every
        zone of its external view, from the same files, so no zone it serves goes unanswered; ensure_zone() then
        moves a forward zone to its site's file once a record of it is written.

        Nothing is written and named is not restarted when the server already has this configuration. One that
        named-checkconf refuses, or that named does not start with, is not left in place: the previous named.conf is
        restored. Returns whether `server` serves the site view.
        """
        if self._view_sites.get(server) == site:
            return True
        info = detect_os(server)
        os_id, os_like = info.get("id", ""), info.get("like", "")
        script = _SERVE_SITE_VIEW.format(
            conf=_quote(dns_zone.main_named_conf(os_id, os_like)),
            ext=_quote(dns_zone.remote_named_conf(os_id, os_like)),
            site_conf=_quote(dns_zone.site_named_conf(os_id, os_like)),
            marker=_quote(dns_zone.SITE_MARKER), site=_quote(site),
            text=dns_zone.view_named_conf(list(options), list(site_clients), os_id, os_like))
        result = ssh_run(server, "bash -s", input_text=script, check=False, capture=True)
        if result.returncode != 0:
            warn("- DNS server '{}' was NOT given the site view of '{}' ({}), so it keeps answering every client "
                 "with public addresses: {}".format(server, site, _SERVE_SITE_VIEW_ERRORS.get(
                     result.returncode, "exit {}".format(result.returncode)),
                     " ".join((result.stdout or result.stderr or "").split())))
            self._view_sites[server] = ""
            return False
        if "changed" in (result.stdout or ""):
            log("- DNS server '{}' now answers clients of site '{}' with that site's private addresses".format(
                server, site))
        self._view_sites[server] = site
        return True

    # ── sync: every master answers the same records ─────────────────────────

    def zone_records(self, server, file_name: str) -> set:
        """The comparable records `server` (None for this host) holds in `file_name`; empty when it has no such zone."""
        if server is None:
            path = NAMED_ZONE_DIR / file_name
            return dns_zone.records(path.read_text()) if path.exists() else set()
        result = self._remote(server, "cat {} 2>/dev/null".format(_quote(self._zone_path(server, file_name))),
                              check=False, capture=True)
        return dns_zone.records(result.stdout if result.returncode == 0 else "")

    def _zone_files(self, mydomain: str, mynet_reverse: str = "") -> list:
        """
        The zone files of `mydomain`: its forward zone, the view file of every site this host holds one for, and its
        reverse zone when the lab states one.
        """
        files = self._forward_files(mydomain, self.sites(mydomain))
        if mynet_reverse:
            files.append(dns_zone.reverse_file(mynet_reverse))
        return files

    def _zone_pairs(self, mydomain: str, mynet_reverse: str = "") -> list:
        """The zone files of `mydomain` as (file name, zone name), for anything that has to name the zone itself."""
        pairs = [(dns_zone.forward_file(mydomain), mydomain)]
        if mynet_reverse:
            pairs.append((dns_zone.reverse_file(mynet_reverse), dns_zone.reverse_zone(mynet_reverse)))
        return pairs

    def _run_on(self, server, cmd: str):
        """Run a shell command on `server`, or on this host when it is None, without raising."""
        if server is None:
            return subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True)
        return self._remote(server, cmd, check=False, capture=True)

    def zone_loads(self, server, zone: str, file_name: str) -> tuple:
        """
        Whether `server` (None for this host) actually answers for `zone`, as (loads, detail).

        Comparing records deliberately ignores a zone's header, because the SOA and the NS address are per server,
        so a master whose SOA is damaged or missing agrees perfectly with the others while serving nothing.

        The server's own named is asked for the zone's SOA, which is the only question that matters. A zone can be
        valid on disk and still never answered: Debian and Ubuntu confine named with AppArmor, and a zone file
        outside its profile is denied however correctly it is declared. named-checkzone answers a different
        question, because it reads the file as root and unconfined, so it is the fallback for a server with no dig
        or nothing listening on 53.

        (True, "") when the server holds no such zone file, which _compare_zone() reports as the records it is
        missing instead, and when neither tool is available to ask.
        """
        path = self._zone_path(server, file_name)
        asked = self._run_on(server, (
            "[ -f {p} ] || exit 126; command -v dig >/dev/null 2>&1 || exit 127; "
            "dig +time=3 +tries=1 +short @127.0.0.1 SOA {z}").format(p=_quote(path), z=_quote(zone)))
        if asked.returncode == 126:
            return True, ""
        if asked.returncode == 0:
            if (asked.stdout or "").strip():
                return True, ""
            return False, ("its own named is running but answers no SOA for the zone, so the zone is declared and "
                           "written but not served")
        # dig is absent, or nothing is listening to ask: fall back to whether the file itself is a valid zone.
        checked = self._run_on(server, (
            "command -v named-checkzone >/dev/null 2>&1 || exit 127; [ -f {p} ] || exit 126; "
            "named-checkzone {z} {p} 2>&1").format(p=_quote(path), z=_quote(zone)))
        if checked.returncode in (0, 126, 127):
            return True, ""
        detail = " ".join(line.strip() for line in (checked.stdout or "").splitlines() if line.strip())
        return False, detail or "named-checkzone exited {}".format(checked.returncode)

    def validate_zones(self, mydomain: str, mynet_reverse: str = "", remote_dns_servers=None) -> list:
        """
        One message per reachable master of `mydomain` whose own copy of a zone does not load, which is a fault no
        record comparison can see. Empty when every master's zones load.
        """
        messages = []
        for server in [None] + self.masters(remote_dns_servers):
            for file_name, zone in self._zone_pairs(mydomain, mynet_reverse):
                loads, detail = self.zone_loads(server, zone, file_name)
                if not loads:
                    messages.append("DNS server '{}' cannot load its copy of '{}', so it answers nothing for that "
                                    "zone however well its records agree with the other masters: {}".format(
                                        server or "this host", zone, detail))
        return messages

    def _compare_zone(self, mydomain: str, mynet_reverse: str = "", remote_dns_servers=None) -> list:
        """
        What the reachable masters of `mydomain` disagree on, as a list of
        (zone file, server, records missing from it, records only it holds).

        `server` is None for this host. Both sets are empty for a master that already agrees, and such a master is
        still listed, so a caller can report how many were compared.
        """
        servers = [None] + self.masters(remote_dns_servers)
        out = []
        for file_name in self._zone_files(mydomain, mynet_reverse):
            held = {server: self.zone_records(server, file_name) for server in servers}
            union = set().union(*held.values())
            for server, records in held.items():
                others = set().union(*(r for s, r in held.items() if s != server)) if len(held) > 1 else records
                out.append((file_name, server, union - records, records - others))
        return out

    @staticmethod
    def _difference_messages(comparison: list) -> list:
        """One message per real difference in a _compare_zone() result, in the order it was compared."""
        messages = []
        for file_name, server, missing, alone in comparison:
            name = server or "this host"
            if missing:
                messages.append("DNS server '{}' is missing {} record(s) of '{}': {}".format(
                    name, len(missing), file_name, ", ".join(sorted(missing))))
            if alone:
                messages.append("DNS server '{}' holds {} record(s) of '{}' no other master has, which is either a "
                                "record the others have not been written yet or one deleted from them: {}".format(
                                    name, len(alone), file_name, ", ".join(sorted(alone))))
        return messages

    def report_zone(self, mydomain: str, mynet_reverse: str = "", remote_dns_servers=None) -> list:
        """
        What is wrong across the reachable masters of `mydomain`, reported and returned as one message each,
        changing nothing. Empty when every master's zones load and hold the same records.

        A zone that does not load is reported first, because a master that serves nothing is a worse fault than a
        record it is missing, and reconciling records would otherwise report success over it.
        """
        with _dns_lock:
            self._resolve_families(self.masters(remote_dns_servers))
            messages = self.validate_zones(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)
            messages += self._difference_messages(
                self._compare_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers))
        for message in messages:
            warn("- " + message)
        return messages

    def sync_zone(self, mydomain: str, mynet_reverse: str = "", remote_dns_servers=None) -> list:
        """
        Bring every reachable master of `mydomain` into step and report what differed, returning one message per
        difference found (empty when they already agreed).

        Records are only ever added: a record one master holds alone is either not written to the others yet or
        deleted from them, and the record sets cannot tell those apart. Adding is correct for the first and
        harmless for the second, while removing would silently undo a deletion, so what one master holds alone is
        reported for pruning rather than acted on. prune_zone() is the deliberate counterpart.
        """
        with _dns_lock:
            self._resolve_families(self.masters(remote_dns_servers))
            messages = self.validate_zones(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)
            comparison = self._compare_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)
            messages += self._difference_messages(comparison)
            wrote = False
            for file_name, server, missing, _alone in comparison:
                if missing:
                    self._write_records(server, file_name, sorted(missing))
                    wrote = True
            if wrote:
                self.restart_named(remote_servers=self.masters(remote_dns_servers) or None)
        for message in messages:
            warn("- " + message)
        return messages

    def _write_records(self, server, file_name: str, records: list) -> None:
        """Append `records` to `server`'s (None for this host) copy of `file_name`."""
        if server is None:
            path = NAMED_ZONE_DIR / file_name
            for record in records:
                self._dns_append_line(path, record)
            return
        for record in records:
            self._remote_dns_add(server, self._zone_path(server, file_name), record)

    def prune_zone(self, mydomain: str, mynet_reverse: str = "", remote_dns_servers=None) -> list:
        """
        Remove, from every reachable master of `mydomain`, each record no other master holds, and report what was
        removed.

        This is what sync_zone() deliberately does not do. A record held by one master alone is as likely to be one
        the others have not been written yet as one deleted from them, so this is only correct when a human has
        read sync_zone()'s report and decided which it is.
        """
        messages = []
        with _dns_lock:
            self._resolve_families(self.masters(remote_dns_servers))
            comparison = self._compare_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)
            # One master on its own has nothing to compare against, so every record it holds it holds alone.
            if len({server for _f, server, _m, _a in comparison}) < 2:
                return []
            for file_name, server, _missing, alone in comparison:
                for record in sorted(alone):
                    messages.append("removed '{}' from DNS server '{}' ({})".format(
                        record, server or "this host", file_name))
                    if server is None:
                        self._dns_remove_record(NAMED_ZONE_DIR / file_name, record)
                    else:
                        self._remote_dns_remove_record(server, self._zone_path(server, file_name), record)
            if messages:
                self.restart_named(remote_servers=self.masters(remote_dns_servers) or None)
        for message in messages:
            log("- " + message)
        return messages

    def _dns_add_line(self, zone_file, record):
        """
        Append `record` to zone_file unless a line exactly equal to it
        already exists. Exact-line matching (not substring) to avoid the
        node1/node10 false-positive/false-skip risks this project's
        hostnames create — see the original lab_creation.py docstring
        history for the two related bugs (bash and an earlier python draft)
        this precision avoids.
        """
        zone_file = Path(zone_file)
        zone_file.touch()
        lines = zone_file.read_text().splitlines()
        if record not in lines:
            zone_file.write_text("\n".join(lines + [record]) + "\n")

    def _dns_remove_line(self, zone_file, record):
        """Remove any line exactly equal to `record` from zone_file."""
        zone_file = Path(zone_file)
        if zone_file.exists():
            lines = [l for l in zone_file.read_text().splitlines() if l != record]
            zone_file.write_text("\n".join(lines) + "\n")

    def _dns_remove_service_lines(self, zone_file, ip):
        """Remove every service record (`<entry>\tIN A  <ip>`, as add_service_dns writes it) that points at `ip`."""
        zone_file = Path(zone_file)
        if ip and zone_file.exists():
            suffix = "\tIN A  {}".format(ip)
            lines = [l for l in zone_file.read_text().splitlines() if not l.endswith(suffix)]
            zone_file.write_text("\n".join(lines) + "\n")

    @staticmethod
    def _node_pattern(short: str) -> str:
        """
        Matches node `short`'s own A line (dns_zone.node_record()) as a Python and a sed -E pattern, and never a service
        record, which is tab-separated. A host name holds no character either reads specially.
        """
        return "^{} +IN +A +".format(short)

    def _dns_set_node(self, zone_file, short: str, record: str) -> None:
        """Make `record` node `short`'s only own A line in zone_file."""
        zone_file = Path(zone_file)
        zone_file.touch()
        matcher = re.compile(self._node_pattern(short))
        lines = [line for line in zone_file.read_text().splitlines() if not matcher.match(line)]
        zone_file.write_text("\n".join(lines + [record]) + "\n")

    def _remote_set_node(self, server, zone_file: str, short: str, record: str) -> None:
        """The remote counterpart of _dns_set_node()."""
        self._remote(server, "sed -Ei {p} {f}; echo {r} >> {f}".format(
            p=_quote("/{}/d".format(self._node_pattern(short))), f=_quote(zone_file), r=_quote(record)),
            check=False)

    def _dns_retarget_services(self, zone_file, old: str, new: str) -> None:
        """Point every service record of zone_file at `old` (as add_service_dns writes them) at `new` instead."""
        zone_file = Path(zone_file)
        if old and old != new and zone_file.exists():
            suffix = "\tIN A  {}".format(old)
            lines = [line[:-len(old)] + new if line.endswith(suffix) else line
                     for line in zone_file.read_text().splitlines()]
            zone_file.write_text("\n".join(lines) + "\n")

    def _remote_retarget_services(self, server, zone_file: str, old: str, new: str) -> None:
        """The remote counterpart of _dns_retarget_services()."""
        if old and old != new:
            self._remote(server, "sed -i {} {}".format(_quote("s/\\tIN A  {}$/\\tIN A  {}/".format(
                old.replace(".", "\\."), new)), _quote(zone_file)), check=False)

    def _dns_append_line(self, zone_file, record):
        zone_file = Path(zone_file)
        zone_file.touch()
        with zone_file.open("a") as f:
            f.write(record + "\n")

    def _dns_remove_ptr_for_octet(self, zone_file, last_octet):
        """
        Remove any existing PTR line for `last_octet`, whichever hostname it points to.

        _dns_add_line() skips only an exact duplicate. An IP that still has a PTR record for an older hostname would then carry two PTR
        records. BIND serves both, and the node's own reverse lookup can return the older name.

        The octet is matched as the first whitespace-delimited token of the line, not as a substring. Removing PTR '14' therefore does
        not also remove '142'.
        """
        zone_file = Path(zone_file)
        if not zone_file.exists():
            return
        lines = [l for l in zone_file.read_text().splitlines() if l.split()[:1] != [last_octet]]
        zone_file.write_text("\n".join(lines) + "\n")

    def _remote_dns_remove_ptr_for_octet(self, server, zone_file, last_octet):
        """Remote-server counterpart to _dns_remove_ptr_for_octet — same
        octet-as-first-token precision, via sed anchored at line start so it
        can't match '142' while removing '14'."""
        self._remote(server, "sed -i '/^{}[[:space:]]/d' {}".format(last_octet, zone_file), check=False)

    def _remote_dns_remove_record(self, server, zone_file, record):
        """Remove `record` from `server`'s zone file, matched as a whole line so node1 cannot delete node10."""
        self._remote(server, "sed -Ei {} {}".format(
            _quote("\\|{}|d".format(dns_zone.pattern(record))), zone_file), check=False)

    def _dns_remove_record(self, zone_file, record):
        """The local counterpart of _remote_dns_remove_record(), matching the same whitespace-tolerant line."""
        path = Path(zone_file)
        if not path.exists():
            return
        matcher = re.compile(dns_zone.pattern(record).replace("[[:space:]]", r"\s"))
        kept = [line for line in path.read_text().splitlines() if not matcher.match(line)]
        path.write_text("\n".join(kept) + "\n")

    def restart_named(self, remote_servers=None):
        """Restart the local BIND named service and optionally on remote servers."""
        for server in (remote_servers or []):
            self._remote(server, "systemctl restart named", check=False)
        subprocess.run(["systemctl", "restart", "named"], check=False)

    def _hosts_file(self):
        """
        Return lab_creation.cfg's LAB_HOSTS_FILE (for example /etc/hosts), or None. When it is set, every VM registered in DNS also gets
        a line in that file. This serves automation nodes that do not run BIND, so they can reach each VM by name. That includes a cloud
        VM, whose IP is known only after it exists.
        """
        try:
            import primary
            path = (primary.load_config() or {}).get("LAB_HOSTS_FILE", "")
        except (Exception, SystemExit):  # no/invalid lab_creation.cfg: feature off
            return None
        return Path(path) if path else None

    def _hosts_line(self, vm_name, myip):
        return "{}  {} {}  # lab-in-a-box".format(myip, vm_name, vm_name.split(".")[0])

    def _update_hosts_file(self, vm_name, myip=None):
        """Replace (or with myip=None, remove) vm_name's LAB_HOSTS_FILE line."""
        path = self._hosts_file()
        if path is None:
            return
        suffix = "  {} {}  # lab-in-a-box".format(vm_name, vm_name.split(".")[0])
        lines = path.read_text().splitlines() if path.exists() else []
        lines = [l for l in lines if not l.endswith(suffix)]
        if myip:
            lines.append(self._hosts_line(vm_name, myip))
        path.write_text("\n".join(lines) + "\n")

    def add_to_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None, site="", private_ip=""):
        """
        Add forward (A) and reverse (PTR) DNS records for a VM. Without `mynet_reverse` only the A record is added.

        The node's A record is at `myip` in the external view's file and in every site's (dns_zone.site_file()),
        except in the file of its own `site`, where it is at `private_ip` when one is given. A node has one A record
        per file, so an earlier one under its name is replaced, and the service records that pointed at the
        address it replaces point at the new one.
        """
        log("Adding DNS entry for '{}' → {}".format(vm_name, myip))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)
        own_file   = dns_zone.site_file(mydomain, site) if site and private_ip else ""

        rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

        with _dns_lock:
            # Every master holds the zone before a record is appended to it, and one that had to have it created
            # holds none of this lab's earlier records: it is brought into step with the others rather than left
            # with the one record below. A master that does not answer is left out entirely. Under the same lock as
            # the records, because creating a zone writes the very file the appends below read and rewrite.
            servers = self.masters(remote_dns_servers)
            self._resolve_families(servers)
            sites = self.sites(mydomain, servers, [site] if own_file else [])
            resync = self.ensure_zone(None, mydomain, mynet_reverse, sites=sites)
            for server in servers:
                resync = self.ensure_zone(server, mydomain, mynet_reverse, sites=sites) or resync

            # file -> (the address the node had there, the address it has now), read from this host's copy, which
            # every master's matches.
            addresses = {
                file_name: (dns_zone.node_address(self._local_text(file_name), short),
                            private_ip if file_name == own_file else myip)
                for file_name in self._forward_files(mydomain, sites)}

            for server in servers:
                # A master's own zone directory, not this host's: the two differ between distribution families,
                # and a record written to the wrong one is never answered.
                if mynet_reverse:
                    remote_rev = self._zone_path(server, dns_zone.reverse_file(mynet_reverse))
                    self._remote_dns_remove_ptr_for_octet(server, remote_rev, last_octet)
                for file_name, (old, new) in addresses.items():
                    path = self._zone_path(server, file_name)
                    self._remote_set_node(server, path, short, dns_zone.node_record(short, new))
                    self._remote_retarget_services(server, path, old, new)
                if mynet_reverse:
                    self._remote_dns_add(server, remote_rev, ptr_record)
                self._remote(server, "systemctl restart named", check=False)

            # Drop any existing PTR record for this IP first. Each IP keeps one canonical PTR record.
            if mynet_reverse:
                self._dns_remove_ptr_for_octet(rev_file, last_octet)
            for file_name, (old, new) in addresses.items():
                self._dns_set_node(NAMED_ZONE_DIR / file_name, short, dns_zone.node_record(short, new))
                self._dns_retarget_services(NAMED_ZONE_DIR / file_name, old, new)
            if mynet_reverse:
                self._dns_add_line(rev_file, ptr_record)
            self._update_hosts_file(vm_name, myip)
            self.restart_named()

        if resync:
            self.sync_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)

    def del_from_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
        """
        Remove a VM's forward and reverse DNS records and every service record pointing at it, from the external
        view's file and every site's. Without `mynet_reverse` the PTR record is left alone.
        """
        log("Removing DNS entry for '{}'".format(vm_name))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

        rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

        # A removal that does not reach a master cannot be repaired later: sync_zone() only ever adds records, since
        # a record one master holds alone is indistinguishable from one not yet written to the others. So an
        # unreachable master is reported here by name, rather than passing silently as an append would.
        with _dns_lock:
            servers = self.masters(remote_dns_servers)
            for server in (remote_dns_servers or []):
                if server not in servers:
                    warn("- '{}' was NOT removed from DNS server '{}', which did not answer: that server still "
                         "resolves it, and no later run will remove it. Re-run this once it is reachable, or prune "
                         "it with dns_sync.py --prune".format(vm_name, server))

            self._resolve_families(servers)
            # The node's address in each file, read before anything is removed: in its own site's file it is an
            # address the caller does not pass.
            addresses = {file_name: dns_zone.node_address(self._local_text(file_name), short) or myip
                         for file_name in self._forward_files(mydomain, self.sites(mydomain, servers))}
            node_line = _quote("/{}/d".format(self._node_pattern(short)))
            for server in servers:
                # check=False: same non-fatal-secondary-server rationale as
                # _remote_dns_add above.
                remote_rev = self._zone_path(server, dns_zone.reverse_file(mynet_reverse)) if mynet_reverse else ""
                if mynet_reverse:
                    self._remote(server, "sed '/{}/d' -i {}".format(ptr_record, remote_rev), check=False)
                for file_name, address in addresses.items():
                    path = self._zone_path(server, file_name)
                    self._remote(server, "sed -Ei {} {}".format(node_line, path), check=False)
                    self._remote(server, "sed '/\\tIN A  {}$/d' -i {}".format(
                        address.replace(".", "\\."), path), check=False)
                self._remote(server, "systemctl restart named", check=False)

            if mynet_reverse:
                self._dns_remove_line(rev_file, ptr_record)
            matcher = re.compile(self._node_pattern(short))
            for file_name, address in addresses.items():
                path = NAMED_ZONE_DIR / file_name
                if path.exists():
                    path.write_text("\n".join(line for line in path.read_text().splitlines()
                                              if not matcher.match(line)) + "\n")
                self._dns_remove_service_lines(path, address)
            self._update_hosts_file(vm_name)
            self.restart_named()

    def add_service_dns(self, definition, clu_name, clu_type, dns_entry, mydomain, remote_dns_servers=None):
        """Add round-robin A records for a cluster service DNS entry."""
        nodes = definition.get("nodes", {})
        default_role = field_default("nodes", "KUBERNETES_NODE_TYPE")

        agent_nodes = [
            (name, cfg["myip"])
            for name, cfg in nodes.items()
            if (cfg.get("KUBERNETES_NODE_TYPE") or default_role) == "agent" and cfg.get("kcluster") == clu_name
            and "myip" in cfg
        ]

        record_targets = agent_nodes or [
            (name, cfg["myip"])
            for name, cfg in nodes.items()
            if cfg.get("kcluster") == clu_name and "myip" in cfg
        ]

        if not record_targets:
            # A cloud-backend node (AWS, etc.) never gets a static 'myip' in the lab JSON — the provider
            # assigns its address, only known at runtime (see _cloud_no_ip() in backends.py). Fall back to
            # asking each cluster node's own backend for its CURRENT live IP — the same resolution the
            # node's own per-node DNS record and SSH access already use — instead of giving up just because
            # the static-myip path has nothing to read. Found live 2026-11-06: ollama's and open_webui's own
            # ingress hostnames on the AWS-backed "ai-gpu" cluster never got a DNS record for exactly this
            # reason, left unnoticed because this only warns, never dies.
            import backends
            import primary
            config = primary.load_config()
            record_targets = [
                (name, ip) for name, ip in (
                    (name, backends.get_backend(definition, config, name, for_existing=True).get_ip(name))
                    for name, cfg in nodes.items() if cfg.get("kcluster") == clu_name
                ) if ip
            ]
            if not record_targets:
                warn("DNS '{}' NOT added: no node of cluster '{}' has a 'myip', and none of its "
                     "backend(s) could resolve a live IP either".format(dns_entry, clu_name))
                return
        msg = "agent" if agent_nodes else "all"
        log("DNS '{}' added pointing to {} nodes of cluster '{}' ({})".format(
            dns_entry, msg, clu_name, ", ".join(ip for _n, ip in record_targets)))

        with _dns_lock:
            servers = self.masters(remote_dns_servers)
            self._resolve_families(servers)
            sites = self.sites(mydomain, servers)
            resync = self.ensure_zone(None, mydomain, sites=sites)
            for server in servers:
                resync = self.ensure_zone(server, mydomain, sites=sites) or resync
            # Each file points the service at the address that file gives the node, so a site's view of it is the
            # node's private address there.
            for file_name in self._forward_files(mydomain, sites):
                zone_file = NAMED_ZONE_DIR / file_name
                text = self._local_text(file_name)
                for name, myip in record_targets:
                    ip = dns_zone.node_address(text, name.split(".")[0]) or myip
                    record = "{}\tIN A  {}".format(dns_entry, ip)
                    for server in servers:
                        # check=False: same non-fatal-secondary-server rationale as
                        # DNSService.add_to_dns above.
                        remote_zone = self._zone_path(server, file_name)
                        self._remote(server, "sed '/{}\tIN A  {}/d' -i {}".format(
                            dns_entry, ip, remote_zone), check=False)
                        self._remote(server, "echo -e '{}' >> {}".format(record, remote_zone), check=False)
                    self._dns_remove_line(zone_file, record)
                    self._dns_append_line(zone_file, record)
            for server in servers:
                self._remote(server, "systemctl restart named", check=False)

            self.restart_named()

        if resync:
            self.sync_zone(mydomain, remote_dns_servers=remote_dns_servers)

    def add_dns_to_named_rr(self, definition, dns_entry, node_name, mydomain, remote_dns_servers=None):
        """Add a single round-robin A record (dns_entry -> node_name's own myip)."""
        myip = (definition.get("nodes", {}).get(node_name, {}) or {}).get("myip", "")
        short = node_name.split(".")[0]

        with _dns_lock:
            servers = self.masters(remote_dns_servers)
            self._resolve_families(servers)
            sites = self.sites(mydomain, servers)
            resync = self.ensure_zone(None, mydomain, sites=sites)
            for server in servers:
                resync = self.ensure_zone(server, mydomain, sites=sites) or resync

            # Each file's record points at the address that file gives the node, as add_service_dns() does.
            records = {file_name: "{}\tIN A  {}".format(
                dns_entry, dns_zone.node_address(self._local_text(file_name), short) or myip)
                for file_name in self._forward_files(mydomain, sites)}
            if not resync and all(record in self._local_text(file_name).splitlines()
                                  for file_name, record in records.items()):
                log("- DNS entry \"{} → {}\" already correct, skipping".format(dns_entry, myip))
                return

            log("- add DNS entry \"{}.{}\"".format(dns_entry, mydomain))

            for server in servers:
                for file_name, record in records.items():
                    self._remote_dns_add(server, self._zone_path(server, file_name), record)
                self._remote(server, "systemctl restart named", check=False)

            for file_name, record in records.items():
                self._dns_add_line(NAMED_ZONE_DIR / file_name, record)

        if resync:
            self.sync_zone(mydomain, remote_dns_servers=remote_dns_servers)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class HTTPService(AuxService):
    """
    Formalizes the existing /srv/www/htdocs/lab_creation share (ignition/
    combustion/cloud-init/install_iso files) — already provisioned and
    started by the bash bootstrap, same story as DNSService.
    """

    name = "http"

    def install(self):
        pass

    def configure(self, definition, config):
        pass

    def enable(self):
        pass

    def is_active(self):
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "apache2"],
        ).returncode == 0


# ── PXE / TFTP / (optional) DHCP ────────────────────────────────────────────

_PXE_CONTAINERFILE = """\
FROM registry.opensuse.org/opensuse/leap:15.6
RUN zypper --non-interactive install --no-recommends dnsmasq && zypper clean --all
ENTRYPOINT ["dnsmasq", "--no-daemon", "--conf-file=/etc/dnsmasq.conf"]
"""

_PXE_IMAGE = "lab-pxe-dnsmasq"
_PXE_QUADLET_PATH = Path("/etc/containers/systemd/lab-pxe.container")
_PXE_UNIT_NAME = "lab-pxe.service"

# iPXE's own prebuilt UEFI firmware binary — dnsmasq's ipxe-uefi-mode stage-1
# handout (see _dnsmasq_conf()). Fetched at configure() time rather than
# vendored in the repo: a compiled binary with its own upstream release
# cadence, not source this project should track.
_IPXE_EFI_URL = "https://boot.ipxe.org/x86_64-efi/ipxe.efi"


def _dnsmasq_conf(cfg, tftp_root, nodes=None):
    """
    Build the dnsmasq.conf content for the PXE container. Pure function —
    no I/O — so its output is unit-testable without podman/root.

    cfg keys (all from the lab JSON's optional "pxe" section, all optional):
      pxe_bridge            : interface dnsmasq binds to (default "br0")
      pxe_dhcp_mode         : "off" (default) | "proxy" | "full"
      pxe_dhcp_range_start/_end : required when pxe_dhcp_mode == "full"
      pxe_dhcp_lease        : lease time for "full" mode (default "12h")
      pxe_dhcp_proxy_subnet : required when pxe_dhcp_mode == "proxy" — a
                              network address on the target subnet (e.g.
                              "192.168.88.0"), NOT an interface name;
                              dnsmasq's own proxy dhcp-range syntax needs it
      pxe_boot_filename     : PXE boot filename to hand out in "pxelinux"
                              mode (default "lpxelinux.0" — syslinux's
                              standard BIOS loader)
      pxe_mode              : "pxelinux" (default) — today's single-stage
                              BIOS netboot, paired with
                              generate_boot_entry()'s per-MAC
                              pxelinux.cfg/01-<mac> files.
                              "ipxe-uefi" — two-stage UEFI netboot: DHCP
                              first hands a booting UEFI VM iPXE's own
                              firmware (ipxe.efi) over TFTP; once iPXE
                              itself is running it re-requests DHCP and,
                              via dnsmasq's dhcp-userclass tagging, gets
                              redirected to an HTTP URL for its real boot
                              script instead of another TFTP file — iPXE
                              can fetch a kernel/initrd/rootfs straight
                              over HTTP, which is what Harvester's own
                              installer needs (see
                              scripts/setup_harvester_cluster.py, the
                              first real consumer of this mode; confirmed
                              against Harvester's own PXE docs and the
                              harvester/ipxe-examples repo's libvirt guide,
                              which uses this exact same dhcp-userclass
                              tagging via dnsmasq — see use_pxe_service
                              below for the one real difference from that
                              guide's own "full" DHCP setup: under "proxy"
                              mode, the redirect is delivered via
                              pxe-service, not dhcp-boot).
    nodes (new, only consulted in "ipxe-uefi" mode): a definition["nodes"]-
      shaped dict supplying the per-MAC -> boot-script-URL mapping via each
      node's "mymac"/"pxe_ipxe_url" keys — a node missing either is silently
      skipped (same opt-in-per-node stance as generate_boot_entry(), which
      skips any node missing pxe_kernel).
    """
    bridge = cfg.get("pxe_bridge") or "br0"
    mode = cfg.get("pxe_dhcp_mode") or "off"
    boot_filename = cfg.get("pxe_boot_filename") or "lpxelinux.0"
    pxe_mode = cfg.get("pxe_mode") or "pxelinux"

    lines = [
        "port=0",                 # never answer plain DNS queries — BIND owns that
        "interface={}".format(bridge),
        "bind-interfaces",
        "enable-tftp",
        "tftp-root=/tftpboot",
    ]

    # In proxy mode the boot server and file name are sent only through the PXE options that a pxe-service directive generates.
    # A plain dhcp-boot entry is accepted silently and never sent in a proxy reply. Outside proxy mode dhcp-boot works, because
    # dnsmasq generates the whole reply there, so only proxy mode needs the pxe-service form.
    use_pxe_service = (mode == "proxy")

    if pxe_mode == "pxelinux":
        if use_pxe_service:
            # pxe-service's basename omits pxelinux's usual ".0" — dnsmasq
            # appends the right suffix per client architecture itself.
            basename = boot_filename[:-2] if boot_filename.endswith(".0") else boot_filename
            lines.append('pxe-service=x86PC,"network boot",{}'.format(basename))
        else:
            lines.append("dhcp-boot={}".format(boot_filename))
    elif pxe_mode == "ipxe-uefi":
        # Stage 1: a client that is not iPXE yet gets the iPXE UEFI binary over TFTP. Stage 2: a client tagged ipxe gets this node's
        # boot-script URL over HTTP. The per-MAC dhcp-host and set: tag lets different nodes load different scripts from one DHCP
        # server. x86-64_EFI is the pxe-service name for the UEFI-only scope this mode targets.
        lines.append("dhcp-userclass=set:ipxe,iPXE")
        if use_pxe_service:
            lines.append('pxe-service=tag:!ipxe,x86-64_EFI,"PXE chainload to iPXE",ipxe.efi')
        else:
            lines.append("dhcp-boot=tag:!ipxe,ipxe.efi")
        for node_name, node_cfg in sorted((nodes or {}).items()):
            mac = (node_cfg or {}).get("mymac")
            url = (node_cfg or {}).get("pxe_ipxe_url")
            if not mac or not url:
                continue
            mac = mac.lower()
            tag = "n" + mac.replace(":", "")
            lines.append("dhcp-host={},set:{}".format(mac, tag))
            if use_pxe_service:
                lines.append('pxe-service=tag:{},tag:ipxe,x86-64_EFI,"{}",{}'.format(tag, node_name, url))
            else:
                lines.append("dhcp-boot=tag:{},tag:ipxe,{}".format(tag, url))
    else:
        die("pxe_mode '{}' is invalid — must be one of: pxelinux, ipxe-uefi".format(pxe_mode))

    if mode == "full":
        start = cfg.get("pxe_dhcp_range_start")
        end = cfg.get("pxe_dhcp_range_end")
        if not start or not end:
            die("pxe_dhcp_mode is 'full' but pxe_dhcp_range_start/pxe_dhcp_range_end are not both set")
        lease = cfg.get("pxe_dhcp_lease") or "12h"
        lines.append("dhcp-range={},{},{}".format(start, end, lease))
    elif mode == "proxy":
        # Proxy DHCP: dnsmasq answers only the PXE options, next-server and the boot file name. An external DHCP server still hands
        # out the lease. This keeps DHCP optional, and it is the default mode.
        #
        # The proxy dhcp-range takes a network address on the target subnet, for example 192.168.88.0, not an interface name. An
        # interface name makes dnsmasq refuse to start, and systemd then gives up restarting the service.
        subnet = cfg.get("pxe_dhcp_proxy_subnet")
        if not subnet:
            die("pxe_dhcp_mode is 'proxy' but pxe_dhcp_proxy_subnet is not set — dnsmasq's proxy "
                "dhcp-range needs a network address on the target subnet (e.g. \"192.168.88.0\"), "
                "not an interface name")
        lines.append("dhcp-range={},proxy".format(subnet))
    elif mode == "off":
        # No DHCP role at all — the network's own DHCP server must already
        # supply next-server/boot-filename itself. TFTP/boot-file serving
        # still works; this container just never touches DHCP traffic.
        pass
    else:
        die("pxe_dhcp_mode '{}' is invalid — must be one of: off, proxy, full".format(mode))

    return "\n".join(lines) + "\n"


def _pxe_quadlet_unit(tftp_root, config_path):
    """
    Return the podman-systemd Quadlet unit for the PXE container. systemd generates lab-pxe.service from this file on daemon-reload.

    --network host is required. DHCP and PXE broadcasts need layer 2 access to the lab's bridge, which a container's own network
    namespace does not have.

    NET_ADMIN and NET_RAW are required as well. dnsmasq does not start without them, because it needs to bind privileged ports and send
    raw-socket DHCP replies.
    """
    return (
        "[Container]\n"
        "Image={image}\n"
        "ContainerName=lab-pxe\n"
        "Network=host\n"
        "AddCapability=NET_ADMIN\n"
        "AddCapability=NET_RAW\n"
        "Volume={tftp_root}:/tftpboot:ro\n"
        "Volume={config_path}:/etc/dnsmasq.conf:ro\n"
        "\n"
        "[Service]\n"
        "Restart=always\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).format(image=_PXE_IMAGE, tftp_root=tftp_root, config_path=config_path)


class PXEService(AuxService):
    """
    TFTP + PXE boot-file serving + optional DHCP, bundled into one small
    podman container (dnsmasq). See module docstring for the DHCP-mode
    rationale and MIGRATION_TODO.md Phase 5 §5.4 for the full design.

    Genuinely new capability — nothing to preserve from bash or earlier
    python, so this class has more implementation latitude than the
    move-only services above. Config comes from the lab JSON's optional
    "pxe" section (see _dnsmasq_conf's docstring for every key) plus
    LAB_SETUP_PATH (reused — pxe files land under
    <LAB_SETUP_PATH>/pxe/tftpboot, alongside the existing ignition/
    combustion/cloud-init/install_iso directories).
    """

    name = "pxe"

    def __init__(self, lab_setup_path="/srv/www/htdocs/lab_creation"):
        super(PXEService, self).__init__(lab_setup_path)
        self.tftp_root = str(Path(lab_setup_path) / "pxe" / "tftpboot")
        self.config_dir = Path(lab_setup_path) / "pxe"
        self.config_path = str(self.config_dir / "dnsmasq.conf")

    def install(self):
        """Build the dnsmasq container image once, if not already present."""
        exists = subprocess.run(
            ["podman", "image", "exists", _PXE_IMAGE],
        ).returncode == 0
        if exists:
            return
        if shutil.which("podman") is None:
            die("podman is required for the PXE service but is not installed")
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            containerfile = Path(tmpdir) / "Containerfile"
            containerfile.write_text(_PXE_CONTAINERFILE)
            result = subprocess.run(["podman", "build", "-t", _PXE_IMAGE, tmpdir])
            if result.returncode != 0:
                die("podman build failed for the PXE service image")

    def configure(self, definition, config):
        """Write the TFTP root, dnsmasq.conf, and the Quadlet unit."""
        cfg = definition.get("pxe", {}) or {}
        nodes = definition.get("nodes", {}) or {}
        pxe_mode = cfg.get("pxe_mode") or "pxelinux"

        Path(self.tftp_root).mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        if pxe_mode == "ipxe-uefi":
            self._fetch_ipxe_efi()

        Path(self.config_path).write_text(_dnsmasq_conf(cfg, self.tftp_root, nodes=nodes))
        _PXE_QUADLET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PXE_QUADLET_PATH.write_text(_pxe_quadlet_unit(self.tftp_root, self.config_path))

        if pxe_mode == "pxelinux":
            for node_name in nodes:
                self.generate_boot_entry(definition, node_name)
        # ipxe-uefi mode needs no per-node TFTP boot-config file — the
        # per-MAC dhcp-boot line written into dnsmasq.conf above already
        # points each node straight at its own HTTP script; nothing else
        # goes under tftp_root for it besides the shared ipxe.efi fetched
        # above.

    def _fetch_ipxe_efi(self):
        """
        Idempotently fetch iPXE's own UEFI firmware binary into the TFTP
        root — see _IPXE_EFI_URL's docstring for why this is fetched rather
        than vendored.
        """
        dest = Path(self.tftp_root) / "ipxe.efi"
        if dest.exists():
            return
        try:
            urllib.request.urlretrieve(_IPXE_EFI_URL, str(dest))
        except OSError as e:
            die("failed to fetch ipxe.efi from {}: {}".format(_IPXE_EFI_URL, e))

    def generate_boot_entry(self, definition, node_name):
        """
        Write a per-node PXE boot config (pxelinux.cfg/01-<mac>) from that
        node's optional pxe_kernel/pxe_initrd/pxe_append fields. Skips (with
        a warning, not an error — PXE is opt-in and most nodes in a lab
        won't need it) any node missing pxe_kernel, since boot content is
        inherently deployment-specific and there is no generic default.
        """
        node_cfg = definition.get("nodes", {}).get(node_name, {}) or {}
        kernel = node_cfg.get("pxe_kernel")
        if not kernel:
            return

        mac = node_cfg.get("mymac")
        if not mac:
            warn("PXE boot entry for '{}' skipped — no mymac set".format(node_name))
            return

        initrd = node_cfg.get("pxe_initrd", "")
        append = node_cfg.get("pxe_append", "")

        entry = "DEFAULT pxe\nLABEL pxe\n  KERNEL {}\n".format(kernel)
        if initrd:
            entry += "  INITRD {}\n".format(initrd)
        if append:
            entry += "  APPEND {}\n".format(append)

        pxelinux_dir = Path(self.tftp_root) / "pxelinux.cfg"
        pxelinux_dir.mkdir(parents=True, exist_ok=True)
        mac_filename = "01-" + mac.lower().replace(":", "-")
        (pxelinux_dir / mac_filename).write_text(entry)

    def enable(self):
        # A Quadlet unit is generated by systemd, so systemctl enable rejects it. daemon-reload processes its [Install] section,
        # and start or restart is enough. Restart is used, because start leaves a running unit alone, and a rebuilt image would
        # never take effect.
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        result = subprocess.run(["systemctl", "restart", _PXE_UNIT_NAME])
        if result.returncode != 0:
            die("failed to start {}".format(_PXE_UNIT_NAME))

    def is_active(self):
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", _PXE_UNIT_NAME],
        ).returncode == 0


# ── Port forwarding ──────────────────────────────────────────────────────────

class PortForwardService(AuxService):
    """
    DNAT-forwards each node's own "forwarded_ports" list from the KVM
    hypervisor's real IP — the counterpart, for lab VMs, of
    setup_lab_automation.sh's own configure_nat_port_forwarding() for the
    automation VM at bootstrap time (see setup_demo_server/
    lab.cfg.template's _network_mode). Meant for VMs attached to a NAT'd
    libvirt network (_network_mode=nat) rather than a real bridge, where a
    node's own "myip" is a private address unreachable from outside — but
    nothing here actually checks _network_mode; it just forwards whatever
    ports it's told to, for whichever nodes declare them, exactly like
    PXEService is agnostic to why a node opted into PXE.

    Genuinely new capability, like PXEService — no bash equivalent, no
    prior behaviour to preserve. All the actual rule-building/idempotency
    logic lives in libs/portforward.py (shared with the automation VM's own
    bootstrap-time forwarding); this class is just the AuxService-shaped
    wrapper setup_lab.py's phase_services() already knows how to drive.
    """

    name = "portforward"

    def __init__(self, lab_setup_path="/srv/www/htdocs/lab_creation"):
        super(PortForwardService, self).__init__(lab_setup_path)
        # Set by configure() — is_active() (AuxService's interface takes no
        # args) needs it too, since this service's actual state lives on the
        # hypervisor, not the automation VM these calls run from.
        self._remote_host = None
        # _remote_host is None for a local hypervisor, both before and after configure(), so it cannot tell is_active() whether
        # configure() has run. A flag records that it has, so is_active() does not check the wrong host for an unconfigured service.
        self._configured = False

    def install(self):
        pass  # iptables is always present on the hypervisor; nothing to install

    def configure(self, definition, config):
        """
        Build {node's own myip: node's own forwarded_ports} across every
        node that declares one (opt-in per node, same stance as PXEService's
        generate_boot_entry() skipping nodes missing pxe_kernel), then apply
        it on the hypervisor — the actual NAT boundary with the real
        external IP; these rules can never live on the automation VM itself.
        """
        import portforward

        nodes = definition.get("nodes", {}) or {}
        port_map = {}
        for node_name, node_cfg in nodes.items():
            specs = (node_cfg or {}).get("forwarded_ports")
            myip = (node_cfg or {}).get("myip")
            if not specs:
                continue
            if not myip:
                warn("'{}' has forwarded_ports but no myip — skipping".format(node_name))
                continue
            port_map[myip] = specs

        self._remote_host = config.get("REMOTE_HOST") or None
        self._configured = True
        portforward.apply_forwarded_ports(port_map, remote_host=self._remote_host)

    def enable(self):
        pass  # rules are applied synchronously in configure() — no daemon to start

    def is_active(self):
        """Checks the CHAIN_DNAT chain exists — on the hypervisor (over SSH)
        if configure() has run and recorded one, locally otherwise (mirrors
        setup_lab_automation.sh's own bootstrap-time call, which always
        applies rules locally since it runs ON the hypervisor already).

        False before configure() has ever run: a service that was never
        applied can't be active, and _remote_host alone can't tell that
        apart from "configured for a local hypervisor" — both leave it at
        None.
        """
        if not self._configured:
            return False
        import portforward
        run = portforward._iptables_runner(self._remote_host)
        return run(["-t", "nat", "-L", portforward.CHAIN_DNAT]).returncode == 0


SERVICES = {
    "dns": DNSService,
    "http": HTTPService,
    "pxe": PXEService,
    "portforward": PortForwardService,
}


def get(name, **kwargs):
    """Return a service instance by name, or die() listing valid names."""
    cls = SERVICES.get(name)
    if cls is None:
        die("Unknown service '{}' — supported: {}".format(name, ", ".join(sorted(SERVICES))))
    return cls(**kwargs)
