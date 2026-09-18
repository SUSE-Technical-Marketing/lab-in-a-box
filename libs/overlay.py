#!/usr/bin/env python3
# Part of lab-in-a-box — cross-cloud WireGuard overlay network.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/overlay.py — hub-and-spoke WireGuard overlay so lab nodes in
different clouds (and the home libvirt lab) can reach each other as if on
one LAN, without any per-provider real-subnet networking.

Design (see repo TODO, "cross-cloud WireGuard overlay plan", 2026-09-10,
locked via AskUserQuestion + follow-up discussion; started 2026-09-18):

  - Hub-and-spoke, ONE hub. The hub is a cloud automation VM — the SAME
    kind of component that already runs BIND for ensure_cloud_dns_vm(),
    given net.ipv4.ip_forward=1 + a wg0 SERVER interface instead — with a
    stable public IP, running wg on UDP/OVERLAY_WG_PORT (default 51820).
    Every other node (cloud lab nodes in every account, plus the home
    automation VM) connects TO it as a spoke with PersistentKeepalive=25.
  - Overlay CIDR (default 10.99.0.0/16). The hub always takes the first
    usable address (e.g. 10.99.0.1). Each spoke's [Interface] Address is
    given the FULL overlay prefix (not /32) — the standard wg-quick trick
    that makes the kernel install a route for the whole overlay CIDR via
    wg0 automatically, so spoke-to-spoke traffic (through the hub, which
    forwards) needs no extra routes on the spoke. Each spoke has exactly
    ONE [Peer] (the hub). The hub gets one [Peer] per spoke
    (AllowedIPs=<that node's overlay /32>).
  - Keys are generated ON each node itself (mirrors
    lab_creation.ensure_lab_ssh_key()'s own "never locally, never
    centrally" convention) — a private key never transits SSH.
  - Overlay IP allocation is tracked in a plain text file
    (/etc/wireguard/overlay_allocations, "<node_name> <ip>" lines) ON THE
    HUB — sequential, good enough for a lab-scale overlay (a handful to a
    few dozen nodes), not a general-purpose IPAM.

NOT YET DONE (real, flagged gaps, not silently glossed over):
  - Per-provider real-subnet net-to-net: reaching a non-WireGuard VM
    inside a cloud VPC is out of scope — only WG-spoke nodes are reachable
    through the overlay. See the TODO entry's own "EXTRA" estimate.
  - DNS: this module allocates/tracks overlay IPs but does not itself
    register a DNS record for them. A caller that wants a node resolvable
    by hostname OVER the overlay (as opposed to its "real" IP) needs to
    feed the returned overlay IP into the existing add_to_dns()/
    remote_dns_servers mechanism itself — not done here, to avoid
    duplicating that mechanism's own zone-selection logic.
  - The hub itself is never auto-destroyed (same convention as
    backends.ensure_cloud_dns_vm()'s DNS VM — shared, persistent,
    reused across every lab/run on that account, not per-lab).
  - Single hub = SPOF; all cross-site traffic hairpins through one region.
    Fine for a lab, per the TODO's own RISKS/NOTES.
"""

import ipaddress
import shlex

from lab_creation import ssh_run, ssh_output, log, warn, die


DEFAULT_OVERLAY_CIDR = "10.99.0.0/16"
DEFAULT_WG_PORT = 51820

_WG_KEY_DIR = "/etc/wireguard"
_WG_PRIVKEY_PATH = _WG_KEY_DIR + "/wg0.key"
_WG_PUBKEY_PATH = _WG_KEY_DIR + "/wg0.pub"
_WG_CONF_PATH = _WG_KEY_DIR + "/wg0.conf"
_ALLOC_PATH = _WG_KEY_DIR + "/overlay_allocations"

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
                          hub_pubkey, hub_endpoint, hub_port):
    prefix = ipaddress.ip_network(overlay_cidr).prefixlen
    return (
        "[Interface]\n"
        "PrivateKey = {pk}\n"
        "Address = {ip}/{prefix}\n"
        "\n"
        "[Peer]\n"
        "PublicKey = {hpub}\n"
        "Endpoint = {hep}:{hport}\n"
        "AllowedIPs = {cidr}\n"
        "PersistentKeepalive = 25\n"
    ).format(pk=private_key, ip=node_overlay_ip, prefix=prefix,
              hpub=hub_pubkey, hep=hub_endpoint, hport=hub_port, cidr=overlay_cidr)


def _allocate_overlay_ip(hub_host, node_name, overlay_cidr):
    """
    Idempotently allocates (or returns the existing) overlay IP for
    node_name, tracked in _ALLOC_PATH on the hub itself. The hub's own
    address (network[1], e.g. 10.99.0.1 for 10.99.0.0/16) is always
    reserved and never handed out here. Sequential allocation starting
    at the next address after the hub, skipping anything already
    recorded. Dies if the CIDR is exhausted.
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

    if node_name in by_name:
        return by_name[node_name]

    for candidate in net.hosts():
        if candidate not in used:
            ssh_run(hub_host, "printf '%s %s\\n' {name} {ip} >> {f}".format(
                name=shlex.quote(node_name), ip=shlex.quote(str(candidate)), f=shlex.quote(_ALLOC_PATH)))
            return str(candidate)

    die("overlay CIDR '{}' is exhausted — no free address left for '{}'".format(overlay_cidr, node_name))


def add_peer_to_hub(hub_host, node_name, peer_pubkey, peer_overlay_ip):
    """
    Idempotently registers `node_name` as a WireGuard peer on the hub:
    applies it immediately via `wg set` (live, no interface restart), AND
    persists a marked [Peer] block in wg0.conf (so the peer survives a hub
    reboot / wg-quick restart, which only reads the config file at
    startup) — same "apply live + persist to the file that will be re-read
    later" split this project already uses elsewhere (e.g. Salt highstate
    application vs. the state definition itself).
    """
    marker = "# peer: {}".format(node_name)
    already = ssh_run(hub_host, "grep -qxF {} {}".format(
        shlex.quote(marker), shlex.quote(_WG_CONF_PATH)), check=False).returncode == 0
    if not already:
        block = "\n{marker}\n[Peer]\nPublicKey = {pk}\nAllowedIPs = {ip}/32\n".format(
            marker=marker, pk=peer_pubkey, ip=peer_overlay_ip)
        ssh_run(hub_host, "cat >> {}".format(shlex.quote(_WG_CONF_PATH)), input_text=block)
    ssh_run(hub_host, "wg set wg0 peer {} allowed-ips {}/32 persistent-keepalive 25".format(
        shlex.quote(peer_pubkey), shlex.quote(peer_overlay_ip)))
    log("- Registered '{}' ({}) as a peer on the overlay hub".format(node_name, peer_overlay_ip))


def remove_peer_from_hub(hub_host, node_name):
    """
    Best-effort removal of `node_name`'s peer entry from the hub — both the
    LIVE wg0 interface state (`wg set ... remove`) and the persisted
    wg0.conf block. Looked up entirely from the hub's own recorded state
    (the marker comment + its own PublicKey line), never from the node
    itself — by the time this runs (destroy_vm.py teardown), the node may
    already be gone. Never dies (mirrors ensure_cloud_dns_vm()'s own
    best-effort teardown-time cleanup convention elsewhere in this
    project) — a hub that's unreachable, or a node that was never a member,
    is a silent no-op.
    """
    marker = "# peer: {}".format(node_name)
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
    log("- Removed '{}' from the overlay hub (best-effort)".format(node_name))


def ensure_overlay_hub_ready(hub_host, wg_port=DEFAULT_WG_PORT, overlay_cidr=DEFAULT_OVERLAY_CIDR):
    """
    Idempotently initializes a WireGuard hub ON an ALREADY-REACHABLE host
    (either one just created by ensure_overlay_hub() below, or an
    operator-designated existing host via OVERLAY_HUB_HOST). Installs
    wireguard-tools, generates a keypair if needed, writes wg0.conf (hub
    side — no [Peer] blocks yet, those get appended by add_peer_to_hub()
    as spokes join), enables IPv4 forwarding, brings wg0 up. Returns
    (hub_overlay_ip, hub_pubkey).
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
    return hub_overlay_ip, hub_pubkey


def ensure_overlay_hub(backend, backend_name, iso_image, lab_setup_path,
                        wg_port=DEFAULT_WG_PORT, overlay_cidr=DEFAULT_OVERLAY_CIDR):
    """
    Idempotently ensures a cloud "overlay hub" VM exists on `backend` and
    is running as this lab's WireGuard hub. Mirrors
    backends.ensure_cloud_dns_vm()'s own reuse-or-create convention
    exactly (fixed name — "lab-overlay-hub-<backend>[-<account>]" — same
    ISO_IMAGE the calling lab already configured, smallest instance size):
    this genuinely is "the automation VM's BIND role, transplanted into a
    cloud", the same move applied to routing instead of DNS (see TODO).

    Opens UDP/wg_port via the existing open_ports create_vm() kwarg (the
    same mechanism AWSBackend already uses for aws_open_ports — see
    AWSBackend._ensure_security_group_access()).

    Returns (hub_public_ip, hub_overlay_ip, hub_pubkey).
    """
    acct = getattr(backend, "account", "") or ""
    if acct in ("", "default"):
        hub_vm_name = "lab-overlay-hub-{}".format(backend_name)
    else:
        hub_vm_name = "lab-overlay-hub-{}-{}".format(backend_name, acct)

    if backend.vm_exists(hub_vm_name):
        hub_public_ip = backend.get_ip(hub_vm_name)
        if not hub_public_ip:
            die("overlay hub VM '{}' exists on backend '{}' but reported no IP — check it "
                "manually before retrying".format(hub_vm_name, backend_name))
        log("- Reusing existing overlay hub VM \"{}\" ({})".format(hub_vm_name, hub_public_ip))
    else:
        log("- No overlay hub VM found for backend \"{}\" — creating \"{}\"".format(
            backend_name, hub_vm_name))
        backend.copy_vm_image(iso_image, hub_vm_name, 8, config_method="cloud-init")
        backend.push_provisioning_files(hub_vm_name, config_method="cloud-init")
        hub_public_ip = backend.create_vm(
            hub_vm_name, 1, 512, 8, None,
            config_method="cloud-init", iso_image=iso_image, mymac=None,
            open_ports=["{}/udp".format(wg_port)],
        )
        if not hub_public_ip:
            die("overlay hub VM '{}' was created on backend '{}' but create_vm() reported "
                "no IP".format(hub_vm_name, backend_name))

    hub_overlay_ip, hub_pubkey = ensure_overlay_hub_ready(hub_public_ip, wg_port=wg_port,
                                                           overlay_cidr=overlay_cidr)
    return hub_public_ip, hub_overlay_ip, hub_pubkey


def ensure_overlay_spoke(node_host, node_name, hub_public_ip, hub_pubkey, hub_wg_port,
                          overlay_cidr=DEFAULT_OVERLAY_CIDR):
    """
    Idempotently makes `node_host` a WireGuard spoke of the overlay hub:
    installs wireguard-tools, generates (or reuses) its own keypair,
    allocates it a stable overlay IP (tracked on the hub — see
    _allocate_overlay_ip()), writes wg0.conf with a single [Peer] (the hub
    only, per the module's hub-and-spoke design), brings the interface up,
    and registers itself as a peer on the hub. Idempotent end to end — a
    re-run on an already-joined node reuses its existing key/IP and just
    re-asserts the hub-side peer registration.

    Returns the node's own overlay IP.
    """
    ensure_wg_installed(node_host)
    privkey, pubkey = generate_wg_keypair(node_host)
    node_overlay_ip = _allocate_overlay_ip(hub_public_ip, node_name, overlay_cidr)

    conf = _render_spoke_config(privkey, node_overlay_ip, overlay_cidr,
                                 hub_pubkey, hub_public_ip, hub_wg_port)
    ssh_run(node_host, "mkdir -p {d} && cat > {f} && chmod 600 {f}".format(
        d=shlex.quote(_WG_KEY_DIR), f=shlex.quote(_WG_CONF_PATH)), input_text=conf)

    already_up = ssh_run(node_host, "wg show wg0", check=False).returncode == 0
    if already_up:
        # Config may have changed (e.g. hub was recreated with a new pubkey/IP) — reload
        # rather than assume the running interface still matches the file on disk.
        ssh_run(node_host, "wg-quick down wg0 2>/dev/null; wg-quick up wg0")
    else:
        ssh_run(node_host, "systemctl enable --now wg-quick@wg0")

    add_peer_to_hub(hub_public_ip, node_name, pubkey, node_overlay_ip)
    log("- '{}' joined the overlay as {} (hub {})".format(node_name, node_overlay_ip, hub_public_ip))
    return node_overlay_ip


def remove_overlay_spoke(hub_public_ip, node_name):
    """
    Teardown counterpart to ensure_overlay_spoke() — removes `node_name`
    from the hub only (see remove_peer_from_hub()'s own docstring for why
    the node itself is never contacted here). Does NOT touch the node's
    own wg0 interface/config — irrelevant once the node itself is being
    destroyed by destroy_vm.py, and this function is deliberately usable
    even when the node is already gone.
    """
    remove_peer_from_hub(hub_public_ip, node_name)
