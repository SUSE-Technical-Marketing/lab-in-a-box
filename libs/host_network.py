"""
host_network.py: read a host's current network settings and turn its NIC into a bridge that keeps
the host's address, through whichever network stack manages the NIC.

Stacks: netplan (any renderer), NetworkManager, wicked, systemd-networkd, ifupdown.

The render_*/..._commands functions are pure: they take the current settings and return file
contents or command lists. make_bridge() applies them:
  1. copies the stack's configuration to a backup directory and writes a restore script there;
  2. arms a systemd-run timer that runs the restore script after ROLLBACK_SECONDS;
  3. applies the new configuration;
  4. waits until the bridge holds an address and the gateway answers, then disarms the timer;
     if that never happens it runs the restore script at once and raises BridgeError.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import ipaddress
import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROLLBACK_SECONDS = 180
ROLLBACK_UNIT = "lab-in-a-box-net-rollback"
BACKUP_ROOT = Path("/var/lib/lab-in-a-box/network-backup")
CLOUD_INIT_NET_OFF = Path("/etc/cloud/cloud.cfg.d/99-lab-in-a-box-network.cfg")
STACK_PATHS = {
    "netplan": ["/etc/netplan"],
    "networkmanager": ["/etc/NetworkManager/system-connections"],
    "wicked": ["/etc/sysconfig/network"],
    "systemd-networkd": ["/etc/systemd/network"],
    "ifupdown": ["/etc/network/interfaces", "/etc/network/interfaces.d"],
}


class BridgeError(RuntimeError):
    pass


@dataclass
class HostNet:
    """The host's current IPv4 settings on the NIC that carries its default route."""
    nic: str
    mac: str
    address: str                     # CIDR form, e.g. 192.168.8.20/24
    gateway: str
    dhcp: bool
    dns: List[str] = field(default_factory=list)
    search: List[str] = field(default_factory=list)
    # DHCP client identity to carry over to the bridge, so the DHCP server hands it the same lease:
    # systemd-networkd's IAID (its client ID is IAID + the machine's DUID), NetworkManager's settings.
    dhcp_iaid: str = ""
    nm_client_id: str = ""
    nm_iaid: str = ""

    @property
    def ip(self) -> str:
        return self.address.split("/")[0]

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_interface(self.address).network


# ── reading the current state ─────────────────────────────────────────────────

def _ip_json(*args: str) -> list:
    out = subprocess.run(["ip", "-j"] + list(args), capture_output=True, text=True, check=True).stdout
    return json.loads(out or "[]")


def _service_active(name: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def _dns_for(nic: str) -> Tuple[List[str], List[str]]:
    """DNS servers and search domains: systemd-resolved's per-link values when it runs, else resolv.conf."""
    if shutil.which("resolvectl") and _service_active("systemd-resolved"):
        def _vals(sub: str) -> List[str]:
            out = subprocess.run(["resolvectl", sub, nic], capture_output=True, text=True, check=False).stdout
            return out.split(":", 1)[1].split() if ":" in out else []
        dns, search = _vals("dns"), [d for d in _vals("domain") if d != "~."]
        if dns:
            return dns, search
    dns, search = [], []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver" and not parts[1].startswith("127."):
                dns.append(parts[1])
            elif parts and parts[0] == "search":
                search = parts[1:]
    except OSError:
        pass
    return dns, search


def current_settings(nic: Optional[str] = None) -> HostNet:
    """Settings of `nic`, or of the NIC carrying the default IPv4 route when nic is None."""
    routes = [r for r in _ip_json("-4", "route", "show", "default") if r.get("gateway")]
    if nic is None:
        if not routes:
            raise BridgeError("no default IPv4 route: cannot tell which NIC to bridge")
        nic = routes[0]["dev"]
    gateway = next((r["gateway"] for r in routes if r.get("dev") == nic), "")
    link = _ip_json("link", "show", "dev", nic)[0]
    addrs = [a for a in _ip_json("-4", "addr", "show", "dev", nic)[0].get("addr_info", [])
             if a.get("family") == "inet" and a.get("scope") == "global"]
    if not addrs:
        raise BridgeError("{} has no global IPv4 address".format(nic))
    a = addrs[0]
    dns, search = _dns_for(nic)
    return HostNet(nic=nic, mac=link.get("address", ""), address="{}/{}".format(a["local"], a["prefixlen"]),
                   gateway=gateway, dhcp=bool(a.get("dynamic")), dns=dns, search=search,
                   dhcp_iaid=_networkd_iaid(nic))


def _networkd_iaid(nic: str) -> str:
    """The IAID in systemd-networkd's DHCPv4 client ID ("IAID:0x3ef30695/DUID"), or "". The label is "DHCP4 Client ID" or "DHCPv4 Client ID" depending on the systemd version."""
    if not shutil.which("networkctl"):
        return ""
    out = subprocess.run(["networkctl", "status", nic], capture_output=True, text=True, check=False).stdout
    m = re.search(r"DHCPv?4 Client ID:\s*IAID:(0x[0-9a-fA-F]+)", out)
    return m.group(1) if m else ""


def is_bridge(dev: str) -> bool:
    return Path("/sys/class/net/{}/bridge".format(dev)).is_dir()


def _netplan_defines(nic: str) -> bool:
    if not shutil.which("netplan") or not list(Path("/etc/netplan").glob("*.yaml")):
        return False
    out = subprocess.run(["netplan", "get", "ethernets"], capture_output=True, text=True, check=False).stdout
    return any(line.rstrip() == "{}:".format(nic) for line in out.splitlines())


def dhcp_configured(stack: str, nic: str, nm_conn: str = "") -> bool:
    """
    Whether `stack` configures `nic` by DHCP. The kernel's "dynamic" address flag alone is not
    enough: wicked's DHCP client adds its address without it.
    """
    if stack == "wicked":
        cfg = Path("/etc/sysconfig/network/ifcfg-{}".format(nic))
        text = cfg.read_text() if cfg.is_file() else ""
        return bool(re.search(r"^BOOTPROTO=['\"]?dhcp", text, flags=re.M))
    if stack == "networkmanager" and nm_conn:
        out = subprocess.run(["nmcli", "-g", "ipv4.method", "con", "show", nm_conn],
                             capture_output=True, text=True, check=False).stdout.strip()
        return out == "auto"
    if stack == "netplan":
        out = subprocess.run(["netplan", "get", "ethernets.{}.dhcp4".format(nic)],
                             capture_output=True, text=True, check=False).stdout.strip()
        return out == "true"
    if stack == "ifupdown":
        text = _ifupdown_file(nic).read_text() if _ifupdown_file(nic).is_file() else ""
        return bool(re.search(r"^\s*iface\s+{}\s+inet\s+dhcp".format(re.escape(nic)), text, flags=re.M))
    return False


def detect_stack(nic: str) -> Optional[str]:
    """The stack that manages `nic`, or None when none of the supported ones does."""
    if _netplan_defines(nic):
        return "netplan"
    if _service_active("NetworkManager"):
        return "networkmanager"
    if _service_active("wickedd"):
        return "wicked"
    if _service_active("systemd-networkd"):
        return "systemd-networkd"
    if Path("/etc/network/interfaces").is_file() and shutil.which("ifup"):
        return "ifupdown"
    return None


# ── pure renderers ────────────────────────────────────────────────────────────

def netplan_commands(net: HostNet, bridge: str) -> List[List[str]]:
    """`netplan set` edits for the single merged file make_bridge() leaves in /etc/netplan."""
    hint = ["--origin-hint", "90-lab-in-a-box"]
    cmds = [["netplan", "set"] + hint + ["ethernets.{}.{}=null".format(net.nic, k)]
            for k in ("addresses", "routes", "nameservers", "gateway4", "dhcp6")]
    cmds.append(["netplan", "set"] + hint + ["ethernets.{}.dhcp4=false".format(net.nic)])
    params = "parameters: {stp: false, forward-delay: 0}"
    if net.dhcp:
        br = "{{interfaces: [{}], dhcp4: true, macaddress: \"{}\", {}}}".format(net.nic, net.mac, params)
    else:
        ns = ""
        if net.dns:
            ns = ", nameservers: {{addresses: [{}]{}}}".format(
                ", ".join(net.dns), ", search: [{}]".format(", ".join(net.search)) if net.search else "")
        br = "{{interfaces: [{}], dhcp4: false, addresses: [{}], routes: [{{to: default, via: {}}}]{}, {}}}".format(
            net.nic, net.address, net.gateway, ns, params)
    cmds.append(["netplan", "set"] + hint + ["bridges.{}={}".format(bridge, br)])
    return cmds


def netplan_dhcp_dropin(net: HostNet, bridge: str) -> Dict[str, str]:
    """A systemd-networkd drop-in giving netplan's generated bridge network the NIC's DHCP IAID."""
    if not (net.dhcp and net.dhcp_iaid):
        return {}
    return {"/etc/systemd/network/10-netplan-{}.network.d/50-lab-in-a-box-iaid.conf".format(bridge):
            "[DHCPv4]\nIAID={}\n".format(net.dhcp_iaid)}


def nmcli_commands(net: HostNet, bridge: str, existing_conn: str) -> List[List[str]]:
    """nmcli calls creating `bridge` with the host's address and making `net.nic` its port."""
    add = ["nmcli", "con", "add", "type", "bridge", "con-name", bridge, "ifname", bridge,
           "bridge.stp", "no", "bridge.forward-delay", "0"]
    if net.dhcp:
        add += ["ipv4.method", "auto", "bridge.mac-address", net.mac]
        if net.nm_client_id:
            add += ["ipv4.dhcp-client-id", net.nm_client_id]
        if net.nm_iaid:
            add += ["ipv4.dhcp-iaid", net.nm_iaid]
    else:
        add += ["ipv4.method", "manual", "ipv4.addresses", net.address, "ipv4.gateway", net.gateway]
        if net.dns:
            add += ["ipv4.dns", ",".join(net.dns)]
        if net.search:
            add += ["ipv4.dns-search", ",".join(net.search)]
    port = "{}-port-{}".format(bridge, net.nic)
    cmds = [add, ["nmcli", "con", "add", "type", "bridge-slave", "con-name", port,
                  "ifname", net.nic, "master", bridge]]
    if existing_conn:
        cmds.append(["nmcli", "con", "mod", existing_conn, "connection.autoconnect", "no"])
        cmds.append(["nmcli", "con", "down", existing_conn])
    cmds += [["nmcli", "con", "up", port], ["nmcli", "con", "up", bridge]]
    return cmds


def render_networkd(net: HostNet, bridge: str) -> Dict[str, str]:
    """systemd-networkd files; the 05- prefix sorts them before the files that matched the NIC so far."""
    d = "/etc/systemd/network/05-lab-in-a-box-{}".format(bridge)
    if net.dhcp:
        br_net = "DHCP=ipv4\n" + ("\n[DHCPv4]\nIAID={}\n".format(net.dhcp_iaid) if net.dhcp_iaid else "")
    else:
        br_net = "Address={}\nGateway={}\n".format(net.address, net.gateway)
        br_net += "".join("DNS={}\n".format(s) for s in net.dns)
        if net.search:
            br_net += "Domains={}\n".format(" ".join(net.search))
    return {
        d + ".netdev": "[NetDev]\nName={}\nKind=bridge\nMACAddress={}\n\n[Bridge]\nSTP=no\nForwardDelaySec=0\n".format(
            bridge, net.mac),
        d + "-port.network": "[Match]\nName={}\n\n[Network]\nBridge={}\n".format(net.nic, bridge),
        d + ".network": "[Match]\nName={}\n\n[Network]\n{}".format(bridge, br_net),
    }


def render_wicked(net: HostNet, bridge: str, routes_text: str) -> Dict[str, str]:
    """ifcfg files for the bridge and the NIC, plus the routes file with the NIC's routes moved to the bridge."""
    sc = "/etc/sysconfig/network/"
    br = "STARTMODE='auto'\nBRIDGE='yes'\nBRIDGE_PORTS='{}'\nBRIDGE_STP='off'\nBRIDGE_FORWARDDELAY='0'\n".format(net.nic)
    files = {sc + "ifcfg-" + net.nic: "STARTMODE='auto'\nBOOTPROTO='none'\n"}
    if net.dhcp:
        files[sc + "ifcfg-" + bridge] = br + "BOOTPROTO='dhcp'\nLLADDR='{}'\n".format(net.mac)
        return files
    files[sc + "ifcfg-" + bridge] = br + "BOOTPROTO='static'\nIPADDR='{}'\n".format(net.address)
    kept = []
    for line in routes_text.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[3] == net.nic:
            cols[3] = bridge
            line = " ".join(cols)
        kept.append(line)
    if not any(l.split()[:1] == ["default"] for l in kept if l.strip()):
        kept.append("default {} - {}".format(net.gateway, bridge))
    files[sc + "routes"] = "\n".join(kept) + "\n"
    return files


_IFUPDOWN_STANZA = re.compile(r"^(iface|auto|allow-\S+|mapping|source|source-directory|rename)\b")


def render_ifupdown(interfaces_text: str, net: HostNet, bridge: str) -> str:
    """/etc/network/interfaces with the NIC switched to `inet manual` and a bridge stanza appended."""
    out, skipping = [], False
    for line in interfaces_text.splitlines():
        words = line.split()
        if _IFUPDOWN_STANZA.match(line.strip()):
            skipping = False
            if words[:2] == ["iface", net.nic]:
                skipping = True
                if len(words) >= 3 and words[2] == "inet":
                    out.append("iface {} inet manual".format(net.nic))
                continue
        elif skipping and line.strip():
            continue
        out.append(line)
    stanza = ["", "auto {}".format(bridge)]
    if net.dhcp:
        stanza += ["iface {} inet dhcp".format(bridge), "    bridge_hw {}".format(net.mac)]
    else:
        stanza += ["iface {} inet static".format(bridge), "    address {}".format(net.address),
                   "    gateway {}".format(net.gateway)]
        if net.dns:
            stanza.append("    dns-nameservers {}".format(" ".join(net.dns)))
        if net.search:
            stanza.append("    dns-search {}".format(" ".join(net.search)))
    stanza += ["    bridge_ports {}".format(net.nic), "    bridge_stp off", "    bridge_fd 0"]
    return "\n".join(out).rstrip("\n") + "\n" + "\n".join(stanza) + "\n"


def _ifupdown_file(nic: str) -> Path:
    """The interfaces file holding the NIC's iface stanza (the main file or one under interfaces.d)."""
    for p in [Path("/etc/network/interfaces")] + sorted(Path("/etc/network/interfaces.d").glob("*")):
        try:
            if re.search(r"^\s*iface\s+{}\s".format(re.escape(nic)), p.read_text(), flags=re.M):
                return p
        except OSError:
            continue
    return Path("/etc/network/interfaces")


# ── applying, with rollback ───────────────────────────────────────────────────

def _run(cmd: List[str]) -> None:
    subprocess.run(cmd, check=True)


def _backup(stack: str, backup_dir: Path) -> List[str]:
    """Copy the stack's config into backup_dir; returns shell lines that put it back."""
    restore = []
    for i, src in enumerate(STACK_PATHS[stack]):
        p = Path(src)
        dst = backup_dir / "{}-{}".format(i, p.name)
        if p.is_dir():
            shutil.copytree(p, dst, symlinks=True)
            restore.append("rm -rf {0} && cp -a {1} {0}".format(shlex.quote(src), shlex.quote(str(dst))))
        elif p.exists():
            shutil.copy2(p, dst)
            restore.append("cp -a {} {}".format(shlex.quote(str(dst)), shlex.quote(src)))
    restore.append("rm -f {}".format(shlex.quote(str(CLOUD_INIT_NET_OFF))))
    return restore


_RELOAD = {
    "netplan": "ip link delete {bridge} ; netplan apply",
    "wicked": "ip link delete {bridge} ; wicked ifreload all",
    "systemd-networkd": "ip link delete {bridge} ; networkctl reload ; networkctl reconfigure {nic}",
    "ifupdown": "ifdown --force {bridge} ; ip link delete {bridge} ; ifup {nic} ; systemctl restart networking",
}


def _nm_restore(net: HostNet, bridge: str, existing_conn: str) -> List[str]:
    lines = ["nmcli con delete {} {}-port-{} || true".format(bridge, bridge, net.nic)]
    if existing_conn:
        q = shlex.quote(existing_conn)
        lines += ["nmcli con mod {} connection.autoconnect yes".format(q), "nmcli con up {}".format(q)]
    return lines


def _write_files(files: Dict[str, str]) -> None:
    for path, content in files.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)


def _arm_rollback(script: Path, seconds: int) -> None:
    subprocess.run(["systemctl", "stop", ROLLBACK_UNIT + ".timer"], check=False, capture_output=True)
    _run(["systemd-run", "--unit", ROLLBACK_UNIT, "--on-active={}".format(seconds), "/bin/sh", str(script)])


def _gateway_reachable(bridge: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        routes = [r for r in _ip_json("-4", "route", "show", "default") if r.get("dev") == bridge and r.get("gateway")]
        if routes and subprocess.run(["ping", "-c", "1", "-W", "2", routes[0]["gateway"]],
                                     capture_output=True, check=False).returncode == 0:
            return True
        time.sleep(2)
    return False


def make_bridge(bridge: str, nic: Optional[str] = None,
                rollback_after: int = ROLLBACK_SECONDS) -> Tuple[HostNet, HostNet]:
    """
    Turn `nic` (default: the default-route NIC) into a port of `bridge`, keeping the host's address.
    Returns (settings before, bridge settings after); both are the bridge's when it already exists,
    in which case nothing is changed. A DHCP host can still end up with a new address when its DHCP
    server ignores the carried-over client identity; compare the two to tell.
    """
    if is_bridge(bridge):
        existing = current_settings(bridge)
        return existing, existing
    net = current_settings(nic)
    if is_bridge(net.nic):
        raise BridgeError("{} is already a bridge; set _bridge_name to it instead".format(net.nic))
    stack = detect_stack(net.nic)
    if stack is None:
        raise BridgeError("no supported network stack manages {}".format(net.nic))

    backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True)
    existing_conn = ""
    if stack == "networkmanager":
        out = subprocess.run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", net.nic],
                             capture_output=True, text=True, check=False).stdout.strip()
        existing_conn = "" if out in ("", "--") else out
        if existing_conn:
            ids = subprocess.run(["nmcli", "-g", "ipv4.dhcp-client-id,ipv4.dhcp-iaid", "con", "show", existing_conn],
                                 capture_output=True, text=True, check=False).stdout.splitlines() + ["", ""]
            net.nm_client_id, net.nm_iaid = ids[0].strip(), ids[1].strip()
        net.dhcp = net.dhcp or dhcp_configured(stack, net.nic, existing_conn)
        _backup(stack, backup_dir)
        restore = _nm_restore(net, bridge, existing_conn) + ["rm -f {}".format(shlex.quote(str(CLOUD_INIT_NET_OFF)))]
    else:
        net.dhcp = net.dhcp or dhcp_configured(stack, net.nic)
        restore = _backup(stack, backup_dir) + [_RELOAD[stack].format(nic=net.nic, bridge=bridge)]
        restore += ["rm -rf {}".format(shlex.quote(str(Path(f).parent))) for f in netplan_dhcp_dropin(net, bridge)
                    if stack == "netplan"]
    script = backup_dir / "restore.sh"
    script.write_text("#!/bin/sh\n" + "\n".join(restore) + "\n")
    _arm_rollback(script, rollback_after)

    if CLOUD_INIT_NET_OFF.parent.is_dir():
        CLOUD_INIT_NET_OFF.write_text("network: {config: disabled}\n")
    if stack == "netplan":
        merged = subprocess.run(["netplan", "get"], capture_output=True, text=True, check=True).stdout
        for f in Path("/etc/netplan").glob("*.yaml"):
            f.unlink()
        target = Path("/etc/netplan/90-lab-in-a-box.yaml")
        target.write_text(merged)
        target.chmod(0o600)
        for cmd in netplan_commands(net, bridge):
            _run(cmd)
        _write_files(netplan_dhcp_dropin(net, bridge))
        _run(["netplan", "apply"])
    elif stack == "networkmanager":
        for cmd in nmcli_commands(net, bridge, existing_conn):
            _run(cmd)
    elif stack == "wicked":
        routes = Path("/etc/sysconfig/network/routes")
        _write_files(render_wicked(net, bridge, routes.read_text() if routes.is_file() else ""))
        _run(["wicked", "ifreload", "all"])
    elif stack == "systemd-networkd":
        _write_files(render_networkd(net, bridge))
        _run(["networkctl", "reload"])
        _run(["networkctl", "reconfigure", net.nic])
    else:
        f = _ifupdown_file(net.nic)
        f.write_text(render_ifupdown(f.read_text(), net, bridge))
        subprocess.run(["ifdown", "--force", net.nic], check=False)
        _run(["ifup", bridge])

    if not _gateway_reachable(bridge, timeout=60):
        subprocess.run(["systemctl", "start", ROLLBACK_UNIT + ".service"], check=False)
        raise BridgeError("{} did not reach gateway {} through {}; previous configuration restored from {}".format(
            net.nic, net.gateway, bridge, backup_dir))
    subprocess.run(["systemctl", "stop", ROLLBACK_UNIT + ".timer"], check=False)
    return net, current_settings(bridge)
