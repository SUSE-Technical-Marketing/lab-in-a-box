#!/usr/bin/env python3
# Unit tests for libs/services.py's PXEService "ipxe-uefi" mode: the two-stage UEFI netboot
# support for Harvester's PXE install path (see scripts/setup_harvester_cluster.py). No podman/root
# needed: _dnsmasq_conf() is a pure function, and configure()'s only I/O
# (ipxe.efi fetch, file writes) is mocked/redirected to a tempdir.
# Run from 29_pxe_ipxe_uefi.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import services  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


nodes = {
    "harvester1.mydemo.lab": {"mymac": "52:54:00:AB:CD:E1", "pxe_ipxe_url": "http://10.0.0.1/ipxe-harvester1"},
    "harvester2.mydemo.lab": {"mymac": "52:54:00:ab:cd:e2", "pxe_ipxe_url": "http://10.0.0.1/ipxe-harvester2"},
    "no-mac.mydemo.lab": {"pxe_ipxe_url": "http://10.0.0.1/ipxe-no-mac"},
    "no-url.mydemo.lab": {"mymac": "52:54:00:ab:cd:e3"},
}

# ── _dnsmasq_conf(): pxelinux mode is unchanged (regression guard) ──────────
conf = services._dnsmasq_conf({}, "/tftpboot")
check("pxelinux (default) mode still emits a plain dhcp-boot line",
      "dhcp-boot=lpxelinux.0" in conf)
check("pxelinux mode never emits ipxe-only lines",
      "dhcp-userclass" not in conf and "ipxe.efi" not in conf)

# ── _dnsmasq_conf(): ipxe-uefi mode, no nodes ────────────────────────────────
conf = services._dnsmasq_conf({"pxe_mode": "ipxe-uefi"}, "/tftpboot")
check("ipxe-uefi mode emits the iPXE user-class tag",
      "dhcp-userclass=set:ipxe,iPXE" in conf)
check("ipxe-uefi mode's stage-1 handout is ipxe.efi, gated on NOT being iPXE yet",
      "dhcp-boot=tag:!ipxe,ipxe.efi" in conf)
check("ipxe-uefi mode with no nodes emits no per-host lines",
      "dhcp-host=" not in conf)

# ── _dnsmasq_conf(): "proxy" DHCP mode needs a real network address, NOT an
#    interface name. "dhcp-range=br0,proxy" makes dnsmasq refuse to start outright
#    ("bad dhcp-range at line N"), which takes the whole PXE service down.
try:
    services._dnsmasq_conf({"pxe_dhcp_mode": "proxy"}, "/tftpboot")
    check("proxy mode without pxe_dhcp_proxy_subnet dies clearly", False)
except SystemExit:
    check("proxy mode without pxe_dhcp_proxy_subnet dies clearly", True)

conf = services._dnsmasq_conf({"pxe_dhcp_mode": "proxy", "pxe_dhcp_proxy_subnet": "192.168.88.0"}, "/tftpboot")
check("proxy mode with pxe_dhcp_proxy_subnet emits a real network address, not the bridge name",
      "dhcp-range=192.168.88.0,proxy" in conf)
check("proxy mode never emits the old broken bridge-name form",
      "dhcp-range=br0,proxy" not in conf)

# ── _dnsmasq_conf(): proxy mode needs pxe-service, NOT dhcp-boot.
#    A proxyDHCP reply carries its boot info only in pxe-service's vendor-encapsulated options,
#    never in dhcp-boot's plain next-server/filename fields. Using dhcp-boot is a silent no-op:
#    dnsmasq starts, recognizes the client's PXE vendor class and sends no reply.
conf = services._dnsmasq_conf(
    {"pxe_mode": "pxelinux", "pxe_dhcp_mode": "proxy", "pxe_dhcp_proxy_subnet": "192.168.88.0"}, "/tftpboot")
check("pxelinux+proxy emits pxe-service (x86PC), not dhcp-boot",
      'pxe-service=x86PC,"network boot",lpxelinux' in conf and "dhcp-boot=lpxelinux.0" not in conf)

conf = services._dnsmasq_conf(
    {"pxe_mode": "ipxe-uefi", "pxe_dhcp_mode": "proxy", "pxe_dhcp_proxy_subnet": "192.168.88.0"},
    "/tftpboot", nodes=nodes)
check("ipxe-uefi+proxy's stage-1 handout uses pxe-service (x86-64_EFI), not dhcp-boot",
      'pxe-service=tag:!ipxe,x86-64_EFI,"PXE chainload to iPXE",ipxe.efi' in conf
      and "dhcp-boot=tag:!ipxe,ipxe.efi" not in conf)
check("ipxe-uefi+proxy's per-node stage-2 redirect uses pxe-service too",
      'pxe-service=tag:n525400abcde1,tag:ipxe,x86-64_EFI,"harvester1.mydemo.lab",'
      "http://10.0.0.1/ipxe-harvester1" in conf)
check("ipxe-uefi+proxy still sets the dhcp-host tag lines (unrelated to the dhcp-boot/pxe-service choice)",
      "dhcp-host=52:54:00:ab:cd:e1,set:n525400abcde1" in conf)

# ── _dnsmasq_conf(): "off"/"full" modes still use plain dhcp-boot (dnsmasq
#    generates the whole DHCP reply itself there — no proxyDHCP involved,
#    so dhcp-boot's fields are sent normally) ─────────────────────────────
conf = services._dnsmasq_conf({"pxe_mode": "ipxe-uefi"}, "/tftpboot", nodes=nodes)  # dhcp_mode defaults to "off"
check("ipxe-uefi+off still uses plain dhcp-boot (regression guard)",
      "dhcp-boot=tag:!ipxe,ipxe.efi" in conf and "pxe-service" not in conf)

# ── _dnsmasq_conf(): "full" DHCP mode (regression guard) ────────────────────
conf = services._dnsmasq_conf(
    {"pxe_dhcp_mode": "full", "pxe_dhcp_range_start": "192.168.88.200", "pxe_dhcp_range_end": "192.168.88.210"},
    "/tftpboot")
check("full mode emits the configured start/end range",
      "dhcp-range=192.168.88.200,192.168.88.210,12h" in conf)

# ── _dnsmasq_conf(): ipxe-uefi mode with nodes ───────────────────────────────
conf = services._dnsmasq_conf({"pxe_mode": "ipxe-uefi"}, "/tftpboot", nodes=nodes)
check("ipxe-uefi mode: MAC is lowercased in the dhcp-host line",
      "dhcp-host=52:54:00:ab:cd:e1,set:n525400abcde1" in conf)
check("ipxe-uefi mode: stage-2 dhcp-boot line points at this node's own URL, gated on both tags",
      "dhcp-boot=tag:n525400abcde1,tag:ipxe,http://10.0.0.1/ipxe-harvester1" in conf)
check("ipxe-uefi mode: a second node gets its own distinct tag/URL",
      "dhcp-boot=tag:n525400abcde2,tag:ipxe,http://10.0.0.1/ipxe-harvester2" in conf)
check("ipxe-uefi mode: a node missing mymac is silently skipped",
      "no-mac" not in conf)
check("ipxe-uefi mode: a node missing pxe_ipxe_url is silently skipped",
      "no-url" not in conf and "cd-e3" not in conf.lower())

# ── _dnsmasq_conf(): invalid pxe_mode dies clearly ───────────────────────────
try:
    services._dnsmasq_conf({"pxe_mode": "not-a-mode"}, "/tftpboot")
    check("an invalid pxe_mode raises SystemExit via die()", False)
except SystemExit:
    check("an invalid pxe_mode raises SystemExit via die()", True)

# ── PXEService.configure(): fetches ipxe.efi only in ipxe-uefi mode ─────────
fetched_urls = []


def _fake_urlretrieve(url, dest):
    fetched_urls.append(url)
    Path(dest).write_bytes(b"fake-ipxe-efi")


services.urllib.request.urlretrieve = _fake_urlretrieve

with tempfile.TemporaryDirectory() as tmp:
    svc = services.PXEService(lab_setup_path=tmp)
    services._PXE_QUADLET_PATH = Path(tmp) / "lab-pxe.container"
    definition = {
        "nodes": {"harvester1.mydemo.lab": {"mymac": "52:54:00:ab:cd:e1",
                                             "pxe_ipxe_url": "http://10.0.0.1/ipxe-harvester1"}},
        "pxe": {"pxe_mode": "ipxe-uefi", "pxe_bridge": "br0"},
    }
    svc.configure(definition, {})
    check("configure() in ipxe-uefi mode fetches ipxe.efi exactly once",
          fetched_urls == [services._IPXE_EFI_URL])
    check("configure() in ipxe-uefi mode writes ipxe.efi into the TFTP root",
          (Path(svc.tftp_root) / "ipxe.efi").exists())
    check("configure() in ipxe-uefi mode writes no pxelinux.cfg dir (that mode's own file layout)",
          not (Path(svc.tftp_root) / "pxelinux.cfg").exists())

    # A second configure() call must not re-fetch (idempotent).
    fetched_urls.clear()
    svc.configure(definition, {})
    check("configure() does not re-fetch ipxe.efi if already present",
          fetched_urls == [])

with tempfile.TemporaryDirectory() as tmp:
    svc = services.PXEService(lab_setup_path=tmp)
    services._PXE_QUADLET_PATH = Path(tmp) / "lab-pxe.container"
    fetched_urls.clear()
    definition = {
        "nodes": {"n1.mydemo.lab": {"mymac": "52:54:00:ab:cd:e1", "pxe_kernel": "/vmlinuz"}},
        "pxe": {},  # default pxe_mode == "pxelinux"
    }
    svc.configure(definition, {})
    check("configure() in (default) pxelinux mode never fetches ipxe.efi",
          fetched_urls == [])
    check("configure() in pxelinux mode still writes a pxelinux.cfg entry",
          (Path(svc.tftp_root) / "pxelinux.cfg" / "01-52-54-00-ab-cd-e1").exists())

# ── _pxe_quadlet_unit(): NET_ADMIN/NET_RAW granted (dnsmasq refuses to start without
#    them: "process is missing required capability NET_ADMIN") ─────────────────────────────────────────
unit = services._pxe_quadlet_unit("/tftpboot", "/dnsmasq.conf")
check("PXE Quadlet unit grants NET_ADMIN", "AddCapability=NET_ADMIN" in unit)
check("PXE Quadlet unit grants NET_RAW", "AddCapability=NET_RAW" in unit)
check("PXE Quadlet unit still uses host networking", "Network=host" in unit)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all PXE ipxe-uefi checks passed")
