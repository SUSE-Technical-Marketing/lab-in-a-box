#!/usr/bin/env python3
# Unit tests for libs/overlay.py — the cross-cloud, SITE-TO-SITE WireGuard
# overlay (hub-and-spoke among SITE GATEWAYS, never individual lab nodes —
# see the module's own docstring). No real
# WireGuard/SSH: ssh_run/ssh_output are monkeypatched directly on the
# module (same convention as 52_ansible_control_node_test.py), and a fake
# VMBackend stands in for backends.ensure_cloud_dns_vm()'s own
# reuse-vs-create test pattern (see 42_cloud_dns_vm_test.py). Run from
# 53_overlay.sh, in its own container — see tests/run_tests.sh.
import ipaddress
import sys
import tempfile
from unittest import mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import overlay  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
# host -> {cmd_substring: FakeResult} — first matching substring wins; falls back to rc=0/"" .
scripted = {}


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    for sub, result in scripted.get(hostname, {}).items():
        if sub in cmd:
            return result
    return FakeResult(returncode=0, stdout="")


def _fake_ssh_output(hostname, cmd):
    return _fake_ssh_run(hostname, cmd, capture=True).stdout.strip()


overlay.ssh_run = _fake_ssh_run
overlay.ssh_output = _fake_ssh_output


def reset():
    calls.clear()
    scripted.clear()


# ── site_name_for() / hub_vm_name(): naming conventions ────────────────────
check("site_name_for(): libvirt is always 'home', account ignored",
      overlay.site_name_for("libvirt", None) == "home" and overlay.site_name_for("libvirt", "x") == "home")
check("site_name_for(): harvester is also 'home' (on-prem, like libvirt)",
      overlay.site_name_for("harvester", None) == "home")
check("site_name_for(): a cloud backend with no account -> '<backend>-default'",
      overlay.site_name_for("aws", None) == "aws-default" and overlay.site_name_for("aws", "") == "aws-default")
check("site_name_for(): a cloud backend with a named account -> '<backend>-<account>'",
      overlay.site_name_for("aws", "aws-tmm") == "aws-aws-tmm")

check("hub_vm_name(): no/default account -> 'lab-overlay-gw-<backend>'",
      overlay.hub_vm_name("aws", None) == "lab-overlay-gw-aws"
      and overlay.hub_vm_name("aws", "default") == "lab-overlay-gw-aws")
check("hub_vm_name(): named account -> 'lab-overlay-gw-<backend>-<account>'",
      overlay.hub_vm_name("aws", "aws-tmm") == "lab-overlay-gw-aws-aws-tmm")


# ── _render_hub_config / _render_spoke_config: real content, correct prefix ─
hub_conf = overlay._render_hub_config("HUBPRIV", 51820, "10.99.0.1", "10.99.0.0/16")
check("_render_hub_config: embeds the private key", "PrivateKey = HUBPRIV" in hub_conf)
check("_render_hub_config: Address carries the overlay CIDR's own prefix length (/16)",
      "Address = 10.99.0.1/16" in hub_conf)
check("_render_hub_config: ListenPort is set", "ListenPort = 51820" in hub_conf)
check("_render_hub_config: has no [Peer] block — peers are added live via wg set, not baked in",
      "[Peer]" not in hub_conf)

spoke_conf = overlay._render_spoke_config("SPOKEPRIV", "10.99.0.2", "10.99.0.0/16",
                                           "10.99.0.0/16,192.168.88.0/24", "HUBPUB", "203.0.113.50", 51820)
check("_render_spoke_config: embeds the private key", "PrivateKey = SPOKEPRIV" in spoke_conf)
check("_render_spoke_config: Address carries the OVERLAY CIDR's own prefix (its admin address)",
      "Address = 10.99.0.2/16" in spoke_conf)
check("_render_spoke_config: has exactly ONE [Peer] — the hub only (hub-and-spoke)",
      spoke_conf.count("[Peer]") == 1)
check("_render_spoke_config: AllowedIPs is exactly the caller-supplied list (overlay admin CIDR "
      "UNION every other known site's real subnet) — real site-to-site routing, not a single /32",
      "AllowedIPs = 10.99.0.0/16,192.168.88.0/24" in spoke_conf)
check("_render_spoke_config: PersistentKeepalive is set (needed behind NAT)",
      "PersistentKeepalive = 25" in spoke_conf)
check("_render_spoke_config: Endpoint points at the real hub IP:port",
      "Endpoint = 203.0.113.50:51820" in spoke_conf)


# ── generate_wg_keypair: idempotent — reuse vs generate (unchanged mechanics) ──
reset()
scripted["hostA"] = {
    "test -f": FakeResult(returncode=1),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="PRIVKEYDATA\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="PUBKEYDATA\n"),
}
priv, pub = overlay.generate_wg_keypair("hostA")
check("generate_wg_keypair: returns the real private key content", priv == "PRIVKEYDATA")
check("generate_wg_keypair: returns the real public key content", pub == "PUBKEYDATA")
check("generate_wg_keypair: generates via 'wg genkey | tee ... | wg pubkey' when no key exists",
      any("wg genkey" in c and "wg pubkey" in c for _, c, _ in calls))

reset()
scripted["hostA"] = {
    "test -f": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="EXISTINGPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="EXISTINGPUB\n"),
}
priv2, pub2 = overlay.generate_wg_keypair("hostA")
check("generate_wg_keypair: reuses an existing key instead of regenerating",
      priv2 == "EXISTINGPRIV" and not any("wg genkey" in c for _, c, _ in calls))


# ── _allocate_overlay_ip: sequential, reserves the hub address, idempotent per site name ──
reset()
scripted["hub1"] = {"cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="")}
ip1 = overlay._allocate_overlay_ip("hub1", "home", "10.99.0.0/24")
check("_allocate_overlay_ip: first allocation skips the hub's own reserved address (.1)",
      ip1 != "10.99.0.1")
check("_allocate_overlay_ip: allocates a real address inside the given CIDR",
      ipaddress.ip_address(ip1) in ipaddress.ip_network("10.99.0.0/24"))
check("_allocate_overlay_ip: persists the allocation by appending to the hub's own file",
      any("overlay_allocations" in c and "home" in c and ">>" in c for _, c, _ in calls))

reset()
scripted["hub1"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="home 10.99.0.2\n"),
}
ip_again = overlay._allocate_overlay_ip("hub1", "home", "10.99.0.0/24")
check("_allocate_overlay_ip: a site already recorded gets its EXISTING ip back, not a new one",
      ip_again == "10.99.0.2" and not any(">>" in c for _, c, _ in calls))


# ── _register_site / list_other_sites: upsert + exclude-self listing ────────
reset()
scripted["hub1"] = {"cat /etc/wireguard/overlay_sites": FakeResult(returncode=0, stdout="")}
overlay._register_site("hub1", "home", "192.168.88.0/24")
check("_register_site: writes the site name + real CIDR to the registry file",
      any(inp and "home 192.168.88.0/24" in inp for _, _, inp in calls))

reset()
scripted["hub1"] = {
    "cat /etc/wireguard/overlay_sites": FakeResult(
        returncode=0, stdout="home 192.168.88.0/24\naws-aws-tmm 172.31.0.0/20\n"),
}
overlay._register_site("hub1", "home", "10.0.0.0/24")
written = next(inp for _, c, inp in calls if "cat > " in c and "overlay_sites" in c)
check("_register_site: replaces an existing site's line (upsert), not appends a duplicate",
      "home 10.0.0.0/24" in written and "home 192.168.88.0/24" not in written)
check("_register_site: leaves OTHER sites' entries untouched",
      "aws-aws-tmm 172.31.0.0/20" in written)

reset()
scripted["hub1"] = {
    "cat /etc/wireguard/overlay_sites": FakeResult(
        returncode=0, stdout="home 192.168.88.0/24\naws-aws-tmm 172.31.0.0/20\n"),
}
others = overlay.list_other_sites("hub1", exclude_site_name="home")
check("list_other_sites: returns every OTHER site as (name, cidr) pairs",
      others == [("aws-aws-tmm", "172.31.0.0/20")])
others_none_excluded = overlay.list_other_sites("hub1")
check("list_other_sites: with no exclusion, returns every registered site",
      set(others_none_excluded) == {("home", "192.168.88.0/24"), ("aws-aws-tmm", "172.31.0.0/20")})


# ── add_peer_to_hub: multi-CIDR AllowedIPs, idempotent ──
reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=1)}  # marker not yet present
overlay.add_peer_to_hub("hub1", "aws-aws-tmm", "GWPUB", ["10.99.0.5/32", "172.31.0.0/20"])
check("add_peer_to_hub: applies the peer LIVE via wg set with the FULL comma-joined AllowedIPs",
      any("wg set wg0 peer" in c and "GWPUB" in c and "10.99.0.5/32,172.31.0.0/20" in c
          for _, c, _ in calls))
check("add_peer_to_hub: persists a marked [Peer] block with the real site subnet, not just a /32",
      any(inp and "# peer: aws-aws-tmm" in inp and "PublicKey = GWPUB" in inp
          and "AllowedIPs = 10.99.0.5/32,172.31.0.0/20" in inp for _, _, inp in calls))
check("add_peer_to_hub: adds a REAL kernel route for every AllowedIPs CIDR via wg0 "
      "(`wg set` alone does not install the kernel route that wg-quick normally would, so the "
      "hub could decrypt traffic from a new peer but had no route to reply)",
      any("ip route replace 10.99.0.5/32 dev wg0" in c for _, c, _ in calls)
      and any("ip route replace 172.31.0.0/20 dev wg0" in c for _, c, _ in calls))

reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=0)}  # marker already present
overlay.add_peer_to_hub("hub1", "aws-aws-tmm", "GWPUB", ["10.99.0.5/32", "172.31.0.0/20"])
check("add_peer_to_hub: does NOT re-append the [Peer] block when the marker already exists",
      not any(inp and "# peer: aws-aws-tmm" in inp for _, _, inp in calls))
check("add_peer_to_hub: still re-asserts the live wg set (idempotent, cheap, always correct)",
      any("wg set wg0 peer" in c for _, c, _ in calls))


# ── ensure_overlay_hub_ready: init vs already-up vs down, + optional site registration ──
reset()
scripted["hub2"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=1),
    "test -f": FakeResult(returncode=1),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="HUBPUBKEY\n"),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="HUBPRIVKEY\n"),
}
hub_ip, hub_pub = overlay.ensure_overlay_hub_ready("hub2", wg_port=51820, overlay_cidr="10.99.0.0/16")
check("ensure_overlay_hub_ready: hub takes the first usable overlay address", hub_ip == "10.99.0.1")
check("ensure_overlay_hub_ready: returns the real hub public key", hub_pub == "HUBPUBKEY")
check("ensure_overlay_hub_ready: first-time init enables IPv4 forwarding",
      any("net.ipv4.ip_forward=1" in c for _, c, _ in calls))
check("ensure_overlay_hub_ready: first-time init enables+starts wg-quick@wg0",
      any("systemctl enable --now wg-quick@wg0" in c for _, c, _ in calls))
check("ensure_overlay_hub_ready: writes a wg0.conf with NO [Peer] block yet (spokes join later)",
      any(inp and inp.startswith("[Interface]") and "[Peer]" not in inp for _, _, inp in calls))
check("ensure_overlay_hub_ready: with no site_name/site_cidr given, never touches the site registry",
      not any("overlay_sites" in c for _, c, _ in calls))

reset()
scripted["hub2"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=0),
    "wg show wg0": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="HUBPUBKEY\n"),
    "cat /etc/wireguard/overlay_sites": FakeResult(returncode=0, stdout=""),
}
overlay.ensure_overlay_hub_ready("hub2", wg_port=51820, overlay_cidr="10.99.0.0/16",
                                  site_name="aws-aws-tmm", site_cidr="172.31.0.0/20")
check("ensure_overlay_hub_ready: already configured+up — does NOT re-run wg genkey or rewrite the conf",
      not any("wg genkey" in c for _, c, _ in calls)
      and not any(inp and inp.startswith("[Interface]") for _, _, inp in calls))
check("ensure_overlay_hub_ready: WITH site_name/site_cidr given, registers the hub's own site too "
      "(the hub is a site gateway for its own account's nodes, same as every spoke)",
      any(inp and "aws-aws-tmm 172.31.0.0/20" in inp for _, _, inp in calls))

reset()
scripted["hub2"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=0),
    "wg show wg0": FakeResult(returncode=1),  # configured but down (e.g. after a reboot)
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="HUBPUBKEY\n"),
}
overlay.ensure_overlay_hub_ready("hub2", wg_port=51820, overlay_cidr="10.99.0.0/16")
check("ensure_overlay_hub_ready: configured but down — brings wg0 up without regenerating keys",
      any("systemctl enable --now wg-quick@wg0" in c for _, c, _ in calls)
      and not any("wg genkey" in c for _, c, _ in calls))


# ── ensure_overlay_hub / ensure_site_gateway_vm: reuse vs create (fake backend) ──
class _FakeBackendReuse:
    def __init__(self):
        self.account = ""
        self.calls = []

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return True

    def get_ip(self, vm_name):
        self.calls.append(("get_ip", vm_name))
        return "203.0.113.9"

    def create_vm(self, *a, **kw):
        raise AssertionError("create_vm() must not be called when the VM already exists")


reset()
scripted["203.0.113.9"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=0),
    "wg show wg0": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="REUSEDPUB\n"),
}
reuse_backend = _FakeBackendReuse()
hub_public_ip, hub_overlay_ip, hub_pub = overlay.ensure_overlay_hub(
    reuse_backend, "aws", "ami-xyz", "/tmp/lab-setup", "ssh-ed25519 AAAAfake test@example")
check("ensure_overlay_hub: reuse path checks vm_exists() with the fixed 'lab-overlay-gw-<backend>' name",
      ("vm_exists", "lab-overlay-gw-aws") in reuse_backend.calls)
check("ensure_overlay_hub: reuse path returns the existing hub's real public IP",
      hub_public_ip == "203.0.113.9")
check("ensure_overlay_hub: reuse path still returns a real overlay IP + pubkey",
      hub_overlay_ip == "10.99.0.1" and hub_pub == "REUSEDPUB")


class _FakeBackendCreate:
    def __init__(self):
        self.account = "prod"
        self.calls = []

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return False

    def copy_vm_image(self, *a, **kw):
        self.calls.append(("copy_vm_image", a, kw))

    def push_provisioning_files(self, *a, **kw):
        self.calls.append(("push_provisioning_files", a, kw))

    def create_vm(self, vm_name, cpu, mem, dsk, network, **kwargs):
        self.calls.append(("create_vm", vm_name, cpu, mem, dsk, network, kwargs))
        return "203.0.113.77"


reset()
scripted["203.0.113.77"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=1),
    "test -f": FakeResult(returncode=1),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="NEWHUBPUB\n"),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="NEWHUBPRIV\n"),
}
create_backend = _FakeBackendCreate()
with tempfile.TemporaryDirectory() as tmp, mock.patch.object(overlay, "wait_for_ssh_command", return_value=True):
    hub_public_ip2, _, _ = overlay.ensure_overlay_hub(
        create_backend, "aws", "ami-xyz", tmp, "ssh-ed25519 AAAAfake test@example", wg_port=51820)
    check("ensure_overlay_hub: create path writes a cloud-init user-data file "
          "(create_vm() dies without one)",
          (Path(tmp) / "cloud-init" / "lab-overlay-gw-aws-prod_user-data").exists())
check("ensure_overlay_hub: create path uses the per-account gateway name ('lab-overlay-gw-aws-prod')",
      ("vm_exists", "lab-overlay-gw-aws-prod") in create_backend.calls)
check("ensure_overlay_hub: create path returns the newly created hub's real IP",
      hub_public_ip2 == "203.0.113.77")
create_call = next(c for c in create_backend.calls if c[0] == "create_vm")
check("ensure_overlay_hub: create path opens the WireGuard UDP port via the existing open_ports kwarg "
      "(the HUB needs an inbound listener — unlike a plain spoke gateway, see ensure_site_gateway_vm)",
      create_call[-1].get("open_ports") == ["51820/udp"])

reset()
create_backend2 = _FakeBackendCreate()
waits = []
with tempfile.TemporaryDirectory() as tmp, \
        mock.patch.object(overlay, "wait_for_ssh_command", side_effect=lambda h, c, **kw: waits.append((h, c)) or True):
    gw_ip = overlay.ensure_site_gateway_vm(create_backend2, "aws", "ami-xyz", tmp,
                                            "ssh-ed25519 AAAAfake test@example")
check("ensure_site_gateway_vm: create path returns the new gateway's real IP", gw_ip == "203.0.113.77")
check("ensure_site_gateway_vm: create path waits for cloud-init to finish before returning",
      waits == [("203.0.113.77", "test -f /var/lib/cloud/instance/boot-finished")])
died = []
with tempfile.TemporaryDirectory() as tmp, mock.patch.object(overlay, "wait_for_ssh_command", return_value=False), \
        mock.patch.object(overlay, "die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit(1))):
    try:
        overlay.ensure_site_gateway_vm(_FakeBackendCreate(), "aws", "ami-xyz", tmp, "ssh-ed25519 AAAAfake test@example")
    except SystemExit:
        pass
check("ensure_site_gateway_vm: dies naming the VM when cloud-init never finishes",
      len(died) == 1 and "lab-overlay-gw-aws-prod" in died[0])
gw_create_call = next(c for c in create_backend2.calls if c[0] == "create_vm")
check("ensure_site_gateway_vm: does NOT open any inbound port (a spoke gateway only connects OUT)",
      "open_ports" not in gw_create_call[-1] or not gw_create_call[-1].get("open_ports"))


# ── ensure_overlay_site_gateway: full site-join flow, AllowedIPs covers every known site ──
reset()
scripted["gw1"] = {
    "test -f": FakeResult(returncode=1),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="GWPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="GWPUB\n"),
    "wg show wg0": FakeResult(returncode=1),  # not up yet
}
scripted["hub3"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout=""),
    "cat /etc/wireguard/overlay_sites": FakeResult(returncode=0, stdout="aws-aws-tmm 172.31.0.0/20\n"),
    "grep -qxF": FakeResult(returncode=1),
}
gw_overlay_ip = overlay.ensure_overlay_site_gateway(
    "home", "gw1", "hub3", "HUBPUBKEY", 51820, "192.168.88.0/24", overlay_cidr="10.99.0.0/16")
check("ensure_overlay_site_gateway: returns a real allocated overlay admin IP",
      ipaddress.ip_address(gw_overlay_ip) in ipaddress.ip_network("10.99.0.0/16"))
check("ensure_overlay_site_gateway: registers its OWN site with its real subnet",
      any(h == "hub3" and inp and "home 192.168.88.0/24" in inp for h, _, inp in calls))
check("ensure_overlay_site_gateway: writes a spoke config whose AllowedIPs covers BOTH the overlay "
      "admin CIDR and every OTHER already-known site's real subnet (172.31.0.0/20)",
      any(h == "gw1" and inp and "AllowedIPs = 10.99.0.0/16,172.31.0.0/20" in inp
          for h, _, inp in calls))
check("ensure_overlay_site_gateway: brings the interface up via systemctl (not yet up)",
      any(h == "gw1" and "systemctl enable --now wg-quick@wg0" in c for h, c, _ in calls))
check("ensure_overlay_site_gateway: enables IPv4 forwarding on the gateway itself (it now forwards "
      "for its own site's other nodes, not just the hub)",
      any(h == "gw1" and "net.ipv4.ip_forward=1" in c for h, c, _ in calls))
check("ensure_overlay_site_gateway: registers itself on the hub with its admin /32 PLUS its real "
      "site subnet as AllowedIPs — real site-to-site routing",
      any(h == "hub3" and "wg set wg0 peer" in c and "GWPUB" in c
          and "192.168.88.0/24" in c for h, c, _ in calls))

# Re-run (already up, a new remote site appeared meanwhile): reloads via down+up, AllowedIPs grows.
reset()
scripted["gw1"] = {
    "test -f": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="GWPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="GWPUB\n"),
    "wg show wg0": FakeResult(returncode=0),  # already up
}
scripted["hub3"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="home 10.99.0.2\n"),
    "cat /etc/wireguard/overlay_sites": FakeResult(
        returncode=0, stdout="home 192.168.88.0/24\naws-aws-tmm 172.31.0.0/20\ngcp-default 10.0.0.0/24\n"),
    "grep -qxF": FakeResult(returncode=0),
}
gw_overlay_ip2 = overlay.ensure_overlay_site_gateway(
    "home", "gw1", "hub3", "HUBPUBKEY", 51820, "192.168.88.0/24", overlay_cidr="10.99.0.0/16")
check("ensure_overlay_site_gateway: re-join reuses its previously allocated overlay admin IP",
      gw_overlay_ip2 == "10.99.0.2")
check("ensure_overlay_site_gateway: re-join with an already-up interface reloads via wg-quick down+up",
      any(h == "gw1" and "wg-quick down wg0" in c and "wg-quick up wg0" in c for h, c, _ in calls))
check("ensure_overlay_site_gateway: refreshed AllowedIPs now covers a THIRD site that joined later "
      "(gcp-default) — routing knowledge grows on every re-run",
      any(h == "gw1" and inp and "172.31.0.0/20" in inp and "10.0.0.0/24" in inp
          for h, _, inp in calls))
check("ensure_overlay_site_gateway: excludes its OWN site (home) from its own AllowedIPs list",
      not any(h == "gw1" and inp and inp.count("192.168.88.0/24") > 0 for h, _, inp in calls))


# ── ensure_route_via_site_gateway: writes an idempotent persistent route unit on a plain node ──
reset()
overlay.ensure_route_via_site_gateway("mercury.mydemo.lab", "192.168.88.5", ["172.31.0.0/20"])
check("ensure_route_via_site_gateway: writes a route script with the real gateway IP and CIDR",
      any(inp and "ip route replace 172.31.0.0/20 via 192.168.88.5" in inp for _, _, inp in calls))
check("ensure_route_via_site_gateway: installs+enables a systemd oneshot unit for persistence",
      any("systemctl daemon-reload" in c and "enable --now lab-overlay-routes.service" in c
          for _, c, _ in calls))
check("ensure_route_via_site_gateway: the unit's ExecStart points at the real route script",
      any(inp and "ExecStart=/etc/lab-overlay-routes.sh" in inp for _, _, inp in calls))

reset()
overlay.ensure_route_via_site_gateway("mercury.mydemo.lab", "192.168.88.5", [])
check("ensure_route_via_site_gateway: no remote CIDRs at all -> complete no-op (nothing to route yet)",
      len(calls) == 0)


print()
# ── the node registry: who the hub routes to ───────────────────────────────
# Gaps closed here: a node provisioned before another site joined never learned that site's subnet, and a retired
# site's routes could never be taken off the nodes holding them, because the hub knew its sites but not their nodes.
reset()
overlay.register_node("hub", "aws-tmm", "n1.lab", "10.0.0.9")
written = [body for host, cmd, body in calls if body is not None and "overlay_nodes" in cmd]
check("a node is recorded on the hub, one line per node",
      written and written[-1].strip() == "aws-tmm n1.lab 10.0.0.9")

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_nodes": FakeResult(stdout="aws-tmm n1.lab 10.0.0.9\n")}
overlay.register_node("hub", "aws-tmm", "n1.lab", "10.0.0.250")
written = [body for host, cmd, body in calls if body is not None and "overlay_nodes" in cmd]
check("recording the same node again replaces its line rather than adding one",
      written and written[-1].strip() == "aws-tmm n1.lab 10.0.0.250")

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_nodes":
                   FakeResult(stdout="home n0.lab 192.168.88.73\naws-tmm n1.lab 10.0.0.9\n")}
check("every node is listed", overlay.list_nodes("hub")
      == [("home", "n0.lab", "192.168.88.73"), ("aws-tmm", "n1.lab", "10.0.0.9")])
check("one site's nodes can be asked for on their own",
      overlay.list_nodes("hub", site_name="home") == [("home", "n0.lab", "192.168.88.73")])

reset()
overlay.forget_node("hub", "n1.lab")
check("forgetting a node edits the registry by that node's own name",
      any("overlay_nodes" in cmd and "n1\\.lab" in cmd for _h, cmd, _b in calls))

# ── a node learns about a site that joined after it ───────────────────────
reset()
scripted["hub"] = {
    "cat /etc/wireguard/overlay_nodes": FakeResult(stdout="home n0.lab 192.168.88.73\naws-tmm n1.lab 10.0.0.9\n"),
    "cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\naws-tmm 10.0.0.0/16\n"),
}
refreshed, unreachable = overlay.refresh_routes("hub")
check("every registered node is refreshed", (refreshed, unreachable) == (2, 0))
home_script = [body for host, cmd, body in calls
               if host == "n0.lab" and body and "ip route replace" in body]
check("the home node is routed to the other site's subnet, via its own gateway",
      home_script and "ip route replace 10.0.0.0/16 via 192.168.88.73" in home_script[-1])
check("...and not to its own site's subnet",
      home_script and "192.168.88.0/24" not in home_script[-1])
cloud_script = [body for host, cmd, body in calls
                if host == "n1.lab" and body and "ip route replace" in body]
check("the cloud node is routed the other way, via its own gateway",
      cloud_script and "ip route replace 192.168.88.0/24 via 10.0.0.9" in cloud_script[-1])
check("one site's nodes can be refreshed alone",
      overlay.refresh_routes("hub", only_site="home")[0] == 1)

# A node that is not answering is reported, not retried forever: its own next provisioning run routes it anyway.
reset()
scripted["hub"] = {
    "cat /etc/wireguard/overlay_nodes": FakeResult(stdout="home n0.lab 192.168.88.73\n"),
    "cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\naws-tmm 10.0.0.0/16\n"),
}
scripted["n0.lab"] = {"true": FakeResult(returncode=255)}
check("an unreachable node is counted, and nothing is written to it",
      overlay.refresh_routes("hub") == (0, 1)
      and not any(host == "n0.lab" and body for host, _c, body in calls))

# The last other site going away leaves a node with no overlay route at all, not a stale one.
reset()
scripted["hub"] = {
    "cat /etc/wireguard/overlay_nodes": FakeResult(stdout="home n0.lab 192.168.88.73\n"),
    "cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\n"),
}
overlay.refresh_routes("hub")
check("the route script and its unit are removed when no other site is left",
      any(host == "n0.lab" and "rm -f /etc/lab-overlay-routes.sh" in cmd for host, cmd, _b in calls))

# ── retiring a site ───────────────────────────────────────────────────────
reset()
scripted["hub"] = {
    "cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\naws-tmm 10.0.0.0/16\n"),
    "cat /etc/wireguard/overlay_nodes": FakeResult(stdout="home n0.lab 192.168.88.73\naws-tmm n1.lab 10.0.0.9\n"),
}
cidr = overlay.retire_site("hub", "aws-tmm")
hub_cmds = "\n".join(cmd for host, cmd, _b in calls if host == "hub")
check("the site's CIDR is reported back", cidr == "10.0.0.0/16")
check("its gateway stops being a peer of the hub", "# peer: aws-tmm" in hub_cmds and "wg set wg0 peer" in hub_cmds)
check("the hub stops routing its subnet", "ip route del 10.0.0.0/16 dev wg0" in hub_cmds)
for registry in ("overlay_sites", "overlay_allocations", "overlay_nodes"):
    check("its line goes out of {}".format(registry),
          any(registry in cmd and "aws-tmm" in cmd and "sed -i" in cmd for _h, cmd, _b in calls))
check("no VM is created, started or destroyed",
      not any(k in hub_cmds for k in ("virsh", "virt-install", "run-instances", "terminate-instances")))

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\n")}
check("retiring a site the hub does not have changes nothing",
      overlay.retire_site("hub", "nope") == ""
      and not any("ip route del" in cmd for _h, cmd, _b in calls))

# ── the command that exposes all of it ────────────────────────────────────
import subprocess  # noqa: E402

r = subprocess.run([sys.executable, str(_REPO / "scripts" / "overlay_ctl.py"), "--help"],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("overlay_ctl.py explains itself", r.returncode == 0 and "retire" in r.stdout and "--hub" in r.stdout)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "overlay_ctl.py"), "list"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
check("it refuses to guess which hub to talk to", r.returncode != 0)
source = (_REPO / "scripts" / "overlay_ctl.py").read_text()
check("retiring a site asks first, since the hub is shared with every other lab",
      "Retire it? [y/N]" in source and "--yes" in source)
check("the installer deploys it",
      "overlay_ctl.py" in (_REPO / "install_automation_node_scripts.sh").read_text())

# ── a new site's arrival routes the nodes that are already there ──────────
setup_vm_source = (_REPO / "scripts" / "setup_vm.py").read_text()
check("provisioning records the node on the hub",
      "overlay.register_node(hub_host, this_site, vm_name, gateway_lan_ip)" in setup_vm_source)
check("...and refreshes the nodes of every other site, which is what closes the gap",
      "overlay.refresh_routes(hub_host)" in setup_vm_source)


# ── cross-site DNS: the record is written where it has to resolve ─────────
# Every DNS server here is a master of the same zone and a record arrives by being written into its zone file
# (libs/services.py), so a node becomes resolvable at another site by writing it there as well. The hub is where a
# run learns the other sites' servers.
reset()
overlay.register_dns_server("hub", "home", "192.168.88.73")
written = [body for host, cmd, body in calls if body is not None and "overlay_dns" in cmd]
check("a site's DNS server is recorded on the hub", written and written[-1].strip() == "home 192.168.88.73")

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_dns": FakeResult(stdout="home 192.168.88.73\n")}
overlay.register_dns_server("hub", "home", "192.168.88.9")
written = [body for host, cmd, body in calls if body is not None and "overlay_dns" in cmd]
check("registering the same site again replaces its line", written and written[-1].strip() == "home 192.168.88.9")

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_dns":
                   FakeResult(stdout="home 192.168.88.73\naws-tmm 10.0.0.53\n")}
check("every site's server is listed",
      overlay.list_dns_servers("hub") == [("home", "192.168.88.73"), ("aws-tmm", "10.0.0.53")])
check("a site's own server is left out when it asks, since the local write already reached it",
      overlay.list_dns_servers("hub", exclude_site="home") == [("aws-tmm", "10.0.0.53")])

# ── the whole zone, for the drift per-record writes cannot fix ────────────
reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_dns":
                   FakeResult(stdout="home 192.168.88.73\naws-tmm 10.0.0.53\n")}
with tempfile.TemporaryDirectory() as zone_dir:
    Path(zone_dir, "mydemo.lab.lan").write_text("$TTL 300\nn1         IN  A       192.168.88.5\n")
    synced, failed = overlay.sync_zone_to_sites("hub", "mydemo.lab", zone_dir=zone_dir)
check("the zone reaches every registered site", (synced, failed) == (2, 0))
pushed = [(host, body) for host, cmd, body in calls if "cat > " in cmd and body and "IN  A" in body]
check("the file is replaced, not appended to",
      pushed and all("cat > /var/lib/named/mydemo.lab.lan" in cmd
                     for _h, cmd, body in calls if body and "IN  A" in body))
check("it goes to both servers", sorted(host for host, _b in pushed) == ["10.0.0.53", "192.168.88.73"])
check("named is reloaded where it landed",
      any("reload named" in cmd for host, cmd, _b in calls if host == "10.0.0.53"))

reset()
scripted["hub"] = {"cat /etc/wireguard/overlay_dns": FakeResult(stdout="home 192.168.88.73\n")}
scripted["192.168.88.73"] = {"cat > ": FakeResult(returncode=255)}
with tempfile.TemporaryDirectory() as zone_dir:
    Path(zone_dir, "mydemo.lab.lan").write_text("$TTL 300\n")
    check("a site that cannot be written to is counted, not fatal",
          overlay.sync_zone_to_sites("hub", "mydemo.lab", zone_dir=zone_dir) == (0, 1))

reset()
with tempfile.TemporaryDirectory() as zone_dir:
    try:
        overlay.sync_zone_to_sites("hub", "absent.lab", zone_dir=zone_dir)
        check("pushing a zone this node does not have is an error", False)
    except SystemExit:
        pass

# A retired site stops receiving records, which is the same cleanup as its routes.
reset()
scripted["hub"] = {
    "cat /etc/wireguard/overlay_sites": FakeResult(stdout="home 192.168.88.0/24\naws-tmm 10.0.0.0/16\n"),
    "cat /etc/wireguard/overlay_nodes": FakeResult(stdout=""),
}
overlay.retire_site("hub", "aws-tmm")
check("retiring a site also drops its DNS server",
      any("overlay_dns" in cmd and "aws-tmm" in cmd and "sed -i" in cmd for _h, cmd, _b in calls))

setup_vm_source = (_REPO / "scripts" / "setup_vm.py").read_text()
check("provisioning registers this site's DNS server",
      "overlay.register_dns_server(hub_host, this_site, site_dns_server)" in setup_vm_source)
check("...and writes the node's record into the other sites' servers",
      "overlay.list_dns_servers(hub_host, exclude_site=this_site)" in setup_vm_source
      and "remote_dns_servers=other_dns" in setup_vm_source)
check("a libvirt or harvester site is served by this automation node's own BIND",
      'site_dns_server = env.get("mydns", "")' in setup_vm_source)
ctl = (_REPO / "scripts" / "overlay_ctl.py").read_text()
check("the command can push the zone", "dns-sync" in ctl and "sync_zone_to_sites" in ctl)


if failures:
    print("{} check(s) FAILED:".format(len(failures)))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all overlay checks passed")
