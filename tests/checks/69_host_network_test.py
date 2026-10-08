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

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all host_network checks passed")
