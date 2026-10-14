#!/usr/bin/env python3.11
# Unit tests for libs/host_network.py: the per-stack bridge configuration renderers, and
# make_bridge()'s rollback handling with every system call mocked. Run from 69_host_network.sh,
# in its own container — see tests/run_tests.sh.
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import host_network as hn  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


STATIC = hn.HostNet(nic="eth0", mac="52:54:00:12:34:56", address="192.168.8.20/24", gateway="192.168.8.1",
                    dhcp=False, dns=["192.168.8.1", "9.9.9.9"], search=["mydemo.lab"])
DHCP = hn.HostNet(nic="enp1s0", mac="52:54:00:ab:cd:ef", address="10.0.0.5/24", gateway="10.0.0.1", dhcp=True)

check("HostNet derives its network", str(STATIC.network) == "192.168.8.0/24" and STATIC.ip == "192.168.8.20")

# ── systemd-networkd ──────────────────────────────────────────────────────────
files = hn.render_networkd(STATIC, "br0")
netdev = files["/etc/systemd/network/05-lab-in-a-box-br0.netdev"]
port = files["/etc/systemd/network/05-lab-in-a-box-br0-port.network"]
brnet = files["/etc/systemd/network/05-lab-in-a-box-br0.network"]
check("networkd: the bridge netdev keeps the NIC's MAC", "Kind=bridge" in netdev and "MACAddress=52:54:00:12:34:56" in netdev)
check("networkd: the NIC becomes a port of the bridge", "Name=eth0" in port and "Bridge=br0" in port)
check("networkd: the bridge gets the host's static address, gateway and DNS",
      all(s in brnet for s in ("Address=192.168.8.20/24", "Gateway=192.168.8.1", "DNS=192.168.8.1",
                               "DNS=9.9.9.9", "Domains=mydemo.lab")))
check("networkd: a DHCP host's bridge uses DHCP", "DHCP=ipv4" in hn.render_networkd(DHCP, "br0")[
    "/etc/systemd/network/05-lab-in-a-box-br0.network"])

# ── wicked ────────────────────────────────────────────────────────────────────
files = hn.render_wicked(STATIC, "br0", "default 192.168.8.1 - eth0\n10.1.0.0/16 192.168.8.254 - eth0\n")
br = files["/etc/sysconfig/network/ifcfg-br0"]
check("wicked: the bridge is static with the host's address and the NIC as its port",
      all(s in br for s in ("BOOTPROTO='static'", "IPADDR='192.168.8.20/24'", "BRIDGE='yes'", "BRIDGE_PORTS='eth0'")))
check("wicked: the NIC keeps no address", files["/etc/sysconfig/network/ifcfg-eth0"] == "STARTMODE='auto'\nBOOTPROTO='none'\n")
check("wicked: routes bound to the NIC move to the bridge",
      files["/etc/sysconfig/network/routes"] == "default 192.168.8.1 - br0\n10.1.0.0/16 192.168.8.254 - br0\n")
check("wicked: a missing default route is added on the bridge",
      "default 192.168.8.1 - br0" in hn.render_wicked(STATIC, "br0", "")["/etc/sysconfig/network/routes"])
dfiles = hn.render_wicked(DHCP, "br0", "")
check("wicked: a DHCP host's bridge uses DHCP with the NIC's MAC and leaves routes alone",
      "BOOTPROTO='dhcp'" in dfiles["/etc/sysconfig/network/ifcfg-br0"]
      and "LLADDR='52:54:00:ab:cd:ef'" in dfiles["/etc/sysconfig/network/ifcfg-br0"]
      and "/etc/sysconfig/network/routes" not in dfiles)

# ── ifupdown ──────────────────────────────────────────────────────────────────
INTERFACES = """source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

allow-hotplug eth0
iface eth0 inet static
    address 192.168.8.20/24
    gateway 192.168.8.1
iface eth0 inet6 auto
"""
out = hn.render_ifupdown(INTERFACES, STATIC, "br0")
check("ifupdown: the loopback and source lines are kept",
      "auto lo\niface lo inet loopback" in out and out.startswith("source /etc/network/interfaces.d/*"))
check("ifupdown: the NIC switches to inet manual and loses its options and inet6 stanza",
      "iface eth0 inet manual" in out and "iface eth0 inet static" not in out and "inet6" not in out
      and out.count("address 192.168.8.20/24") == 1)
check("ifupdown: a static bridge stanza carries the address, gateway and port",
      all(s in out for s in ("auto br0", "iface br0 inet static", "    address 192.168.8.20/24",
                             "    gateway 192.168.8.1", "    bridge_ports eth0", "    dns-nameservers 192.168.8.1 9.9.9.9")))
out = hn.render_ifupdown("auto enp1s0\niface enp1s0 inet dhcp\n", DHCP, "br0")
check("ifupdown: a DHCP host's bridge uses DHCP with the NIC's MAC",
      "iface br0 inet dhcp" in out and "bridge_hw 52:54:00:ab:cd:ef" in out and "iface enp1s0 inet manual" in out)

# ── netplan ───────────────────────────────────────────────────────────────────
cmds = hn.netplan_commands(STATIC, "br0")
check("netplan: every edit targets the single merged file", all(c[2:4] == ["--origin-hint", "90-lab-in-a-box"] for c in cmds))
check("netplan: the NIC's addresses, routes and DNS are removed and DHCP turned off",
      all(["netplan", "set", "--origin-hint", "90-lab-in-a-box", "ethernets.eth0.{}=null".format(k)] in cmds
          for k in ("addresses", "routes", "nameservers")) and cmds[-2][-1] == "ethernets.eth0.dhcp4=false")
check("netplan: the bridge gets the host's static settings",
      cmds[-1][-1] == "bridges.br0={interfaces: [eth0], dhcp4: false, addresses: [192.168.8.20/24], "
                      "routes: [{to: default, via: 192.168.8.1}], nameservers: {addresses: [192.168.8.1, 9.9.9.9], "
                      "search: [mydemo.lab]}, parameters: {stp: false, forward-delay: 0}}")
check("netplan: a DHCP host's bridge uses DHCP with the NIC's MAC",
      'dhcp4: true, macaddress: "52:54:00:ab:cd:ef"' in hn.netplan_commands(DHCP, "br0")[-1][-1])

# ── NetworkManager ────────────────────────────────────────────────────────────
cmds = hn.nmcli_commands(STATIC, "br0", "Wired connection 1")
add = cmds[0]
check("nmcli: the bridge gets the host's static settings",
      all(x in add for x in ("manual", "192.168.8.20/24", "192.168.8.1", "192.168.8.1,9.9.9.9", "mydemo.lab")))
check("nmcli: the old connection stops autoconnecting before it goes down, so a reboot keeps the bridge",
      cmds.index(["nmcli", "con", "mod", "Wired connection 1", "connection.autoconnect", "no"])
      < cmds.index(["nmcli", "con", "down", "Wired connection 1"]))
check("nmcli: the port and the bridge come up last", cmds[-2:] == [["nmcli", "con", "up", "br0-port-eth0"],
                                                                    ["nmcli", "con", "up", "br0"]])
dadd = hn.nmcli_commands(DHCP, "br0", "")[0]
check("nmcli: a DHCP host's bridge uses DHCP with the NIC's MAC",
      "auto" in dadd and "52:54:00:ab:cd:ef" in dadd)
check("nmcli: with no existing connection nothing is taken down",
      not any(c[2] in ("down", "mod") for c in hn.nmcli_commands(DHCP, "br0", "")))


# ── DHCP client identity carried over to the bridge ──────────────────────────
IAID = hn.HostNet(nic="enp1s0", mac="52:54:00:ab:cd:ef", address="10.0.0.5/24", gateway="10.0.0.1", dhcp=True,
                  dhcp_iaid="0x3ef30695", nm_client_id="mac", nm_iaid="ifname")
check("networkd: a DHCP bridge keeps the NIC's IAID",
      "[DHCPv4]\nIAID=0x3ef30695" in hn.render_networkd(IAID, "br0")["/etc/systemd/network/05-lab-in-a-box-br0.network"])
check("netplan: a drop-in gives netplan's generated bridge network the NIC's IAID",
      hn.netplan_dhcp_dropin(IAID, "br0") == {
          "/etc/systemd/network/10-netplan-br0.network.d/50-lab-in-a-box-iaid.conf": "[DHCPv4]\nIAID=0x3ef30695\n"})
check("netplan: no drop-in for a static host or an unknown IAID",
      hn.netplan_dhcp_dropin(STATIC, "br0") == {} and hn.netplan_dhcp_dropin(DHCP, "br0") == {})
add_cmd = hn.nmcli_commands(IAID, "br0", "")[0]
check("nmcli: a DHCP bridge keeps the old connection's client ID and IAID",
      add_cmd[add_cmd.index("ipv4.dhcp-client-id") + 1] == "mac" and add_cmd[add_cmd.index("ipv4.dhcp-iaid") + 1] == "ifname")
with mock.patch.object(hn.shutil, "which", return_value="/usr/bin/networkctl"), \
     mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
         [], 0, stdout="  DHCP4 Client ID: IAID:0x3ef30695/DUID\n")):
    check("_networkd_iaid reads the IAID from networkctl status", hn._networkd_iaid("enp1s0") == "0x3ef30695")
with mock.patch.object(hn.shutil, "which", return_value="/usr/bin/networkctl"), \
     mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
         [], 0, stdout="            DHCPv4 Client ID: IAID:0x6849eedf/DUID\n")):
    check("_networkd_iaid also reads newer systemd's 'DHCPv4 Client ID' label", hn._networkd_iaid("enp1s0") == "0x6849eedf")

# ── dhcp_configured(): DHCP read from the stack's own configuration ──────────
with tempfile.TemporaryDirectory() as d:
    ifcfg = Path(d) / "ifcfg-eth0"
    ifcfg.write_text("BOOTPROTO=dhcp4\nSTARTMODE=auto\n")
    real_path = hn.Path
    with mock.patch.object(hn, "Path", side_effect=lambda x: ifcfg if x == "/etc/sysconfig/network/ifcfg-eth0" else real_path(x)):
        check("wicked: BOOTPROTO=dhcp4 counts as DHCP", hn.dhcp_configured("wicked", "eth0"))
        ifcfg.write_text("BOOTPROTO='static'\nIPADDR='10.0.0.5/24'\n")
        check("wicked: BOOTPROTO=static does not", not hn.dhcp_configured("wicked", "eth0"))
with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="auto\n")):
    check("NetworkManager: ipv4.method auto counts as DHCP", hn.dhcp_configured("networkmanager", "eth0", "Wired"))
with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="true\n")):
    check("netplan: dhcp4 true counts as DHCP", hn.dhcp_configured("netplan", "eth0"))
with mock.patch.object(hn, "_ifupdown_file", return_value=Path(tempfile.mkdtemp()) / "missing"):
    check("ifupdown: no interfaces file means not DHCP", not hn.dhcp_configured("ifupdown", "eth0"))

# ── make_bridge(): rollback armed first, run on failure, disarmed on success ──
def _make(reachable):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "network").mkdir()
    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="")
    with mock.patch.object(hn, "BACKUP_ROOT", tmp / "backup"), \
         mock.patch.object(hn, "CLOUD_INIT_NET_OFF", tmp / "no-cloud" / "99.cfg"), \
         mock.patch.dict(hn.STACK_PATHS, {"systemd-networkd": [str(tmp / "network")]}), \
         mock.patch.object(hn, "is_bridge", return_value=False), \
         mock.patch.object(hn, "current_settings", return_value=STATIC), \
         mock.patch.object(hn, "detect_stack", return_value="systemd-networkd"), \
         mock.patch.object(hn, "dhcp_configured", return_value=False), \
         mock.patch.object(hn, "_write_files"), \
         mock.patch.object(hn, "_gateway_reachable", return_value=reachable), \
         mock.patch.object(subprocess, "run", side_effect=fake_run):
        try:
            hn.make_bridge("br0")
            raised = False
        except hn.BridgeError:
            raised = True
    restore = next((tmp / "backup").iterdir()) / "restore.sh"
    return calls, raised, restore.read_text()


calls, raised, restore = _make(reachable=False)
arm = next(i for i, c in enumerate(calls) if c[0] == "systemd-run")
reload_ = calls.index(["networkctl", "reload"])
check("make_bridge arms the rollback timer before changing the network", arm < reload_)
check("make_bridge runs the rollback at once and raises when the gateway is unreachable",
      raised and ["systemctl", "start", hn.ROLLBACK_UNIT + ".service"] in calls)
check("the restore script puts the saved config back, deletes the bridge and reloads networkd",
      "cp -a" in restore and "ip link delete br0" in restore and "networkctl reconfigure eth0" in restore)
calls, raised, _ = _make(reachable=True)
check("make_bridge disarms the rollback timer once the gateway answers",
      not raised and ["systemctl", "stop", hn.ROLLBACK_UNIT + ".timer"] == calls[-1])

with mock.patch.object(hn, "is_bridge", side_effect=lambda dev: dev == "br0"), \
     mock.patch.object(hn, "current_settings", return_value=STATIC) as cs, \
     mock.patch.object(hn, "detect_stack") as ds:
    hn.make_bridge("br0")
check("make_bridge leaves an existing bridge alone", cs.call_args[0] == ("br0",) and ds.call_count == 0)


# ── reading the host's state: DNS, current settings, the managing stack ───────────────────────────────────────────

root = Path(tempfile.mkdtemp())
real_path = hn.Path


def rooted(p, *a):
    """hn.Path with /etc redirected into the temporary root."""
    p = str(p)
    return real_path(str(root) + p) if p.startswith("/etc") else real_path(p, *a)


def done(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


(root / "etc").mkdir()
(root / "etc" / "resolv.conf").write_text("nameserver 127.0.0.53\nnameserver 10.0.0.53\nsearch lab corp\n")
with mock.patch.object(hn, "Path", side_effect=rooted), mock.patch.object(hn.shutil, "which", return_value=None):
    check("_dns_for: resolv.conf's servers (loopback stubs left out) and search domains",
          hn._dns_for("eth0") == (["10.0.0.53"], ["lab", "corp"]))
resolved = {"dns": "Link 2 (eth0): 10.0.0.1 10.0.0.2\n", "domain": "Link 2 (eth0): lab ~.\n"}
with mock.patch.object(hn.shutil, "which", return_value="/usr/bin/resolvectl"), \
        mock.patch.object(hn, "_service_active", return_value=True), \
        mock.patch.object(hn.subprocess, "run", side_effect=lambda cmd, **k: done(resolved[cmd[1]])):
    check("_dns_for: systemd-resolved's per-link servers and domains, without the ~. route-only domain",
          hn._dns_for("eth0") == (["10.0.0.1", "10.0.0.2"], ["lab"]))

IP = {("-4", "route", "show", "default"): [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0"}],
      ("link", "show", "dev", "eth0"): [{"ifname": "eth0", "address": "52:54:00:00:00:01"}],
      ("-4", "addr", "show", "dev", "eth0"): [{"addr_info": [
          {"family": "inet", "scope": "host", "local": "127.0.0.1", "prefixlen": 8},
          {"family": "inet", "scope": "global", "local": "10.0.0.5", "prefixlen": 24, "dynamic": True}]}]}
with mock.patch.object(hn, "_ip_json", side_effect=lambda *a: IP[a]), \
        mock.patch.object(hn, "_dns_for", return_value=(["10.0.0.53"], ["lab"])), \
        mock.patch.object(hn, "_networkd_iaid", return_value="0x1"):
    net = hn.current_settings()
check("current_settings: the default route's NIC with its MAC, global address, gateway, DHCP flag and DNS",
      (net.nic, net.mac, net.address, net.gateway, net.dhcp, net.dns, net.search, net.dhcp_iaid)
      == ("eth0", "52:54:00:00:00:01", "10.0.0.5/24", "10.0.0.1", True, ["10.0.0.53"], ["lab"], "0x1"))
with mock.patch.object(hn, "_ip_json", return_value=[]):
    try:
        hn.current_settings()
        check("current_settings: fails without a default route", False)
    except hn.BridgeError:
        pass

(root / "etc" / "netplan").mkdir()
(root / "etc" / "netplan" / "50-cloud-init.yaml").write_text("network: {}\n")
with mock.patch.object(hn, "Path", side_effect=rooted), mock.patch.object(hn.shutil, "which", return_value="/usr/sbin/netplan"), \
        mock.patch.object(hn.subprocess, "run", return_value=done("eth0:\n  dhcp4: true\nens5:\n")):
    check("_netplan_defines: true when netplan configures the NIC", hn._netplan_defines("eth0") and hn._netplan_defines("ens5"))
    check("_netplan_defines: false for another NIC", not hn._netplan_defines("eth1"))
for active, expected in ((["NetworkManager"], "networkmanager"), (["wickedd"], "wicked"),
                         (["systemd-networkd"], "systemd-networkd"), ([], None)):
    with mock.patch.object(hn, "_netplan_defines", return_value=False), mock.patch.object(hn, "Path", side_effect=rooted), \
            mock.patch.object(hn, "_service_active", side_effect=lambda name: name in active):
        check("detect_stack: {} when only {} runs".format(expected, active or "nothing"), hn.detect_stack("eth0") == expected)
with mock.patch.object(hn, "_netplan_defines", return_value=True), mock.patch.object(hn, "_service_active", return_value=True):
    check("detect_stack: netplan wins over the renderer it drives", hn.detect_stack("eth0") == "netplan")

(root / "etc" / "network" / "interfaces.d").mkdir(parents=True)
(root / "etc" / "network" / "interfaces").write_text("source /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\n")
(root / "etc" / "network" / "interfaces.d" / "eth0").write_text("auto eth0\niface eth0 inet dhcp\n")
with mock.patch.object(hn, "Path", side_effect=rooted):
    check("_ifupdown_file: the file holding the NIC's iface stanza", hn._ifupdown_file("eth0") == rooted("/etc/network/interfaces.d/eth0"))
    check("_ifupdown_file: the main file when no file defines the NIC", hn._ifupdown_file("eth9") == rooted("/etc/network/interfaces"))
    with mock.patch.object(hn, "_netplan_defines", return_value=False), mock.patch.object(hn, "_service_active", return_value=False), \
            mock.patch.object(hn.shutil, "which", return_value="/sbin/ifup"):
        check("detect_stack: ifupdown when /etc/network/interfaces and ifup exist", hn.detect_stack("eth0") == "ifupdown")

check("_nm_restore: removes the bridge and its port, brings the old connection back",
      hn._nm_restore(STATIC, "br0", "Wired 1") == ["nmcli con delete br0 br0-port-eth0 || true",
                                                    "nmcli con mod 'Wired 1' connection.autoconnect yes", "nmcli con up 'Wired 1'"])
check("_nm_restore: only removes the bridge without an old connection", hn._nm_restore(STATIC, "br0", "") ==
      ["nmcli con delete br0 br0-port-eth0 || true"])
out = root / "w"
hn._write_files({str(out / "a" / "b.conf"): "x=1\n", str(out / "c.conf"): "y\n"})
check("_write_files: writes every file, creating its directory",
      (out / "a" / "b.conf").read_text() == "x=1\n" and (out / "c.conf").read_text() == "y\n")

pings = [1, 0]
with mock.patch.object(hn, "_ip_json", return_value=[{"dev": "br0", "gateway": "10.0.0.1"}]), \
        mock.patch.object(hn.subprocess, "run", side_effect=lambda cmd, **k: done(rc=pings.pop(0))) as ping, \
        mock.patch.object(hn.time, "sleep"):
    check("_gateway_reachable: true once the bridge's gateway answers a ping",
          hn._gateway_reachable("br0", 60) and ping.call_args[0][0] == ["ping", "-c", "1", "-W", "2", "10.0.0.1"])
clock = iter([0, 0, 10, 20])
with mock.patch.object(hn, "_ip_json", return_value=[]), mock.patch.object(hn.time, "monotonic", side_effect=lambda: next(clock)), \
        mock.patch.object(hn.time, "sleep"):
    check("_gateway_reachable: false when the bridge has no default route before the timeout", not hn._gateway_reachable("br0", 15))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all host_network checks passed")
