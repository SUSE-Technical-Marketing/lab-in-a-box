#!/usr/bin/env python3
# Part of lab-in-a-box — cross-cloud WireGuard overlay network.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/overlay.py — hub-and-spoke, SITE-TO-SITE WireGuard overlay so lab
nodes in different clouds (and the home libvirt lab) can reach each other
by their real IP, as if every site's network were routed to every other.

Design (see repo TODO, "cross-cloud WireGuard overlay plan", 2026-09-10;
built 2026-09-18; corrected to this site-to-site shape later the same day
after live-testing surfaced two real mistakes — see the TODO's own
"CROSS-CLOUD WIREGUARD OVERLAY" entries for the full story):

  - Hub-and-spoke, ONE hub, but the overlay members are SITE GATEWAYS, not
    individual lab nodes. A "site" is one KVM/libvirt environment (the
    home lab) or one cloud account. Each site has exactly ONE gateway: for
    the home site, the automation VM itself (config's own "mysource",
    already the box every lab script already runs on / SSHes from); for a
    cloud account, either the designated hub account's own gateway VM
    (OVERLAY_HUB_ACCOUNT) or a small dedicated per-account gateway VM
    (ensure_site_gateway_vm(), mirrors backends.ensure_cloud_dns_vm()'s own
    reuse-or-create convention). Individual lab nodes are NEVER themselves
    WireGuard peers — see the first mistake below.
  - Each site gateway advertises its OWN site's real subnet CIDR as
    AllowedIPs on the hub (not a synthetic per-node /32) — this is genuine
    site-to-site routing: a node's real IP, not an overlay-assigned one, is
    what's reachable across the tunnel. A small dedicated overlay CIDR
    (default 10.99.0.0/16) is used ONLY for the gateways' own WireGuard
    interface addresses (their point of presence on the tunnel), to avoid
    address collisions between sites that might reuse the same RFC1918
    range independently (e.g. two different home labs both on
    192.168.x.0/24).
  - Every OTHER host at a site (not the gateway) gets a plain, persistent
    IP route for each remote site's subnet, via its own local gateway's
    real LAN/private IP — see ensure_route_via_site_gateway(). This is the
    literal "local route on each host pointing at their local automation
    VM" a lab node needs; it never runs WireGuard itself.
  - Keys are generated ON each gateway itself (mirrors
    lab_creation.ensure_lab_ssh_key()'s own "never locally, never
    centrally" convention) — a private key never transits SSH.
  - Overlay admin-address allocation (_allocate_overlay_ip, keyed by site
    name) and the site registry (site name -> real subnet CIDR) are both
    tracked in plain text files ON THE HUB — sequential/append-only,
    deliberately not a general-purpose IPAM, good enough for a
    lab-scale overlay (a handful of sites).

TWO REAL MISTAKES FOUND LIVE-TESTING THE FIRST VERSION OF THIS MODULE,
2026-09-18, both fixed here — kept in this docstring since they explain
why the shape changed mid-session:
  1. The live test used sol.mydemo.lab (a real SMLM lab NODE) as the hub,
     and made individual lab nodes (e.g. charon.mydemo.lab) join the
     overlay AS THEMSELVES. User: "the automation lab VMs, should be the
     ones doing the tunneling, since there is one on each environment and
     this is not exclusive of SMLM, correct this mistake" — correct: the
     hub/gateway role belongs to the SITE's automation VM (or a dedicated
     small VM playing that role in a cloud account), never an arbitrary
     addon-bearing lab node. sol's wg0/ip_forward were torn down.
  2. Individual lab nodes had no way to reach the overlay at all except by
     joining it directly themselves. User: "there should be a local route
     on each host that points to their local automation VM as the default
     route for the lab internal network on each site" — this module's
     ensure_route_via_site_gateway() is that fix.

NOT YET DONE (real, flagged gaps, not silently glossed over):
  - DNS: site subnets are routable once this runs, but no DNS record
    changes are made here — a node is reachable by its real hostname only
    once that name already resolves to an address inside a routed subnet.
  - A site's own already-provisioned nodes are NOT retroactively re-routed
    when a NEW remote site joins later — routes are (re)pushed only at
    each node's own setup_vm.py provisioning time. A long-lived lab that
    adds a second cloud account well after its first nodes were created
    would need those earlier nodes' routes refreshed by hand.
  - ensure_route_via_site_gateway()'s persistence mechanism (a systemd
    oneshot unit) assumes systemd — true for every OS image this project
    already targets, not re-verified against anything older.
  - AWS-specific: the site gateway's own ENI needs "source/dest check"
    disabled to forward traffic not addressed to itself — see
    backends.AWSBackend.disable_source_dest_check(). No equivalent has
    been implemented for the other 7 cloud backends yet (same "AWS got it
    first" situation as aws_open_ports/ensure_ports_open() already were).
  - The hub itself is never auto-destroyed (same convention as
    backends.ensure_cloud_dns_vm()'s DNS VM — shared, persistent, reused
    across every lab/run on that account, not per-lab).
  - Single hub = SPOF; all cross-site traffic hairpins through one region.
    Fine for a lab, per the TODO's own RISKS/NOTES.
"""

import ipaddress
import shlex
from pathlib import Path

from lab_creation import ssh_run, ssh_output, log, warn, die


DEFAULT_OVERLAY_CIDR = "10.99.0.0/16"
DEFAULT_WG_PORT = 51820

_WG_KEY_DIR = "/etc/wireguard"
_WG_PRIVKEY_PATH = _WG_KEY_DIR + "/wg0.key"
_WG_PUBKEY_PATH = _WG_KEY_DIR + "/wg0.pub"
_WG_CONF_PATH = _WG_KEY_DIR + "/wg0.conf"
_ALLOC_PATH = _WG_KEY_DIR + "/overlay_allocations"
_SITE_REGISTRY_PATH = _WG_KEY_DIR + "/overlay_sites"

_ROUTES_SCRIPT_PATH = "/etc/lab-overlay-routes.sh"
_ROUTES_UNIT_PATH = "/etc/systemd/system/lab-overlay-routes.service"

# Same one-liner OR-chain convention already used by
# scripts/install_ansible_control_node.py for ansible-core — zypper (SUSE),
# apt (Debian/Ubuntu — the "wireguard" meta-package pulls wireguard-tools),
# dnf (RHEL family) in that order, first one that exists on the node wins.
_INSTALL_CMD = (
    "command -v wg >/dev/null 2>&1 || "
    "(zypper --non-interactive install -y wireguard-tools 2>/dev/null || "
    "apt-get install -y wireguard 2>/dev/null || "
    "dnf install -y wireguard-tools 2>/dev/null)"
)


def site_name_for(backend_name, account):
    """Canonical site-registry name for a cloud account, or "home" for
    libvirt — shared by setup_vm.py (to identify which site a node
    belongs to) and this module's own gateway/site-registry functions, so
    both sides always agree on the same name."""
    if backend_name in ("libvirt", "harvester", None, ""):
        return "home"
    return "{}-{}".format(backend_name, account or "default")


def hub_vm_name(backend_name, account):
    """Canonical fixed name for a cloud account's dedicated gateway VM —
    shared by ensure_overlay_hub()/ensure_site_gateway_vm() (which create
    it) and setup_vm.py (which needs to look it up again later to resolve
    its private IP for route-pushing), so the naming convention lives in
    exactly one place. Mirrors backends.ensure_cloud_dns_vm()'s own
    "lab-dns-<backend>[-<account>]" convention."""
    if account in ("", "default", None):
        return "lab-overlay-gw-{}".format(backend_name)
    return "lab-overlay-gw-{}-{}".format(backend_name, account)


def _gateway_vm_user_data(root_ssh_key):
    """
    Minimal cloud-init for a freshly-created hub/gateway VM — just enough
    root SSH access to let ensure_overlay_hub_ready()/ensure_wg_installed()
    do everything else (installing wireguard-tools, writing wg0.conf,
    ip_forward) over SSH afterward, exactly like every other lab node.
    Mirrors backends.py's own _cloud_dns_vm_user_data() root-SSH-user
    stanza (same real bug it already documents/fixes: a bare top-level
    ssh_authorized_keys: only grants the distro's own default user a key,
    not root — Ubuntu's stock cloud image otherwise rejects root SSH
    outright), without that function's BIND-specific package/runcmd work,
    which this gateway role doesn't need at all.
    """
    return (
        "#cloud-config\n"
        "package_update: true\n"
        "users:\n"
        "  - default\n"
        "  - name: root\n"
        "    lock_passwd: false\n"
        "    ssh_authorized_keys:\n"
        "      - {key}\n"
    ).format(key=root_ssh_key.strip())


def _create_gateway_vm(backend, backend_name, vm_name, iso_image, lab_setup_path,
                        root_ssh_key, open_ports=None):
    """
    Shared VM-creation body for ensure_overlay_hub()/ensure_site_gateway_vm()
    — writes the cloud-init user-data create_vm() itself requires (a real
    bug found live-testing 2026-09-18: the first version of both callers
    skipped this step entirely, unlike backends.ensure_cloud_dns_vm()'s own
    equivalent, and create_vm() died outright with "cloud-init user-data
    not found"), then creates the VM. Returns its real IP.
    """
    log("- No gateway VM found for backend \"{}\" — creating \"{}\"".format(backend_name, vm_name))
    base = Path(lab_setup_path) / "cloud-init"
    base.mkdir(parents=True, exist_ok=True)
    (base / "{}_user-data".format(vm_name)).write_text(_gateway_vm_user_data(root_ssh_key))

    backend.copy_vm_image(iso_image, vm_name, 8, config_method="cloud-init")
    backend.push_provisioning_files(vm_name, config_method="cloud-init")
    ip = backend.create_vm(vm_name, 1, 512, 8, None, config_method="cloud-init",
                            iso_image=iso_image, mymac=None, open_ports=open_ports or [])
    if not ip:
        die("gateway VM '{}' was created on backend '{}' but create_vm() reported "
            "no IP".format(vm_name, backend_name))
    return ip


def ensure_wg_installed(host):
    """Idempotently installs wireguard-tools on `host` (no-op if `wg` already exists)."""
    ssh_run(host, _INSTALL_CMD)


def generate_wg_keypair(host):
    """
    Idempotently generates a WireGuard keypair ON `host` itself (never
    locally, never centrally — see module docstring), at the fixed paths
    this module always uses (_WG_PRIVKEY_PATH/_WG_PUBKEY_PATH). Returns
    (private_key, public_key) as stripped strings.
    """
    exists = ssh_run(host, "test -f {}".format(shlex.quote(_WG_PRIVKEY_PATH)), check=False).returncode == 0
    if exists:
        log("- WireGuard keypair already exists on {} — reusing it".format(host))
    else:
        ssh_run(host, "mkdir -p {d} && umask 077 && wg genkey | tee {priv} | wg pubkey > {pub} && "
                       "chmod 600 {priv}".format(
                           d=shlex.quote(_WG_KEY_DIR),
                           priv=shlex.quote(_WG_PRIVKEY_PATH),
                           pub=shlex.quote(_WG_PUBKEY_PATH)))
        log("- Generated a new WireGuard keypair on {}".format(host))
    privkey = ssh_output(host, "cat {}".format(shlex.quote(_WG_PRIVKEY_PATH)))
    pubkey = ssh_output(host, "cat {}".format(shlex.quote(_WG_PUBKEY_PATH)))
    return privkey, pubkey


def _render_hub_config(private_key, port, hub_overlay_ip, overlay_cidr):
    prefix = ipaddress.ip_network(overlay_cidr).prefixlen
    return (
        "[Interface]\n"
        "PrivateKey = {pk}\n"
        "Address = {ip}/{prefix}\n"
        "ListenPort = {port}\n"
    ).format(pk=private_key, ip=hub_overlay_ip, prefix=prefix, port=port)


def _render_spoke_config(private_key, node_overlay_ip, overlay_cidr,
                          allowed_ips_csv, hub_pubkey, hub_endpoint, hub_port):
    prefix = ipaddress.ip_network(overlay_cidr).prefixlen
    return (
        "[Interface]\n"
        "PrivateKey = {pk}\n"
        "Address = {ip}/{prefix}\n"
        "\n"
        "[Peer]\n"
        "PublicKey = {hpub}\n"
        "Endpoint = {hep}:{hport}\n"
        "AllowedIPs = {allowed}\n"
        "PersistentKeepalive = 25\n"
    ).format(pk=private_key, ip=node_overlay_ip, prefix=prefix,
              hpub=hub_pubkey, hep=hub_endpoint, hport=hub_port, allowed=allowed_ips_csv)


def _allocate_overlay_ip(hub_host, name, overlay_cidr):
    """
    Idempotently allocates (or returns the existing) overlay admin IP for
    `name` (a SITE name — see site_name_for()), tracked in _ALLOC_PATH on
    the hub itself. The hub's own address (network[1], e.g. 10.99.0.1 for
    10.99.0.0/16) is always reserved and never handed out here. Sequential
    allocation starting at the next address after the hub, skipping
    anything already recorded. Dies if the CIDR is exhausted.
    """
    ssh_run(hub_host, "mkdir -p {d} && touch {f}".format(
        d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_ALLOC_PATH)))
    existing = ssh_output(hub_host, "cat {}".format(shlex.quote(_ALLOC_PATH)))

    net = ipaddress.ip_network(overlay_cidr)
    hub_ip = net[1]
    used = {hub_ip}
    by_name = {}
    for line in existing.splitlines():
        parts = line.split()
        if len(parts) == 2:
            by_name[parts[0]] = parts[1]
            used.add(ipaddress.ip_address(parts[1]))

    if name in by_name:
        return by_name[name]

    for candidate in net.hosts():
        if candidate not in used:
            ssh_run(hub_host, "printf '%s %s\\n' {name} {ip} >> {f}".format(
                name=shlex.quote(name), ip=shlex.quote(str(candidate)), f=shlex.quote(_ALLOC_PATH)))
            return str(candidate)

    die("overlay CIDR '{}' is exhausted — no free address left for '{}'".format(overlay_cidr, name))


def _register_site(hub_host, site_name, site_cidr):
    """
    Idempotent upsert of `site_name`'s real subnet CIDR into the hub's own
    site registry (_SITE_REGISTRY_PATH) — rewrites the whole (small, one
    line per site) file with `site_name`'s line replaced or appended.
    Called every time a site's gateway (re-)asserts itself, so a site's
    subnet can change (rare, but not assumed impossible) without manual
    cleanup.
    """
    ssh_run(hub_host, "mkdir -p {d} && touch {f}".format(
        d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_SITE_REGISTRY_PATH)))
    existing = ssh_output(hub_host, "cat {}".format(shlex.quote(_SITE_REGISTRY_PATH)))
    kept = [line for line in existing.splitlines() if line.split()[:1] != [site_name]]
    kept.append("{} {}".format(site_name, site_cidr))
    content = "\n".join(kept) + "\n"
    ssh_run(hub_host, "cat > {}".format(shlex.quote(_SITE_REGISTRY_PATH)), input_text=content)


def list_other_sites(hub_host, exclude_site_name=None):
    """
    Returns [(site_name, site_cidr), ...] for every site currently
    registered on the hub, excluding `exclude_site_name` (a site's own
    entry, when the caller IS that site). Public (no leading underscore) —
    called directly by setup_vm.py to know which remote subnets to route
    a newly-provisioned node's traffic toward.
    """
    ssh_run(hub_host, "mkdir -p {d} && touch {f}".format(
        d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_SITE_REGISTRY_PATH)))
    raw = ssh_output(hub_host, "cat {}".format(shlex.quote(_SITE_REGISTRY_PATH)))
    result = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] != exclude_site_name:
            result.append((parts[0], parts[1]))
    return result


def add_peer_to_hub(hub_host, peer_name, peer_pubkey, allowed_cidrs):
    """
    Idempotently registers `peer_name` (a site gateway) as a WireGuard
    peer on the hub, with `allowed_cidrs` (a list of full CIDR strings —
    e.g. ["10.99.0.5/32", "192.168.88.0/24"], its own overlay admin
    address PLUS its real site subnet) as AllowedIPs — this is what makes
    the hub route real site-subnet traffic to the right gateway, not just
    reach the gateway's own admin address. Applies immediately via `wg
    set` (live, no interface restart), AND persists a marked [Peer] block
    in wg0.conf (so the peer survives a hub reboot / wg-quick restart,
    which only reads the config file at startup).

    ALSO adds a real kernel route for every CIDR in `allowed_cidrs` (`ip
    route replace <cidr> dev wg0`) — a real bug found live-testing
    2026-09-18: `wg set` alone updates WireGuard's own crypto-routing
    table (which peer a packet decrypts from / encrypts to) but, unlike
    `wg-quick up` parsing a config file's [Peer] blocks, does NOT insert
    the matching kernel IP route. Without this, the hub correctly
    recognizes and decrypts inbound traffic from a newly-added site, but
    has no route to send a REPLY back out through wg0 for that site's
    subnet — confirmed live: forward-direction ping traffic reached the
    hub (wg0 TX counters on the sending side incremented correctly) but
    100% of replies were lost, and the hub's own `ip route show` was
    missing the peer's site subnet entirely despite it being present in
    `wg show wg0`'s own allowed-ips. `ip route replace` (not `add`) is
    idempotent — safe to call on every join/re-join, and no-op error if
    an identical route from an earlier peer already covers it.
    """
    allowed_csv = ",".join(allowed_cidrs)
    marker = "# peer: {}".format(peer_name)
    already = ssh_run(hub_host, "grep -qxF {} {}".format(
        shlex.quote(marker), shlex.quote(_WG_CONF_PATH)), check=False).returncode == 0
    if not already:
        block = "\n{marker}\n[Peer]\nPublicKey = {pk}\nAllowedIPs = {allowed}\n".format(
            marker=marker, pk=peer_pubkey, allowed=allowed_csv)
        ssh_run(hub_host, "cat >> {}".format(shlex.quote(_WG_CONF_PATH)), input_text=block)
    ssh_run(hub_host, "wg set wg0 peer {} allowed-ips {} persistent-keepalive 25".format(
        shlex.quote(peer_pubkey), shlex.quote(allowed_csv)))
    for cidr in allowed_cidrs:
        ssh_run(hub_host, "ip route replace {} dev wg0".format(shlex.quote(cidr)))
    log("- Registered site gateway '{}' as a peer on the overlay hub, routing {}".format(
        peer_name, allowed_csv))


def remove_peer_from_hub(hub_host, peer_name):
    """
    Best-effort removal of `peer_name`'s peer entry from the hub — both the
    LIVE wg0 interface state (`wg set ... remove`) and the persisted
    wg0.conf block. Looked up entirely from the hub's own recorded state
    (the marker comment + its own PublicKey line), never from the peer
    itself. Never dies (mirrors ensure_cloud_dns_vm()'s own best-effort
    teardown-time cleanup convention elsewhere in this project) — a hub
    that's unreachable, or a peer that was never registered, is a silent
    no-op. Nothing calls this automatically today — see module docstring
    ("the hub itself is never auto-destroyed") — site gateways are shared,
    persistent infrastructure, not torn down per-node.
    """
    marker = "# peer: {}".format(peer_name)
    script = (
        "ln=$(grep -nxF {marker} {conf} 2>/dev/null | head -1 | cut -d: -f1); "
        "if [ -n \"$ln\" ]; then "
        "pubkey=$(sed -n \"$((ln+2))p\" {conf} | awk '{{print $3}}'); "
        "[ -n \"$pubkey\" ] && wg set wg0 peer \"$pubkey\" remove 2>/dev/null; "
        "end=$(awk -v start=\"$ln\" 'NR>start && /^$/{{print NR; exit}}' {conf}); "
        "[ -z \"$end\" ] && end=$(wc -l < {conf}); "
        "sed -i \"${{ln}},${{end}}d\" {conf}; "
        "fi"
    ).format(marker=shlex.quote(marker), conf=shlex.quote(_WG_CONF_PATH))
    ssh_run(hub_host, script, check=False)
    log("- Removed site gateway '{}' from the overlay hub (best-effort)".format(peer_name))


def ensure_overlay_hub_ready(hub_host, wg_port=DEFAULT_WG_PORT, overlay_cidr=DEFAULT_OVERLAY_CIDR,
                              site_name=None, site_cidr=None):
    """
    Idempotently initializes a WireGuard hub ON an ALREADY-REACHABLE host.
    Installs wireguard-tools, generates a keypair if needed, writes
    wg0.conf (hub side — no [Peer] blocks yet, those get appended by
    add_peer_to_hub() as site gateways join), enables IPv4 forwarding,
    brings wg0 up.

    If `site_name`/`site_cidr` are given, ALSO registers the hub's own
    site (its account's real subnet) in the site registry — the hub is a
    site gateway too, for its own account's nodes, same as every spoke;
    this is what lets a spoke route traffic to the hub-account's real
    nodes, not just to the hub's own admin address. Callers should also
    call backend.disable_source_dest_check() on the hub's own cloud
    instance (AWS-specific today) — not done here, since this function is
    cloud-account-agnostic on purpose.

    Returns (hub_overlay_ip, hub_pubkey).
    """
    ensure_wg_installed(hub_host)

    net = ipaddress.ip_network(overlay_cidr)
    hub_overlay_ip = str(net[1])

    conf_exists = ssh_run(hub_host, "test -f {}".format(shlex.quote(_WG_CONF_PATH)), check=False).returncode == 0
    if not conf_exists:
        privkey, _ = generate_wg_keypair(hub_host)
        conf = _render_hub_config(privkey, wg_port, hub_overlay_ip, overlay_cidr)
        ssh_run(hub_host, "mkdir -p {d} && cat > {f} && chmod 600 {f}".format(
            d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_WG_CONF_PATH)), input_text=conf)
        ssh_run(hub_host, "sysctl -w net.ipv4.ip_forward=1")
        ssh_run(hub_host, "grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || "
                           "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf")
        ssh_run(hub_host, "systemctl enable --now wg-quick@wg0")
        log("- Overlay hub initialized on {} (wg0 up, ip_forward enabled)".format(hub_host))
    else:
        # Already configured on a prior run — just make sure it's actually up (idempotent no-op
        # if it already is; `wg-quick up` on an already-up interface exits non-zero, so this only
        # brings it up when it's genuinely down, e.g. after a reboot without the enabled unit
        # having caught on yet).
        already_up = ssh_run(hub_host, "wg show wg0", check=False).returncode == 0
        if not already_up:
            ssh_run(hub_host, "systemctl enable --now wg-quick@wg0")
            log("- Overlay hub on {} was configured but not running — brought wg0 up".format(hub_host))

    hub_pubkey = ssh_output(hub_host, "cat {}".format(shlex.quote(_WG_PUBKEY_PATH)))

    if site_name and site_cidr:
        _register_site(hub_host, site_name, site_cidr)

    return hub_overlay_ip, hub_pubkey


def ensure_overlay_hub(backend, backend_name, iso_image, lab_setup_path, root_ssh_key,
                        wg_port=DEFAULT_WG_PORT, overlay_cidr=DEFAULT_OVERLAY_CIDR):
    """
    Idempotently ensures a cloud "overlay hub" VM exists on `backend` and
    is running as this lab's WireGuard hub. Mirrors
    backends.ensure_cloud_dns_vm()'s own reuse-or-create convention
    exactly (fixed name — see hub_vm_name() — same ISO_IMAGE the calling
    lab already configured, smallest instance size, and a real cloud-init
    user-data — see _create_gateway_vm()): this genuinely is "the
    automation VM's BIND role, transplanted into a cloud", the same move
    applied to routing instead of DNS (see TODO).

    Opens UDP/wg_port via the existing open_ports create_vm() kwarg (the
    same mechanism AWSBackend already uses for aws_open_ports — see
    AWSBackend._ensure_security_group_access()) — the hub is the only
    gateway that needs an INBOUND port open; every spoke gateway connects
    OUT to it (see ensure_site_gateway_vm(), which does not open a port).

    Returns (hub_public_ip, hub_overlay_ip, hub_pubkey). Does NOT register
    the hub's own site — call ensure_overlay_hub_ready() again (or rely on
    the one this function already made, it's idempotent) with
    site_name/site_cidr for that, and disable_source_dest_check()
    separately.
    """
    acct = getattr(backend, "account", "") or ""
    vm_name = hub_vm_name(backend_name, acct)

    if backend.vm_exists(vm_name):
        hub_public_ip = backend.get_ip(vm_name)
        if not hub_public_ip:
            die("overlay hub VM '{}' exists on backend '{}' but reported no IP — check it "
                "manually before retrying".format(vm_name, backend_name))
        log("- Reusing existing overlay hub VM \"{}\" ({})".format(vm_name, hub_public_ip))
    else:
        hub_public_ip = _create_gateway_vm(backend, backend_name, vm_name, iso_image, lab_setup_path,
                                            root_ssh_key, open_ports=["{}/udp".format(wg_port)])

    hub_overlay_ip, hub_pubkey = ensure_overlay_hub_ready(hub_public_ip, wg_port=wg_port,
                                                           overlay_cidr=overlay_cidr)
    return hub_public_ip, hub_overlay_ip, hub_pubkey


def ensure_site_gateway_vm(backend, backend_name, iso_image, lab_setup_path, root_ssh_key):
    """
    Idempotently ensures a small, cheap, DEDICATED gateway VM exists for a
    cloud account that is NOT the overlay hub's own account — reuse-or-
    create, same convention as ensure_overlay_hub()/ensure_cloud_dns_vm(),
    fixed name (hub_vm_name()), smallest instance size. Unlike the hub, no
    inbound port is opened — a spoke-role gateway only ever connects OUT
    to the hub (PersistentKeepalive handles the NAT-traversal side).

    Returns the VM's real (public or private, whichever create_vm()/get_ip()
    report — a spoke gateway only needs SSH reachability from the
    automation node driving this, not necessarily a public IP if this ever
    runs from inside the same cloud) IP.

    NOT live-tested — this session only had one real cloud account (the
    hub's own), so the "second, non-hub cloud account gets its own
    dedicated gateway" path was exercised only by code review + the mocked
    test suite, never against a real second account.
    """
    acct = getattr(backend, "account", "") or ""
    vm_name = hub_vm_name(backend_name, acct)

    if backend.vm_exists(vm_name):
        ip = backend.get_ip(vm_name)
        if not ip:
            die("site gateway VM '{}' exists on backend '{}' but reported no IP — check it "
                "manually before retrying".format(vm_name, backend_name))
        log("- Reusing existing site gateway VM \"{}\" ({})".format(vm_name, ip))
        return ip

    return _create_gateway_vm(backend, backend_name, vm_name, iso_image, lab_setup_path, root_ssh_key)


def ensure_overlay_site_gateway(site_name, gateway_host, hub_host, hub_pubkey, hub_wg_port,
                                 site_cidr, overlay_cidr=DEFAULT_OVERLAY_CIDR):
    """
    Idempotently makes `gateway_host` the WireGuard SITE GATEWAY for
    `site_name`, advertising `site_cidr` (that site's real subnet) to the
    hub — real site-to-site routing, not per-node overlay membership (see
    module docstring for why this replaced the original per-node design).

    Installs wireguard-tools, generates (or reuses) its own keypair,
    allocates itself a stable overlay ADMIN IP (tracked on the hub — see
    _allocate_overlay_ip(), keyed by site_name), registers its own site
    (_register_site()) and fetches every OTHER currently-known site so its
    own [Peer] AllowedIPs-from-hub covers all of them (refreshed on every
    call — see the module docstring's "not retroactive" caveat), writes
    wg0.conf, brings the interface up, enables IPv4 forwarding on itself
    (it now forwards for its own site's other nodes), and registers itself
    as a peer on the hub with AllowedIPs = its own overlay admin /32 PLUS
    its real site_cidr.

    Idempotent end to end — a re-run on an already-set-up gateway reuses
    its existing key/admin-IP and just refreshes routing knowledge.

    Returns the gateway's own overlay admin IP (rarely needed by callers —
    what matters is that site_cidr is now reachable through the tunnel).
    """
    ensure_wg_installed(gateway_host)
    privkey, pubkey = generate_wg_keypair(gateway_host)
    gateway_overlay_ip = _allocate_overlay_ip(hub_host, site_name, overlay_cidr)

    _register_site(hub_host, site_name, site_cidr)
    other_sites = list_other_sites(hub_host, exclude_site_name=site_name)
    allowed_ips = [overlay_cidr] + [cidr for _, cidr in other_sites]

    conf = _render_spoke_config(privkey, gateway_overlay_ip, overlay_cidr, ",".join(allowed_ips),
                                 hub_pubkey, hub_host, hub_wg_port)
    ssh_run(gateway_host, "mkdir -p {d} && cat > {f} && chmod 600 {f}".format(
        d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_WG_CONF_PATH)), input_text=conf)

    already_up = ssh_run(gateway_host, "wg show wg0", check=False).returncode == 0
    if already_up:
        # Config may have changed (new remote sites, hub recreated, ...) — reload rather than
        # assume the running interface still matches the file on disk.
        ssh_run(gateway_host, "wg-quick down wg0 2>/dev/null; wg-quick up wg0")
    else:
        ssh_run(gateway_host, "systemctl enable --now wg-quick@wg0")

    ssh_run(gateway_host, "sysctl -w net.ipv4.ip_forward=1")
    ssh_run(gateway_host, "grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || "
                           "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf")

    add_peer_to_hub(hub_host, site_name, pubkey, ["{}/32".format(gateway_overlay_ip), site_cidr])
    log("- Site '{}' gateway ({}) joined the overlay as {}, advertising {}".format(
        site_name, gateway_host, gateway_overlay_ip, site_cidr))
    return gateway_overlay_ip


def ensure_route_via_site_gateway(node_host, gateway_lan_ip, remote_cidrs):
    """
    Pushes a plain, PERSISTENT IP route on `node_host` (an ordinary lab
    node — never itself a WireGuard peer) for each CIDR in `remote_cidrs`,
    via `gateway_lan_ip` (its own site's gateway, reachable directly on
    the local subnet/bridge). This is the literal "local route on each
    host pointing at their local automation VM" — see module docstring.

    Persistence is a small systemd oneshot unit (ExecStart runs a plain
    `ip route replace` script) rather than any distro-specific network
    config file syntax (wicked ifroute- / NetworkManager / netplan /
    systemd-networkd all differ) — every OS image this project targets
    already has systemd, and `ip route replace` is idempotent on its own,
    so the unit is safe to re-run on every boot unconditionally.

    No-op if `remote_cidrs` is empty (nothing to route yet — e.g. this is
    the very first node at the very first site).
    """
    if not remote_cidrs:
        return
    lines = ["#!/bin/sh"] + [
        "ip route replace {} via {}".format(cidr, gateway_lan_ip) for cidr in remote_cidrs
    ]
    script = "\n".join(lines) + "\n"
    ssh_run(node_host, "cat > {f} && chmod 755 {f}".format(f=shlex.quote(_ROUTES_SCRIPT_PATH)),
            input_text=script)

    unit = (
        "[Unit]\n"
        "Description=lab-in-a-box overlay routes\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart={script}\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).format(script=_ROUTES_SCRIPT_PATH)
    ssh_run(node_host, "cat > {}".format(shlex.quote(_ROUTES_UNIT_PATH)), input_text=unit)
    ssh_run(node_host, "systemctl daemon-reload && systemctl enable --now lab-overlay-routes.service")
    log("- Routed {} via local site gateway {} on {}".format(
        ", ".join(remote_cidrs), gateway_lan_ip, node_host))
