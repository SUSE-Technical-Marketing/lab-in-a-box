#!/usr/bin/env python3
# Unit tests for libs/overlay.py — the cross-cloud, SITE-TO-SITE WireGuard
# overlay (hub-and-spoke among SITE GATEWAYS, never individual lab nodes —
# see the module's own docstring + repo TODO's "CROSS-CLOUD WIREGUARD
# OVERLAY" entries for why the shape changed after live-testing). No real
# WireGuard/SSH: ssh_run/ssh_output are monkeypatched directly on the
# module (same convention as 52_ansible_control_node_test.py), and a fake
# VMBackend stands in for backends.ensure_cloud_dns_vm()'s own
# reuse-vs-create test pattern (see 42_cloud_dns_vm_test.py). Run from
# 53_overlay.sh, in its own container — see tests/run_tests.sh.
import ipaddress
import sys
import tempfile
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


# ── add_peer_to_hub / remove_peer_from_hub: multi-CIDR AllowedIPs, idempotent ──
reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=1)}  # marker not yet present
overlay.add_peer_to_hub("hub1", "aws-aws-tmm", "GWPUB", ["10.99.0.5/32", "172.31.0.0/20"])
check("add_peer_to_hub: applies the peer LIVE via wg set with the FULL comma-joined AllowedIPs",
      any("wg set wg0 peer" in c and "GWPUB" in c and "10.99.0.5/32,172.31.0.0/20" in c
          for _, c, _ in calls))
check("add_peer_to_hub: persists a marked [Peer] block with the real site subnet, not just a /32",
      any(inp and "# peer: aws-aws-tmm" in inp and "PublicKey = GWPUB" in inp
          and "AllowedIPs = 10.99.0.5/32,172.31.0.0/20" in inp for _, _, inp in calls))
check("add_peer_to_hub: adds a REAL kernel route for every AllowedIPs CIDR via wg0 — a real bug "
      "found live-testing 2026-09-18: `wg set` alone never installs the kernel route wg-quick's "
      "own parsing normally would, so the hub could decrypt inbound traffic from a new peer but "
      "had nowhere to route a reply back",
      any("ip route replace 10.99.0.5/32 dev wg0" in c for _, c, _ in calls)
      and any("ip route replace 172.31.0.0/20 dev wg0" in c for _, c, _ in calls))

reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=0)}  # marker already present
overlay.add_peer_to_hub("hub1", "aws-aws-tmm", "GWPUB", ["10.99.0.5/32", "172.31.0.0/20"])
check("add_peer_to_hub: does NOT re-append the [Peer] block when the marker already exists",
      not any(inp and "# peer: aws-aws-tmm" in inp for _, _, inp in calls))
check("add_peer_to_hub: still re-asserts the live wg set (idempotent, cheap, always correct)",
      any("wg set wg0 peer" in c for _, c, _ in calls))

reset()
overlay.remove_peer_from_hub("hub1", "aws-aws-tmm")
check("remove_peer_from_hub: looks up the marker on the HUB itself, never contacts the peer",
      all(h == "hub1" for h, _, _ in calls))
check("remove_peer_from_hub: the removal script greps the exact marker and calls wg set ... remove",
      any("# peer: aws-aws-tmm" in c and "wg set wg0 peer" in c and "remove" in c for _, c, _ in calls))


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
with tempfile.TemporaryDirectory() as tmp:
    hub_public_ip2, _, _ = overlay.ensure_overlay_hub(
        create_backend, "aws", "ami-xyz", tmp, "ssh-ed25519 AAAAfake test@example", wg_port=51820)
    check("ensure_overlay_hub: create path writes a real cloud-init user-data file (a real bug found "
          "live-testing 2026-09-18 — create_vm() dies outright without one)",
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
with tempfile.TemporaryDirectory() as tmp:
    gw_ip = overlay.ensure_site_gateway_vm(create_backend2, "aws", "ami-xyz", tmp,
                                            "ssh-ed25519 AAAAfake test@example")
check("ensure_site_gateway_vm: create path returns the new gateway's real IP", gw_ip == "203.0.113.77")
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
if failures:
    print("{} check(s) FAILED:".format(len(failures)))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all overlay checks passed")
