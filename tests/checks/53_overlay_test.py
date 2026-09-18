#!/usr/bin/env python3
# Unit tests for libs/overlay.py — the cross-cloud WireGuard overlay
# (hub-and-spoke, see the module's own docstring + repo TODO's
# "cross-cloud WireGuard overlay plan"). No real WireGuard/SSH: ssh_run/
# ssh_output are monkeypatched directly on the module (same convention as
# 52_ansible_control_node_test.py), and a fake VMBackend stands in for
# backends.ensure_cloud_dns_vm()'s own reuse-vs-create test pattern (see
# 42_cloud_dns_vm_test.py). Run from 53_overlay.sh, in its own container —
# see tests/run_tests.sh.
import ipaddress
import sys
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


# ── _render_hub_config / _render_spoke_config: real content, correct prefix ─
hub_conf = overlay._render_hub_config("HUBPRIV", 51820, "10.99.0.1", "10.99.0.0/16")
check("_render_hub_config: embeds the private key", "PrivateKey = HUBPRIV" in hub_conf)
check("_render_hub_config: Address carries the overlay CIDR's own prefix length (/16)",
      "Address = 10.99.0.1/16" in hub_conf)
check("_render_hub_config: ListenPort is set", "ListenPort = 51820" in hub_conf)
check("_render_hub_config: has no [Peer] block — peers are added live via wg set, not baked in",
      "[Peer]" not in hub_conf)

spoke_conf = overlay._render_spoke_config("SPOKEPRIV", "10.99.0.2", "10.99.0.0/16",
                                           "HUBPUB", "203.0.113.50", 51820)
check("_render_spoke_config: embeds the private key", "PrivateKey = SPOKEPRIV" in spoke_conf)
check("_render_spoke_config: Address carries the FULL overlay prefix (not /32) — the wg-quick "
      "trick that auto-routes the whole overlay CIDR via wg0",
      "Address = 10.99.0.2/16" in spoke_conf)
check("_render_spoke_config: has exactly ONE [Peer] — the hub only (hub-and-spoke)",
      spoke_conf.count("[Peer]") == 1)
check("_render_spoke_config: AllowedIPs is the WHOLE overlay CIDR (reaches every other spoke "
      "through the hub), not just the hub's own /32",
      "AllowedIPs = 10.99.0.0/16" in spoke_conf)
check("_render_spoke_config: PersistentKeepalive is set (needed behind NAT)",
      "PersistentKeepalive = 25" in spoke_conf)
check("_render_spoke_config: Endpoint points at the real hub IP:port",
      "Endpoint = 203.0.113.50:51820" in spoke_conf)


# ── generate_wg_keypair: idempotent — reuse vs generate ─────────────────────
reset()
scripted["hostA"] = {
    "test -f": FakeResult(returncode=1),  # no existing key
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
    "test -f": FakeResult(returncode=0),  # key already exists
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="EXISTINGPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="EXISTINGPUB\n"),
}
priv2, pub2 = overlay.generate_wg_keypair("hostA")
check("generate_wg_keypair: reuses an existing key instead of regenerating",
      priv2 == "EXISTINGPRIV" and not any("wg genkey" in c for _, c, _ in calls))


# ── _allocate_overlay_ip: sequential, reserves the hub address, idempotent per node ──
reset()
scripted["hub1"] = {"cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="")}
ip1 = overlay._allocate_overlay_ip("hub1", "nodeA", "10.99.0.0/24")
check("_allocate_overlay_ip: first allocation skips the hub's own reserved address (.1)",
      ip1 != "10.99.0.1")
check("_allocate_overlay_ip: allocates a real address inside the given CIDR",
      ipaddress.ip_address(ip1) in ipaddress.ip_network("10.99.0.0/24"))
check("_allocate_overlay_ip: persists the allocation by appending to the hub's own file",
      any("overlay_allocations" in c and "nodeA" in c and ">>" in c for _, c, _ in calls))

reset()
scripted["hub1"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="nodeA 10.99.0.2\n"),
}
ip_again = overlay._allocate_overlay_ip("hub1", "nodeA", "10.99.0.0/24")
check("_allocate_overlay_ip: a node already recorded gets its EXISTING ip back, not a new one",
      ip_again == "10.99.0.2" and not any(">>" in c for _, c, _ in calls))

reset()
scripted["hub1"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="nodeA 10.99.0.2\n"),
}
ip_b = overlay._allocate_overlay_ip("hub1", "nodeB", "10.99.0.0/24")
check("_allocate_overlay_ip: a second, different node gets a DIFFERENT address than the first",
      ip_b != "10.99.0.2" and ip_b != "10.99.0.1")


# ── add_peer_to_hub / remove_peer_from_hub: idempotent marker-based persistence ────
reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=1)}  # marker not yet present
overlay.add_peer_to_hub("hub1", "charon.mydemo.lab", "CHARONPUB", "10.99.0.5")
check("add_peer_to_hub: applies the peer LIVE via wg set",
      any("wg set wg0 peer" in c and "CHARONPUB" in c and "10.99.0.5/32" in c for _, c, _ in calls))
check("add_peer_to_hub: persists a marked [Peer] block for reboot survival",
      any(inp and "# peer: charon.mydemo.lab" in inp and "PublicKey = CHARONPUB" in inp
          for _, _, inp in calls))

reset()
scripted["hub1"] = {"grep -qxF": FakeResult(returncode=0)}  # marker already present
overlay.add_peer_to_hub("hub1", "charon.mydemo.lab", "CHARONPUB", "10.99.0.5")
check("add_peer_to_hub: does NOT re-append the [Peer] block when the marker already exists",
      not any(inp and "# peer: charon.mydemo.lab" in inp for _, _, inp in calls))
check("add_peer_to_hub: still re-asserts the live wg set (idempotent, cheap, always correct)",
      any("wg set wg0 peer" in c for _, c, _ in calls))

reset()
overlay.remove_peer_from_hub("hub1", "charon.mydemo.lab")
check("remove_peer_from_hub: looks up the marker on the HUB itself, never contacts the node",
      all(h == "hub1" for h, _, _ in calls))
check("remove_peer_from_hub: the removal script greps the exact marker and calls wg set ... remove",
      any("# peer: charon.mydemo.lab" in c and "wg set wg0 peer" in c and "remove" in c
          for _, c, _ in calls))


# ── ensure_overlay_hub_ready: first-time init vs already-configured-and-up vs down ──
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
check("ensure_overlay_hub_ready: writes a wg0.conf with NO [Peer] block yet (peers join later)",
      any(inp and inp.startswith("[Interface]") and "[Peer]" not in inp for _, _, inp in calls))

reset()
scripted["hub2"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=0),
    "wg show wg0": FakeResult(returncode=0),  # already up
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="HUBPUBKEY\n"),
}
overlay.ensure_overlay_hub_ready("hub2", wg_port=51820, overlay_cidr="10.99.0.0/16")
check("ensure_overlay_hub_ready: already configured+up — does NOT re-run wg genkey or rewrite the conf",
      not any("wg genkey" in c for _, c, _ in calls)
      and not any(inp and inp.startswith("[Interface]") for _, _, inp in calls))

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


# ── ensure_overlay_hub: reuse vs create, mirroring ensure_cloud_dns_vm()'s own test pattern ──
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
        raise AssertionError("create_vm() must not be called when the hub VM already exists")


reset()
scripted["203.0.113.9"] = {
    "test -f /etc/wireguard/wg0.conf": FakeResult(returncode=0),
    "wg show wg0": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="REUSEDPUB\n"),
}
reuse_backend = _FakeBackendReuse()
hub_public_ip, hub_overlay_ip, hub_pub = overlay.ensure_overlay_hub(
    reuse_backend, "aws", "ami-xyz", "/tmp/lab-setup")
check("ensure_overlay_hub: reuse path checks vm_exists() with the fixed 'lab-overlay-hub-<backend>' name",
      ("vm_exists", "lab-overlay-hub-aws") in reuse_backend.calls)
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
hub_public_ip2, _, _ = overlay.ensure_overlay_hub(
    create_backend, "aws", "ami-xyz", "/tmp/lab-setup", wg_port=51820)
check("ensure_overlay_hub: create path uses the per-account hub name ('lab-overlay-hub-aws-prod')",
      ("vm_exists", "lab-overlay-hub-aws-prod") in create_backend.calls)
check("ensure_overlay_hub: create path returns the newly created hub's real IP",
      hub_public_ip2 == "203.0.113.77")
create_call = next(c for c in create_backend.calls if c[0] == "create_vm")
check("ensure_overlay_hub: create path opens the WireGuard UDP port via the existing open_ports kwarg",
      create_call[-1].get("open_ports") == ["51820/udp"])


# ── ensure_overlay_spoke: full join flow ────────────────────────────────────
reset()
scripted["charon.mydemo.lab"] = {
    "test -f": FakeResult(returncode=1),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="SPOKEPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="SPOKEPUB\n"),
    "wg show wg0": FakeResult(returncode=1),  # not up yet
}
scripted["hub3"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout=""),
    "grep -qxF": FakeResult(returncode=1),
}
node_ip = overlay.ensure_overlay_spoke("charon.mydemo.lab", "charon.mydemo.lab", "hub3",
                                        "HUBPUBKEY", 51820, overlay_cidr="10.99.0.0/16")
check("ensure_overlay_spoke: returns a real allocated overlay IP",
      ipaddress.ip_address(node_ip) in ipaddress.ip_network("10.99.0.0/16"))
check("ensure_overlay_spoke: writes a spoke wg0.conf with the hub's own real pubkey/endpoint",
      any(inp and "PublicKey = HUBPUBKEY" in inp and "Endpoint = hub3:51820" in inp
          for h, _, inp in calls if h == "charon.mydemo.lab"))
check("ensure_overlay_spoke: brings the interface up via systemctl (not yet up)",
      any(h == "charon.mydemo.lab" and "systemctl enable --now wg-quick@wg0" in c
          for h, c, _ in calls))
check("ensure_overlay_spoke: registers itself as a peer on the hub",
      any(h == "hub3" and "wg set wg0 peer" in c for h, c, _ in calls))

# Re-join (already up): must reload via down+up, not systemctl enable
reset()
scripted["charon.mydemo.lab"] = {
    "test -f": FakeResult(returncode=0),
    "cat /etc/wireguard/wg0.key": FakeResult(returncode=0, stdout="SPOKEPRIV\n"),
    "cat /etc/wireguard/wg0.pub": FakeResult(returncode=0, stdout="SPOKEPUB\n"),
    "wg show wg0": FakeResult(returncode=0),  # already up
}
scripted["hub3"] = {
    "cat /etc/wireguard/overlay_allocations": FakeResult(returncode=0, stdout="charon.mydemo.lab 10.99.0.2\n"),
    "grep -qxF": FakeResult(returncode=0),
}
node_ip2 = overlay.ensure_overlay_spoke("charon.mydemo.lab", "charon.mydemo.lab", "hub3",
                                         "HUBPUBKEY", 51820, overlay_cidr="10.99.0.0/16")
check("ensure_overlay_spoke: re-join reuses its previously allocated overlay IP",
      node_ip2 == "10.99.0.2")
check("ensure_overlay_spoke: re-join with an already-up interface reloads via wg-quick down+up",
      any(h == "charon.mydemo.lab" and "wg-quick down wg0" in c and "wg-quick up wg0" in c
          for h, c, _ in calls))


# ── remove_overlay_spoke: thin wrapper delegating to remove_peer_from_hub ──────
reset()
overlay.remove_overlay_spoke("hub3", "charon.mydemo.lab")
check("remove_overlay_spoke: removes the node from the hub only, never contacts the node itself",
      all(h == "hub3" for h, _, _ in calls))
check("remove_overlay_spoke: targets the right marker",
      any("# peer: charon.mydemo.lab" in c for _, c, _ in calls))


print()
if failures:
    print("{} check(s) FAILED:".format(len(failures)))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all overlay checks passed")
