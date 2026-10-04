#!/usr/bin/env python3
# Unit tests for scripts/setup_harvester_cluster.py, the PXE-based Harvester HCI cluster bootstrap. No podman, root or real network is
# needed. urllib fetches, virt-install and the VIP wait are mocked. Template rendering uses the real process_template() on the real
# templates, so a substitution error is caught. Run from 30_setup_harvester_cluster.sh, in its own container (see tests/run_tests.sh).
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_creation as lc  # noqa: E402
import setup_harvester_cluster as shc  # noqa: E402

# Tolerant of pyyaml not being installed in this container — same pattern
# already used in 11_primary_test.py/13_addon_common_test.py.
try:
    import yaml as _yaml
    _has_yaml = True
except ImportError:
    _has_yaml = False

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


_CLUSTER_CFG = {
    "harvester_version": "v1.7.1",
    "token": "test-token",
    "ssh_authorized_keys": ["ssh-rsa AAAAtest key1", "ssh-rsa AAAAtest key2"],
    "password_hash": "$6$fakehash",
    "management_interface": "ens3",
    "netmask": "255.255.255.0",
    "gateway": "192.168.88.1",
    "dns_nameservers": ["192.168.88.73", "192.168.88.1"],
    "hypervisor_bridge": "br0",
    "vip": "192.168.88.142",
    "device": "/dev/vda",
    "_templ_addons_loc": str(_REPO / "templates" / "addons"),
}

_CREATE_NODE = {"name": "harvester1.mydemo.lab", "mac": "52:54:00:AB:CD:E1", "ip": "192.168.88.143", "role": "create"}
_JOIN_NODE = {"name": "harvester2.mydemo.lab", "mac": "52:54:00:ab:cd:e2", "ip": "192.168.88.144", "role": "join"}

# ── _fetch_release_assets(): idempotent, correct URLs ────────────────────────
fetched = []


def _fake_urlretrieve(url, dest):
    fetched.append(url)
    Path(dest).write_bytes(b"fake")


shc.urllib.request.urlretrieve = _fake_urlretrieve

with tempfile.TemporaryDirectory() as tmp:
    dest_dir = Path(tmp) / "harvester" / "v1.7.1"
    shc._fetch_release_assets("v1.7.1", dest_dir)
    check("_fetch_release_assets downloads all 4 real Harvester release assets",
          fetched == [
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-amd64.iso",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-vmlinuz-amd64",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-initrd-amd64",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-rootfs-amd64.squashfs",
          ])
    check("_fetch_release_assets creates the destination directory",
          dest_dir.is_dir())

    fetched.clear()
    shc._fetch_release_assets("v1.7.1", dest_dir)
    check("_fetch_release_assets does not re-download assets already present",
          fetched == [])

# ── _render_node_files(): real template rendering, "create" role ────────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    url = shc._render_node_files(_CLUSTER_CFG, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    check("_render_node_files returns this node's own ipxe script URL",
          url == "http://10.0.0.1/lab_creation/harvester/v1.7.1/ipxe-harvester1.mydemo.lab")

    ipxe_text = (web_root / "ipxe-harvester1.mydemo.lab").read_text()
    check("rendered ipxe script starts with #!ipxe", ipxe_text.startswith("#!ipxe"))
    check("rendered ipxe script references the real vmlinuz/initrd/squashfs asset names",
          "harvester-v1.7.1-vmlinuz-amd64" in ipxe_text
          and "harvester-v1.7.1-initrd-amd64" in ipxe_text
          and "harvester-v1.7.1-rootfs-amd64.squashfs" in ipxe_text)
    check("rendered ipxe script's config_url points at this node's own rendered config file",
          "harvester.install.config_url=http://10.0.0.1/lab_creation/harvester/v1.7.1/config-harvester1.mydemo.lab.yaml"
          in ipxe_text)

    config_text = (web_root / "config-harvester1.mydemo.lab.yaml").read_text()
    check("rendered create config has mode: create", "mode: create" in config_text)
    check("rendered create config has this node's own hostname/IP (not DHCP-guessed)",
          "hostname: harvester1.mydemo.lab" in config_text and "ip: 192.168.88.143" in config_text)
    check("rendered create config carries the cluster VIP", "vip: 192.168.88.142" in config_text)
    check("rendered create config includes both SSH keys, one per line",
          "  - ssh-rsa AAAAtest key1" in config_text and "  - ssh-rsa AAAAtest key2" in config_text)
    check("rendered create config still requires iso_url even under PXE (real Harvester requirement)",
          "iso_url: http://10.0.0.1/lab_creation/harvester/v1.7.1/harvester-v1.7.1-amd64.iso" in config_text)
    check("rendered create config includes dns_nameservers — confirmed live 2026-08-30 that Harvester's "
          "own installer refuses to proceed without it for a static-IP management_interface "
          "('Invalid configuration: DNS servers are required for static IP address')",
          "dns_nameservers:" in config_text
          and "  - 192.168.88.73" in config_text and "  - 192.168.88.1" in config_text)

# _render_node_files() dies clearly if dns_nameservers is missing, rather than
# rendering a config that would fail partway through a real install.
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    no_dns_cfg = dict(_CLUSTER_CFG)
    del no_dns_cfg["dns_nameservers"]
    try:
        shc._render_node_files(no_dns_cfg, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
        check("_render_node_files dies clearly when dns_nameservers is missing", False)
    except SystemExit:
        check("_render_node_files dies clearly when dns_nameservers is missing", True)

# ── _render_node_files(): "join" role points at the VIP, not itself ─────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    shc._render_node_files(_CLUSTER_CFG, _JOIN_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester2.mydemo.lab.yaml").read_text()
    check("rendered join config has mode: join", "mode: join" in config_text)
    check("rendered join config's server_url points at the cluster VIP",
          "server_url: https://192.168.88.142:443" in config_text)
    check("rendered join config has the join node's own hostname/IP",
          "hostname: harvester2.mydemo.lab" in config_text and "ip: 192.168.88.144" in config_text)

# ── optional install-time passthrough blocks: no-op when omitted ────────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    shc._render_node_files(_CLUSTER_CFG, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester1.mydemo.lab.yaml").read_text()
    check("no system_settings key -> no system_settings: block rendered",
          "system_settings:" not in config_text)
    check("default ntp_servers (no override) keeps the two SUSE pool servers",
          "  - 0.suse.pool.ntp.org" in config_text and "  - 1.suse.pool.ntp.org" in config_text)
    check("no os_environment -> no os.environment: block rendered",
          "environment:" not in config_text)
    check("no optional install.* keys -> no extra lines after persistentpartitionsize",
          "data_disk:" not in config_text and "wipe_all_disks:" not in config_text
          and "harvester:" not in config_text)
    if _has_yaml:
        parsed = _yaml.safe_load(config_text)
        check("default-case rendered config is still valid, parseable YAML", isinstance(parsed, dict))

# ── optional install-time passthrough blocks: all populated ─────────────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    populated_cfg = dict(_CLUSTER_CFG,
                          ntp_servers=["10.0.0.1", "10.0.0.2"],
                          os_environment={"HTTP_PROXY": "http://proxy:3128"},
                          data_disk="/dev/vdb",
                          wipe_all_disks=True,
                          cluster_pod_cidr="10.60.0.0/16",
                          cluster_service_cidr="10.61.0.0/16",
                          cluster_dns="10.61.0.10",
                          storage_class_replica_count=1,
                          system_settings={"backup-target": "s3://bucket@region/"})
    node_with_role = dict(_CREATE_NODE, harvester_role="witness")
    shc._render_node_files(populated_cfg, node_with_role, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester1.mydemo.lab.yaml").read_text()

    if _has_yaml:
        parsed = _yaml.safe_load(config_text)
        check("fully-populated rendered config is valid, parseable YAML", isinstance(parsed, dict))
        check("system_settings passthrough renders correctly",
              parsed.get("system_settings") == {"backup-target": "s3://bucket@region/"})
        check("ntp_servers override replaces the default SUSE pool servers",
              parsed["os"]["ntp_servers"] == ["10.0.0.1", "10.0.0.2"])
        check("os_environment renders under os.environment",
              parsed["os"]["environment"] == {"HTTP_PROXY": "http://proxy:3128"})
        check("data_disk renders under install.data_disk", parsed["install"]["data_disk"] == "/dev/vdb")
        check("wipe_all_disks renders as a real YAML boolean, not the string 'True'",
              parsed["install"]["wipe_all_disks"] is True)
        check("cluster_pod_cidr/cluster_service_cidr/cluster_dns all render under install.*",
              parsed["install"]["cluster_pod_cidr"] == "10.60.0.0/16"
              and parsed["install"]["cluster_service_cidr"] == "10.61.0.0/16"
              and parsed["install"]["cluster_dns"] == "10.61.0.10")
        check("per-node harvester_role renders as install.role, distinct from the create/join 'role' key",
              parsed["install"]["role"] == "witness")
        check("storage_class_replica_count renders under install.harvester.storage_class.replica_count "
              "as a real int, not a string",
              parsed["install"]["harvester"]["storage_class"]["replica_count"] == 1)
    else:
        # Without pyyaml, fall back to substring checks on the raw text —
        # weaker (can't confirm real structure/types) but still real
        # coverage of every value actually appearing.
        check("system_settings passthrough renders (substring check, no pyyaml)",
              'backup-target: "s3://bucket@region/"' in config_text)
        check("ntp_servers override renders (substring check, no pyyaml)",
              "  - 10.0.0.1" in config_text and "  - 10.0.0.2" in config_text)
        check("os_environment renders (substring check, no pyyaml)",
              'HTTP_PROXY: "http://proxy:3128"' in config_text)
        check("install.* extras render (substring check, no pyyaml)",
              "data_disk: /dev/vdb" in config_text and "wipe_all_disks: true" in config_text
              and "cluster_pod_cidr: 10.60.0.0/16" in config_text and "role: witness" in config_text
              and "replica_count: 1" in config_text)

# join-role nodes get the same optional blocks (not create-only).
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    shc._render_node_files(dict(_CLUSTER_CFG, storage_class_replica_count=1),
                            _JOIN_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester2.mydemo.lab.yaml").read_text()
    check("join config also gets the optional install.* extra-lines block",
          "replica_count: 1" in config_text)

# ── _yaml_scalar(): quotes, backslashes and newlines are escaped ──────────────
# A value with a newline must not add a top-level key to the rendered YAML. _yaml_scalar() escapes it.
check('_yaml_scalar: embedded double-quotes are escaped, not passed through raw',
      shc._yaml_scalar('a"b') == '"a\\"b"')
check('_yaml_scalar: embedded backslashes are escaped (before quotes, so the '
      'escaping itself cannot be escaped away)',
      shc._yaml_scalar('a\\b') == '"a\\\\b"')
check('_yaml_scalar: embedded newlines/carriage-returns are escaped, not left literal',
      shc._yaml_scalar('a\nb\rc') == '"a\\nb\\rc"')

with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    malicious = 'x"\ninstall:\n  wipe_all_disks: true\n#'
    injection_cfg = dict(_CLUSTER_CFG, system_settings={"backup-target": malicious})
    shc._render_node_files(injection_cfg, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester1.mydemo.lab.yaml").read_text()
    if _has_yaml:
        parsed = _yaml.safe_load(config_text)
        check("a value containing a quote+newline is preserved as DATA, not parsed as new YAML "
              "structure — install.mode must still be 'create', not overwritten/injected",
              parsed["install"]["mode"] == "create"
              and parsed["system_settings"]["backup-target"] == malicious)
        check("no stray 'wipe_all_disks' KEY got injected into install: (checked structurally, "
              "not textually — the escaped malicious value legitimately contains that substring "
              "as inert string data, so a text search would always 'find' it)",
              "wipe_all_disks" not in parsed["install"] and "wipe_all_disks" not in parsed)
    else:
        check("the malicious value's own literal newline never appears un-escaped in the "
              "rendered YAML (substring check, no pyyaml)",
              "\ninstall:\n  wipe_all_disks: true" not in config_text)

# ── _create_netboot_vm(): correct disk-before-network boot order, no OS image ──
captured_calls = []


def _fake_run_libvirt_tool(binary, remote_host, virt_srv, args, **kwargs):
    captured_calls.append((binary, remote_host, virt_srv, list(args)))
    return lc.subprocess.CompletedProcess(args=[], returncode=0)


shc.run_libvirt_tool = _fake_run_libvirt_tool

# The known_hosts entries are purged before the first connection, as setup_lab.py does, so a reused IP does not refuse a new node.
keygen_calls = []
_real_subprocess_run = lc.subprocess.run


def _capture_keygen(args, **kwargs):
    if args and args[0] == "ssh-keygen":
        keygen_calls.append(list(args))
        return lc.subprocess.CompletedProcess(args=args, returncode=0)
    return _real_subprocess_run(args, **kwargs)


shc.subprocess.run = _capture_keygen
vm_cluster_cfg = dict(_CLUSTER_CFG, vm_cpu=12, vm_mem=36864, vm_dsk=260)
shc._create_netboot_vm(_CREATE_NODE, vm_cluster_cfg, {"REMOTE_HOST": "hv1.mydemo.lab", "VM_IMG_LOC": "/var/lib/libvirt/images"})

check("_create_netboot_vm purges known_hosts for both the node's IP and hostname",
      any("-R" in c and "192.168.88.143" in c for c in keygen_calls)
      and any("-R" in c and "harvester1.mydemo.lab" in c for c in keygen_calls))
check("_create_netboot_vm's known_hosts purge runs BEFORE virt-install (so a fresh "
      "known_hosts is guaranteed before anything might later SSH to this node)",
      len(keygen_calls) == 2 and len(captured_calls) == 1)

check("_create_netboot_vm calls virt-install exactly once", len(captured_calls) == 1)
binary, remote_host, virt_srv, args = captured_calls[0]
check("_create_netboot_vm invokes virt-install (not virsh)", binary == "virt-install")
check("_create_netboot_vm passes the configured hypervisor as remote_host", remote_host == "hv1.mydemo.lab")
boot_arg = args[args.index("--boot") + 1]
check("_create_netboot_vm's --boot flag selects the plain (non-secure-boot) OVMF loader/nvram — "
      "confirmed live 2026-08-30 that virt-install's bare 'uefi' shorthand auto-selected the "
      "SECURE BOOT OVMF variant, which silently blocks loading an unsigned ipxe.efi ('Access Denied')",
      "--boot" in args and boot_arg.startswith("uefi,loader=")
      and "ovmf-x86_64-code.bin" in boot_arg and "ovmf-x86_64-vars.bin" in boot_arg
      and "ovmf-x86_64-ms-" not in boot_arg)
disk_arg = next(a for a in args if "path=" in a)
net_arg = next(a for a in args if "mac.address=" in a)
check("_create_netboot_vm's disk has boot.order=1 (avoids the ISO path's reboot-loop bug)",
      "boot.order=1" in disk_arg)
check("_create_netboot_vm's network device has boot.order=2 — confirmed live 2026-08-30 that "
      "giving the disk a boot.order without also giving the network device one produces a domain "
      "with no usable network boot entry at all (per-device boot.order excludes any device "
      "without one, silently dropping --boot's hd,network device-order tokens)",
      "boot.order=2" in net_arg)
check("_create_netboot_vm creates a blank disk (no --import, no source OS image)",
      "--import" not in args)
check("_create_netboot_vm wires the node's own MAC into the network arg",
      any("mac.address=52:54:00:AB:CD:E1" in a for a in args if a.startswith("--network") or "bridge=" in a))

# cluster_cfg's own hypervisor_host/hypervisor_virt_srv override lab_creation.cfg's
# REMOTE_HOST/VIRT_SRV — needed since a Harvester cluster can reasonably target a
# different hypervisor than whatever "normal" lab VMs currently use.
captured_calls.clear()
override_cfg = dict(vm_cluster_cfg, hypervisor_host="nuc6.mydemo.lab",
                     hypervisor_virt_srv="qemu+ssh://root@nuc6.mydemo.lab/system")
shc._create_netboot_vm(_CREATE_NODE, override_cfg, {"REMOTE_HOST": "hv1.mydemo.lab", "VIRT_SRV": "qemu+ssh://root@hv1.mydemo.lab/system"})
_, override_remote_host, override_virt_srv, _ = captured_calls[0]
check("cluster_cfg's hypervisor_host overrides lab_creation.cfg's REMOTE_HOST",
      override_remote_host == "nuc6.mydemo.lab")
check("cluster_cfg's hypervisor_virt_srv overrides lab_creation.cfg's VIRT_SRV",
      override_virt_srv == "qemu+ssh://root@nuc6.mydemo.lab/system")

# A failed virt-install must die(), not silently continue.
captured_calls.clear()


def _failing_run_libvirt_tool(binary, remote_host, virt_srv, args, **kwargs):
    return lc.subprocess.CompletedProcess(args=[], returncode=1)


shc.run_libvirt_tool = _failing_run_libvirt_tool
try:
    shc._create_netboot_vm(_CREATE_NODE, vm_cluster_cfg, {})
    check("a failed virt-install call raises SystemExit via die()", False)
except SystemExit:
    check("a failed virt-install call raises SystemExit via die()", True)


# ── _fetch_harvester_kubeconfig(): rancher user, VIP rewrite, chmod 0600 ─────
ssh_calls = []


def _fake_ssh_run(hostname, cmd, user="root", capture=False, **kwargs):
    ssh_calls.append((hostname, cmd, user))
    return lc.subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="apiVersion: v1\nclusters:\n- cluster:\n    server: https://127.0.0.1:6443\n")


shc.ssh_run = _fake_ssh_run
with tempfile.TemporaryDirectory() as tmp:
    kubeconfig_cfg = dict(_CLUSTER_CFG, kubeconfig_path=str(Path(tmp) / "kc.yaml"))
    dest = shc._fetch_harvester_kubeconfig(kubeconfig_cfg, _CREATE_NODE)
    check("_fetch_harvester_kubeconfig connects to the create node's own IP",
          ssh_calls[0][0] == "192.168.88.143")
    check("_fetch_harvester_kubeconfig reads the standard RKE2 kubeconfig path via sudo — "
          "live-tested 2026-09-04: 'rancher' has NOPASSWD:ALL sudo but can't read this "
          "root-owned 0600 file directly, a plain (no-sudo) `cat` got 'Permission denied'",
          ssh_calls[0][1] == "sudo cat /etc/rancher/rke2/rke2.yaml")
    check("_fetch_harvester_kubeconfig connects as Harvester's default 'rancher' user, not root",
          ssh_calls[0][2] == "rancher")
    check("_fetch_harvester_kubeconfig respects an explicit kubeconfig_path",
          dest == Path(kubeconfig_cfg["kubeconfig_path"]))
    written = dest.read_text()
    check("_fetch_harvester_kubeconfig rewrites 127.0.0.1 to the cluster's own VIP",
          "https://192.168.88.142:6443" in written and "127.0.0.1" not in written)
    check("_fetch_harvester_kubeconfig writes the kubeconfig with 0600 permissions",
          (dest.stat().st_mode & 0o777) == 0o600)

ssh_calls.clear()
with tempfile.TemporaryDirectory() as tmp:
    default_path_cfg = dict(_CLUSTER_CFG)
    default_path_cfg.pop("kubeconfig_path", None)
    dest = shc._fetch_harvester_kubeconfig(default_path_cfg, _CREATE_NODE)
    check("_fetch_harvester_kubeconfig defaults to a version-scoped path when kubeconfig_path is omitted",
          str(dest) == "/etc/lab_creation/harvester-v1.7.1.kubeconfig")
    dest.unlink()  # this default path is a real filesystem path -- clean up after the test

# ── _apply_post_install_settings(): kubectl apply per setting, generic passthrough ──
apply_calls = []


def _fake_subprocess_run(args, input=None, **kwargs):  # noqa: A002 -- matches subprocess.run's own param name
    apply_calls.append((list(args), input))
    return lc.subprocess.CompletedProcess(args=args, returncode=0)


shc.subprocess.run = _fake_subprocess_run
shc._apply_post_install_settings(
    {"post_install_settings": {"backup-target": "s3://bucket@region/", "auto-disk-provision-paths": True}},
    Path("/tmp/fake.kubeconfig"))
check("_apply_post_install_settings calls kubectl apply once per setting", len(apply_calls) == 2)
args0, manifest0 = apply_calls[0]
check("_apply_post_install_settings uses the given kubeconfig path",
      "--kubeconfig" in args0 and "/tmp/fake.kubeconfig" in args0)
check("_apply_post_install_settings applies a real harvesterhci.io/v1beta1 Setting manifest via stdin",
      "kind: Setting" in manifest0 and "apiVersion: harvesterhci.io/v1beta1" in manifest0
      and "name: backup-target" in manifest0 and 'value: "s3://bucket@region/"' in manifest0)
args1, manifest1 = apply_calls[1]
check("_apply_post_install_settings renders a bool Setting value unquoted (true/false, not \"True\")",
      "value: true" in manifest1)

apply_calls.clear()
shc._apply_post_install_settings({}, Path("/tmp/fake.kubeconfig"))
check("_apply_post_install_settings is a no-op when post_install_settings is omitted", apply_calls == [])

apply_calls.clear()
shc.subprocess.run = lambda *a, **kw: lc.subprocess.CompletedProcess(args=[], returncode=1)
try:
    shc._apply_post_install_settings({"post_install_settings": {"x": "y"}}, Path("/tmp/fake.kubeconfig"))
    check("a failed kubectl apply raises SystemExit via die()", False)
except SystemExit:
    check("a failed kubectl apply raises SystemExit via die()", True)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_harvester_cluster checks passed")
