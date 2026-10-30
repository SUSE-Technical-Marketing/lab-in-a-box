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
from lab_creation import log, warn, die, ssh_run, clean_ssh_keys

NAMED_ZONE_DIR = Path("/var/lib/named")
NAMED_CONF = Path("/etc/named.conf")

# Shell-quoting for the zone lines and named.conf stanzas written over SSH, which carry quotes and braces.
_quote = shlex.quote

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

    def _reachable(self, server: str) -> bool:
        """
        Whether `server` answers SSH, probed once per process and reported once when it does not.

        A first failure drops the host's known_hosts entry and probes once more: a cloud lab's DNS VM is recreated
        under a reused address, and the key of the server that held it before makes every write to the new one fail
        without saying so. This is the same stale-key handling clean_ssh_keys() does for a recreated VM.
        """
        if server not in self._reachable_cache:
            reachable = self._remote(server, "true", check=False).returncode == 0
            if not reachable:
                clean_ssh_keys(server, server)
                reachable = self._remote(server, "true", check=False).returncode == 0
            self._reachable_cache[server] = reachable
            if not reachable:
                warn("- DNS server '{}' is not reachable: its zone is left untouched and will be brought back in "
                     "step by the next run that reaches it".format(server))
        return self._reachable_cache[server]

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

    def ensure_zone(self, server, mydomain: str, mynet_reverse: str = "") -> bool:
        """
        Make `server` (None for this host) a master of `mydomain`, and of its reverse zone when `mynet_reverse` is
        given, creating the zone file with the header BIND needs and declaring it in named.conf.

        Returns True when a zone had to be created, which tells the caller this master holds none of the lab's
        records yet and needs sync_zone() rather than one appended line. Returns False when every zone was already
        there, which is the normal path and does nothing.

        A record appended to a zone that is not declared, or to a file with no SOA, is silently never answered:
        this is what stops that happening.
        """
        created = False
        wanted = [(mydomain, dns_zone.forward_file(mydomain), dns_zone.forward_header(mydomain))]
        if mynet_reverse:
            wanted.append((dns_zone.reverse_zone(mynet_reverse), dns_zone.reverse_file(mynet_reverse),
                           dns_zone.reverse_header(mydomain, mynet_reverse)))
        for zone, file_name, header in wanted:
            if self._zone_declared(server, zone):
                continue
            log("- DNS server '{}' is not a master of '{}' yet: creating the zone".format(server or "this host", zone))
            if server is None:
                self._create_local_zone(zone, file_name, header)
            else:
                self._create_remote_zone(server, zone, file_name, header)
            created = True
        if created:
            self.restart_named(remote_servers=[server] if server else None)
        return created

    def _create_local_zone(self, zone: str, file_name: str, header: list) -> None:
        """Write the zone file under NAMED_ZONE_DIR and declare it in this host's named.conf."""
        path = NAMED_ZONE_DIR / file_name
        if not path.exists() or not path.read_text().strip():
            path.write_text("\n".join(header) + "\n")
            path.chmod(0o644)
        with NAMED_CONF.open("a") as conf:
            conf.write("\n" + dns_zone.stanza(zone, file_name) + "\n")

    def _create_remote_zone(self, server: str, zone: str, file_name: str, header: list) -> None:
        """
        The counterpart of _create_local_zone() over SSH, finding the server's own BIND layout the same way
        ensure_cloud_dns_vm() does: a dedicated include file on SUSE and the Fedora family, named.conf.local on
        Debian and Ubuntu.
        """
        lines = " ".join(_quote(line) for line in header)
        self._remote(server, "; ".join([
            '. /etc/os-release; case " $ID $ID_LIKE " in'
            ' *" debian "*|*" ubuntu "*) BIND_CONF=/etc/bind/named.conf.local;;'
            ' *) BIND_CONF=/etc/named.lab.conf;;'
            ' esac',
            "mkdir -p /var/lib/named",
            "[ -s /var/lib/named/{f} ] || printf '%s\\n' {lines} > /var/lib/named/{f}".format(
                f=file_name, lines=lines),
            "printf '%s\\n' {stanza} >> \"$BIND_CONF\"".format(
                stanza=_quote(dns_zone.stanza(zone, file_name))),
            'if [ "$BIND_CONF" = /etc/named.lab.conf ]; then grep -qF "$BIND_CONF" /etc/named.conf'
            ' || echo \'include "/etc/named.lab.conf";\' >> /etc/named.conf; fi',
        ]), check=False)

    # ── sync: every master answers the same records ─────────────────────────

    def zone_records(self, server, file_name: str) -> set:
        """The comparable records `server` (None for this host) holds in `file_name`; empty when it has no such zone."""
        if server is None:
            path = NAMED_ZONE_DIR / file_name
            return dns_zone.records(path.read_text()) if path.exists() else set()
        result = self._remote(server, "cat /var/lib/named/{} 2>/dev/null".format(file_name),
                              check=False, capture=True)
        return dns_zone.records(result.stdout if result.returncode == 0 else "")

    def _zone_files(self, mydomain: str, mynet_reverse: str = "") -> list:
        """The zone files of `mydomain`: its forward zone, and its reverse zone when the lab states one."""
        files = [dns_zone.forward_file(mydomain)]
        if mynet_reverse:
            files.append(dns_zone.reverse_file(mynet_reverse))
        return files

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
        What the reachable masters of `mydomain` disagree on, reported and returned as one message per difference,
        changing nothing. Empty when they already hold the same records.
        """
        with _dns_lock:
            messages = self._difference_messages(
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
            comparison = self._compare_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)
            messages = self._difference_messages(comparison)
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
            self._remote_dns_add(server, "/var/lib/named/{}".format(file_name), record)

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
                        self._remote_dns_remove_record(server, "/var/lib/named/{}".format(file_name), record)
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

    def add_to_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
        """Add forward (A) and reverse (PTR) DNS records for a VM. Without `mynet_reverse` only the A record is added."""
        log("Adding DNS entry for '{}' → {}".format(vm_name, myip))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        a_record   = "{}         IN  A       {}".format(short, myip)
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

        lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
        rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

        with _dns_lock:
            # Every master holds the zone before a record is appended to it, and one that had to have it created
            # holds none of this lab's earlier records: it is brought into step with the others rather than left
            # with the one record below. A master that does not answer is left out entirely. Under the same lock as
            # the records, because creating a zone writes the very file the appends below read and rewrite.
            servers = self.masters(remote_dns_servers)
            resync = self.ensure_zone(None, mydomain, mynet_reverse)
            for server in servers:
                resync = self.ensure_zone(server, mydomain, mynet_reverse) or resync

            for server in servers:
                if mynet_reverse:
                    self._remote_dns_remove_ptr_for_octet(server, rev_file, last_octet)
                self._remote_dns_add(server, lan_file, a_record)
                if mynet_reverse:
                    self._remote_dns_add(server, rev_file, ptr_record)
                self._remote(server, "systemctl restart named", check=False)

            # Drop any existing PTR record for this IP first. Each IP keeps one canonical PTR record.
            if mynet_reverse:
                self._dns_remove_ptr_for_octet(rev_file, last_octet)
            self._dns_add_line(lan_file, a_record)
            if mynet_reverse:
                self._dns_add_line(rev_file, ptr_record)
            self._update_hosts_file(vm_name, myip)
            self.restart_named()

        if resync:
            self.sync_zone(mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)

    def del_from_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
        """Remove a VM's forward and reverse DNS records and every service record pointing at its IP. Without `mynet_reverse`
        the PTR record is left alone."""
        log("Removing DNS entry for '{}'".format(vm_name))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        a_record   = "{}         IN  A       {}".format(short, myip)
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

        lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
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

            for server in servers:
                # check=False: same non-fatal-secondary-server rationale as
                # _remote_dns_add above.
                if mynet_reverse:
                    self._remote(server, "sed '/{}/d' -i {}".format(ptr_record, rev_file), check=False)
                self._remote(server, "sed '/{}/d' -i {}".format(a_record, lan_file), check=False)
                self._remote(server, "sed '/\\tIN A  {}$/d' -i {}".format(myip.replace(".", "\\."), lan_file), check=False)
                self._remote(server, "systemctl restart named", check=False)

            if mynet_reverse:
                self._dns_remove_line(rev_file, ptr_record)
            self._dns_remove_line(lan_file, a_record)
            self._dns_remove_service_lines(lan_file, myip)
            self._update_hosts_file(vm_name)
            self.restart_named()

    def add_service_dns(self, definition, clu_name, clu_type, dns_entry, mydomain, remote_dns_servers=None):
        """Add round-robin A records for a cluster service DNS entry."""
        nodes       = definition.get("nodes", {})
        install_key = "INSTALL_{}_TYPE".format(clu_type.upper())

        agent_nodes = [
            (name, cfg["myip"])
            for name, cfg in nodes.items()
            if cfg.get(install_key) == "agent" and cfg.get("kcluster") == clu_name and "myip" in cfg
        ]

        record_targets = agent_nodes or [
            (name, cfg["myip"])
            for name, cfg in nodes.items()
            if cfg.get("kcluster") == clu_name and "myip" in cfg
        ]

        if not record_targets:
            # Every node of the cluster lacks a myip to point at. A lab definition that leaves addresses out is
            # read from the file the run saved them to (setup_lab.py's phase_allocate_ips), so reaching here means
            # that did not happen and the service would have no DNS name at all.
            warn("DNS '{}' NOT added: no node of cluster '{}' has a 'myip' to point it at".format(
                dns_entry, clu_name))
            return
        msg = "agent" if agent_nodes else "all"
        log("DNS '{}' added pointing to {} nodes of cluster '{}' ({})".format(
            dns_entry, msg, clu_name, ", ".join(ip for _n, ip in record_targets)))

        zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
        with _dns_lock:
            servers = self.masters(remote_dns_servers)
            resync = self.ensure_zone(None, mydomain)
            for server in servers:
                resync = self.ensure_zone(server, mydomain) or resync
            for _, ip in record_targets:
                record = "{}\tIN A  {}".format(dns_entry, ip)
                for server in servers:
                    # check=False: same non-fatal-secondary-server rationale as
                    # DNSService.add_to_dns above.
                    self._remote(server, "sed '/{}\tIN A  {}/d' -i {}".format(dns_entry, ip, zone_file), check=False)
                    self._remote(server, "echo -e '{}' >> {}".format(record, zone_file), check=False)
                    self._remote(server, "systemctl restart named", check=False)
                self._dns_remove_line(zone_file, "{}\tIN A  {}".format(dns_entry, ip))
                self._dns_append_line(zone_file, record)

            self.restart_named()

        if resync:
            self.sync_zone(mydomain, remote_dns_servers=remote_dns_servers)

    def add_dns_to_named_rr(self, definition, dns_entry, node_name, mydomain, remote_dns_servers=None):
        """Add a single round-robin A record (dns_entry -> node_name's own myip)."""
        myip = (definition.get("nodes", {}).get(node_name, {}) or {}).get("myip", "")
        record = "{}\tIN A  {}".format(dns_entry, myip)
        zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)

        with _dns_lock:
            servers = self.masters(remote_dns_servers)
            resync = self.ensure_zone(None, mydomain)
            for server in servers:
                resync = self.ensure_zone(server, mydomain) or resync

            existing = zone_file.read_text().splitlines() if zone_file.exists() else []
            if record in existing and not resync:
                log("- DNS entry \"{} → {}\" already correct, skipping".format(dns_entry, myip))
                return

            log("- add DNS entry \"{}.{}\"".format(dns_entry, mydomain))

            for server in servers:
                self._remote_dns_add(server, zone_file, record)
                self._remote(server, "systemctl restart named", check=False)

            self._dns_add_line(zone_file, record)

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
