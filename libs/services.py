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

import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from lab_creation import log, warn, die, ssh_run

NAMED_ZONE_DIR = Path("/var/lib/named")

# Serializes every DNS zone-file mutation below (add_to_dns/del_from_dns/
# add_service_dns/add_dns_to_named_rr) — added 2026-09-21 for setup_lab.py's
# parallel VM-creation mode. _dns_add_line/_dns_remove_line/_dns_append_line
# do a plain read-whole-file -> mutate -> write-whole-file, non-atomically;
# two nodes' DNS registrations racing on the SAME zone file (every node in a
# domain shares one .lan/.db file) can genuinely lose an entry — a classic
# lost-update race, not a hypothetical one. A single process-wide lock is
# enough (this class only ever runs inside one process — the automation
# node's own setup_lab.py — never multiple processes contending for the
# same zone file at once).
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

    def _remote(self, hostname, cmd, check=True):
        return ssh_run(hostname, cmd, check=check)

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

    def _dns_append_line(self, zone_file, record):
        zone_file = Path(zone_file)
        zone_file.touch()
        with zone_file.open("a") as f:
            f.write(record + "\n")

    def _dns_remove_ptr_for_octet(self, zone_file, last_octet):
        """
        Removes any EXISTING PTR line for `last_octet` regardless of which
        hostname it currently points to.

        Real bug found live 2026-09-23 (solar-system-lab.json): add_to_dns's
        own _dns_add_line() only dedups an EXACT line match, so reusing an IP
        a previous (destroyed, but incompletely cleaned-up) VM once held left
        its OLD PTR record sitting right alongside the new one — two PTR
        records for the same IP, BIND happily serves both, and the new VM's
        own reverse-DNS lookup of its own IP can come back with the WRONG
        (older) hostname. Confirmed live: this is exactly why
        venus.mydemo.lab (reusing an IP a since-destroyed "node1a" VM once
        used) picked up "node1a.mydemo.lab" as its own transient hostname on
        boot instead of its real one — compounded by a separate, also-real
        virt-customize bug (see prepare_virt_customize's own note) that left
        venus with no STATIC hostname set at all, so systemd fell back to
        deriving one from reverse DNS.

        Matches on the octet as the line's own first whitespace-delimited
        token (not a substring search) — same node1-vs-node10 precision
        _dns_add_line's own docstring already documents, so removing PTR
        '14' never accidentally removes PTR '142' too.
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

    def restart_named(self, remote_servers=None):
        """Restart the local BIND named service and optionally on remote servers."""
        for server in (remote_servers or []):
            self._remote(server, "systemctl restart named", check=False)
        subprocess.run(["systemctl", "restart", "named"], check=False)

    def add_to_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
        """Add forward (A) and reverse (PTR) DNS records for a VM."""
        log("Adding DNS entry for '{}' → {}".format(vm_name, myip))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        a_record   = "{}         IN  A       {}".format(short, myip)
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

        lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
        rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

        with _dns_lock:
            for server in (remote_dns_servers or []):
                self._remote_dns_remove_ptr_for_octet(server, rev_file, last_octet)
                self._remote_dns_add(server, lan_file, a_record)
                self._remote_dns_add(server, rev_file, ptr_record)
                self._remote(server, "systemctl restart named", check=False)

            # Drop any stale PTR record for this IP FIRST — see
            # _dns_remove_ptr_for_octet's own docstring for the real
            # incident (a reused IP's old hostname winning a client's own
            # reverse-DNS lookup) this prevents. One canonical PTR per IP.
            self._dns_remove_ptr_for_octet(rev_file, last_octet)
            self._dns_add_line(lan_file, a_record)
            self._dns_add_line(rev_file, ptr_record)
            self.restart_named()

    def del_from_dns(self, vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
        """Remove forward and reverse DNS records for a VM."""
        log("Removing DNS entry for '{}'".format(vm_name))
        short      = vm_name.split(".")[0]
        last_octet = myip.split(".")[-1]
        a_record   = "{}         IN  A       {}".format(short, myip)
        ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

        lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
        rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

        with _dns_lock:
            for server in (remote_dns_servers or []):
                # check=False: same non-fatal-secondary-server rationale as
                # _remote_dns_add above.
                self._remote(server, "sed '/{}/d' -i {}".format(ptr_record, rev_file), check=False)
                self._remote(server, "sed '/{}/d' -i {}".format(a_record, lan_file), check=False)
                self._remote(server, "systemctl restart named", check=False)

            self._dns_remove_line(rev_file, ptr_record)
            self._dns_remove_line(lan_file, a_record)
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

        msg = "agent" if agent_nodes else "all"
        log("DNS '{}' added pointing to {} nodes of cluster '{}'".format(dns_entry, msg, clu_name))

        zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
        with _dns_lock:
            for _, ip in record_targets:
                record = "{}\tIN A  {}".format(dns_entry, ip)
                for server in (remote_dns_servers or []):
                    # check=False: same non-fatal-secondary-server rationale as
                    # DNSService.add_to_dns above.
                    self._remote(server, "sed '/{}\tIN A  {}/d' -i {}".format(dns_entry, ip, zone_file), check=False)
                    self._remote(server, "echo -e '{}' >> {}".format(record, zone_file), check=False)
                    self._remote(server, "systemctl restart named", check=False)
                self._dns_remove_line(zone_file, "{}\tIN A  {}".format(dns_entry, ip))
                self._dns_append_line(zone_file, record)

            self.restart_named()

    def add_dns_to_named_rr(self, definition, dns_entry, node_name, mydomain, remote_dns_servers=None):
        """Add a single round-robin A record (dns_entry -> node_name's own myip)."""
        myip = (definition.get("nodes", {}).get(node_name, {}) or {}).get("myip", "")
        record = "{}\tIN A  {}".format(dns_entry, myip)
        zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)

        with _dns_lock:
            existing = zone_file.read_text().splitlines() if zone_file.exists() else []
            if record in existing:
                log("- DNS entry \"{} → {}\" already correct, skipping".format(dns_entry, myip))
                return

            log("- add DNS entry \"{}.{}\"".format(dns_entry, mydomain))

            for server in (remote_dns_servers or []):
                self._remote_dns_add(server, zone_file, record)
                self._remote(server, "systemctl restart named", check=False)

            self._dns_add_line(zone_file, record)


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

    # dhcp-boot vs pxe-service: confirmed live 2026-08-30 (--log-dhcp showed
    # dnsmasq correctly recognizing a real client's vendor class —
    # "PXEClient:Arch:00007:UNDI:003001" — yet never logging a single
    # reply) that plain dhcp-boot's next-server/filename fields are simply
    # never sent in a PROXY DHCP reply: that information only ever goes out
    # through the PXE-specific vendor-encapsulated options a pxe-service
    # directive generates (over the separate PXE protocol on UDP/4011).
    # dnsmasq accepted the dhcp-boot-only config with no error at all — a
    # silent no-op, not a startup failure like the earlier dhcp-range bug —
    # which is what made this one much harder to spot. dhcp-boot works fine
    # in "full"/"off" mode (dnsmasq is generating the whole DHCP reply
    # itself there, next-server/filename included), so only "proxy" needs
    # the pxe-service form.
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
        # Stage 1: any client that is NOT already iPXE (the VM's own UEFI
        # firmware, on its very first PXE request) gets iPXE's UEFI binary
        # over TFTP. Stage 2: a client tagged "ipxe" (iPXE itself, now
        # running, re-requesting DHCP) gets THIS node's own boot-script URL
        # over HTTP instead — the per-MAC dhcp-host/set: tag is what lets
        # different nodes chainload different scripts (e.g. Harvester's
        # create vs. join configs) from the one DHCP server. x86-64_EFI is
        # the correct pxe-service CSA name for the UEFI-only scope this
        # mode targets (arch 7 in the PXE spec, confirmed against the real
        # client's own reported ARCH option — see module docstring).
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
        # PXE proxyDHCP: dnsmasq answers only the PXE-specific options
        # (next-server/boot filename); an external DHCP server still hands
        # out the actual lease/IP. This is what makes DHCP genuinely
        # optional without breaking PXE — the default mode.
        #
        # dnsmasq's proxy dhcp-range wants a NETWORK ADDRESS on the target
        # subnet (e.g. "192.168.88.0"), not an interface name — confirmed
        # live 2026-08-30 (the first time this ever ran against a real
        # dnsmasq, a pre-existing bug from before this project's own
        # PXEService was ever live-tested): passing pxe_bridge here
        # ("br0,proxy") makes dnsmasq refuse to start at all
        # ("bad dhcp-range at line N"), silently taking the whole PXE
        # service down (systemd's Restart=always retries then gives up —
        # "start request repeated too quickly" — with nothing surfaced to
        # whatever caller expected PXE to be up).
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
    Podman-systemd Quadlet unit content for the PXE container — the modern
    way to run "a small container as a system service" on SUSE systems
    (systemd auto-generates lab-pxe.service from this file on daemon-reload).
    --network host is required (not a design choice to reconsider casually):
    DHCP/PXE broadcast traffic needs L2 visibility on the lab's bridge, which
    a container's default isolated network namespace does not have.

    NET_ADMIN/NET_RAW are required too: confirmed live 2026-08-30 (the
    first time this container ever actually tried to serve DHCP for real)
    that dnsmasq refuses to start at all without them ("process is missing
    required capability NET_ADMIN") — podman's default capability set
    doesn't include what a real DHCP/TFTP server needs to bind privileged
    ports and send raw-socket DHCP replies, even under --network=host.
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
        # Quadlet units are systemd-generated, not real unit files on disk —
        # `systemctl enable` rejects them ("transient or generated";
        # confirmed live 2026-08-29 against an identical Quadlet unit for
        # the MCP endpoint). daemon-reload alone makes systemd process the
        # .container's own [Install] section (what makes it start on boot);
        # only `start`/`restart` is needed/valid here, not `enable --now`.
        # `restart`, not `start`: a rebuilt image (e.g. this method called
        # again after an image update) leaves an already-running unit
        # untouched by `start` — a no-op against a running unit — so the new
        # image would silently never take effect. Confirmed live
        # (2026-08-29) against the identical MCP-endpoint bug: a `start`
        # left a stale container running for 10+ minutes after a rebuild.
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
        # _remote_host alone can't tell is_active() whether configure() has
        # even run yet: a local hypervisor (no REMOTE_HOST configured) is a
        # perfectly normal POST-configure state that also leaves
        # _remote_host at None — found in code review 2026-09-05, not
        # currently reachable (nothing calls is_active() before configure()
        # today), but a real ordering landmine for a future caller: without
        # this flag, is_active() would silently check the wrong (local)
        # host and could misreport an unconfigured service as active.
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
