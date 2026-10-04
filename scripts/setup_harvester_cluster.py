#!/usr/bin/env python3.11
# Part of lab-in-a-box — stands up a Harvester HCI cluster via PXE netboot,
# using PXEService's new "ipxe-uefi" mode (libs/services.py). This is NOT a
# lab-JSON addon: standing up the Harvester cluster itself is infrastructure
# bootstrap (the same category as setup_kvm_node.py — provisioning the
# hypervisor a lab later runs on top of), not "define a lab", so it takes
# its own small config file instead of growing lab.json's schema.
#
# Design + the real Harvester PXE requirements behind this script were
# researched from Harvester's own docs (docs.harvesterhci.io) and the
# harvester/ipxe-examples repo's libvirt-specific PXE guide (a real,
# already-nested-KVM-VM walkthrough, i.e. this project's exact environment)
# — see TODO's "Give Harvester itself a lab-standard, repeatable install
# path" entry for the full research notes and the lessons learned live-
# testing the ISO-based install path this supersedes.
#
# Real findings that shaped this design:
#   - Harvester publishes vmlinuz/initrd/rootfs.squashfs as separate
#     release assets (releases.rancher.com/harvester/<version>/...)
#     alongside the ISO — no loop-mount/kernel-extraction dance needed,
#     unlike the ISO-based install path.
#   - `harvester.install.iso_url` is still required even under PXE (the
#     installer fetches the full ISO separately from the live squashfs) —
#     confirmed in harvester/ipxe-examples' own config-create.yaml.
#   - A `--boot uefi,hd,network` boot order (disk before network) makes the
#     reboot-loop problem the ISO-based path hit (a persistent
#     `--boot kernel=/initrd=` domain override) simply not exist: the first
#     boot (empty disk) falls through to PXE, every boot after install uses
#     the VM's own bootloader and never touches PXE again.
#   - dnsmasq (already this project's PXE engine) supports the two-stage
#     iPXE UEFI handshake natively via dhcp-userclass tagging — no new PXE
#     technology needed, just a second PXEService config mode.
#
# Usage:
#   setup_harvester_cluster.py <cluster.json>
#
# <cluster.json> is a small, standalone config — NOT a lab.json — see
# templates/harvester-cluster.json.example for every key.
__version__ = "fcbef10"

import subprocess
import sys
import urllib.request
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import services  # noqa: E402
from lab_creation import (  # noqa: E402
    log, die, run_libvirt_tool, check_ssh_conn, process_template, ssh_run, purge_known_host,
    yaml_scalar as _yaml_scalar,
)

# Release assets for a Harvester version: one ISO and three boot files, all under releases.rancher.com.
_RELEASE_BASE = "https://releases.rancher.com/harvester"
_ASSETS = ("amd64.iso", "vmlinuz-amd64", "initrd-amd64", "rootfs-amd64.squashfs")


def _fetch_release_assets(version, dest_dir):
    """Idempotently download this version's 4 release assets."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for suffix in _ASSETS:
        fname = "harvester-{}-{}".format(version, suffix)
        dest = dest_dir / fname
        if dest.exists():
            log("- {} already present, skipping download".format(fname))
            continue
        url = "{}/{}/{}".format(_RELEASE_BASE, version, fname)
        log("- downloading {}".format(url))
        try:
            urllib.request.urlretrieve(url, str(dest))
        except OSError as e:
            die("failed to download {}: {}".format(url, e))


# _yaml_scalar is lab_creation.yaml_scalar. The Harvester config and the install-ISO cloud-config use the same escaping.


def _build_system_settings_block(cluster_cfg):
    """
    Optional top-level `system_settings:` — a generic passthrough for any
    Harvester Setting overridable at install time (docs.harvesterhci.io's
    config reference lists this as a top-level key, alongside scheme_
    version/token). Returns "" (nothing) when cluster.json's
    "system_settings" is omitted, so this is a no-op unless the operator
    opts in. See _apply_post_install_settings() below for the
    post-install-only counterpart — which of Harvester's Settings actually
    work at install time vs. only post-install isn't fully mapped out (see
    TODO's "improvement of the Harvester installer" entry), so both
    mechanisms are offered rather than guessing.
    """
    settings = cluster_cfg.get("system_settings")
    if not settings:
        return ""
    lines = ["system_settings:"]
    for key, value in settings.items():
        lines.append("  {}: {}".format(key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n"


def _build_os_extra_lines(cluster_cfg):
    """
    Optional os.* keys beyond what every cluster always sets (hostname/
    ssh_authorized_keys/password/ntp_servers/dns_nameservers — handled
    directly in _render_node_files, always present). Returns a fully-
    indented block ending in its own trailing newline, or "" if nothing to
    add — process_template() has no conditionals of its own, so this is
    the same "build the block in Python, substitute one marker" pattern
    the existing ssh_keys_block/dns_nameservers_block already use.
    """
    lines = []
    environment = cluster_cfg.get("os_environment")
    if environment:
        lines.append("  environment:")
        for key, value in environment.items():
            lines.append("    {}: {}".format(key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n" if lines else ""


def _build_install_extra_lines(cluster_cfg, node):
    """
    Optional install.* keys beyond what every cluster always sets. Same
    block-substitution pattern as _build_os_extra_lines(). `node` supplies
    the one genuinely per-node option (harvester_role) — deliberately a
    different cluster.json key from the existing per-node "role"
    (create/join, an install MODE) to avoid confusing the two: Harvester's
    own "install.role" is a different axis entirely (default/management/
    worker/witness — node classification within an already-decided
    create/join cluster topology).
    """
    lines = []
    if cluster_cfg.get("data_disk"):
        lines.append("  data_disk: {}".format(cluster_cfg["data_disk"]))
    if "wipe_all_disks" in cluster_cfg:
        lines.append("  wipe_all_disks: {}".format(_yaml_scalar(bool(cluster_cfg["wipe_all_disks"]))))
    for key in ("cluster_pod_cidr", "cluster_service_cidr", "cluster_dns"):
        if cluster_cfg.get(key):
            lines.append("  {}: {}".format(key, cluster_cfg[key]))
    if node.get("harvester_role"):
        lines.append("  role: {}".format(node["harvester_role"]))
    # Real, high-value knob for THIS project specifically: Harvester
    # defaults to a 3-replica StorageClass, which silently degrades (or
    # never reaches Healthy) on a cluster with fewer than 3 nodes — exactly
    # the shape of templates/harvester-cluster.json.example's own 2-node
    # (create+join) sample.
    replica_count = cluster_cfg.get("storage_class_replica_count")
    if replica_count is not None:
        lines.append("  harvester:")
        lines.append("    storage_class:")
        lines.append("      replica_count: {}".format(int(replica_count)))
    return "\n".join(lines) + "\n" if lines else ""


def _render_node_files(cluster_cfg, node, http_base, web_root):
    """
    Render this node's own config-<name>.yaml (Harvester's install-config
    scheme) and ipxe-<name> boot script — one pair PER NODE rather than one
    shared script per role: every other node type in this project is always
    statically addressed (myip/mymac are explicit, never DHCP-guessed), so
    an explicit per-node hostname/IP config is the natural fit here too,
    not a per-role one with hostname/IP omitted for DHCP to fill in.

    Returns the HTTP URL of the rendered ipxe script — what PXEService's
    ipxe-uefi mode needs per node (its "pxe_ipxe_url" key).
    """
    templ_dir = Path(cluster_cfg["_templ_addons_loc"]) / "harvester_pxe"
    version = cluster_cfg["harvester_version"]
    keys_block = "\n".join("  - {}".format(k) for k in cluster_cfg["ssh_authorized_keys"])

    ntp_servers = cluster_cfg.get("ntp_servers") or ["0.suse.pool.ntp.org", "1.suse.pool.ntp.org"]

    common_vars = {
        "node_hostname": node["name"],
        "node_ip": node["ip"],
        "harvester_ssh_keys_block": keys_block,
        "harvester_password_hash": cluster_cfg["password_hash"],
        "harvester_token": cluster_cfg["token"],
        "harvester_iface": cluster_cfg["management_interface"],
        "harvester_netmask": cluster_cfg["netmask"],
        "harvester_gateway": cluster_cfg["gateway"],
        "harvester_ntp_servers_block": "\n".join("  - {}".format(s) for s in ntp_servers),
        "harvester_system_settings_block": _build_system_settings_block(cluster_cfg),
        "harvester_os_extra_lines": _build_os_extra_lines(cluster_cfg),
        "harvester_install_extra_lines": _build_install_extra_lines(cluster_cfg, node),
        # Harvester requires DNS servers for a static IP. The installer refuses to proceed without them, so the function stops here
        # rather than rendering a config that would fail during the install.
        "harvester_dns_nameservers_block": "\n".join(
            "  - {}".format(d) for d in (cluster_cfg.get("dns_nameservers") or
                                          die("cluster config has no 'dns_nameservers' — required by "
                                              "Harvester's own installer for a static-IP management_interface"))),
        "harvester_device": cluster_cfg.get("device", "/dev/vda"),
        "harvester_iso_url": "{}/harvester-{}-amd64.iso".format(http_base, version),
        "harvester_vip": cluster_cfg["vip"],
        "harvester_vip_mode": cluster_cfg.get("vip_mode", "static"),
        "harvester_persistent_size": cluster_cfg.get("persistent_partition_size", "150Gi"),
    }

    role_template = "config-create.yaml.tmpl" if node["role"] == "create" else "config-join.yaml.tmpl"
    config_text = process_template(str(templ_dir / role_template), common_vars)
    config_name = "config-{}.yaml".format(node["name"])
    (web_root / config_name).write_text(config_text)

    ipxe_vars = {
        "harvester_http_base": http_base,
        "harvester_version": version,
        "harvester_config_url": "{}/{}".format(http_base, config_name),
    }
    ipxe_text = process_template(str(templ_dir / "ipxe.tmpl"), ipxe_vars)
    ipxe_name = "ipxe-{}".format(node["name"])
    (web_root / ipxe_name).write_text(ipxe_text)

    return "{}/{}".format(http_base, ipxe_name)


def _create_netboot_vm(node, cluster_cfg, config):
    """
    Define and start a VM with an empty disk (boot.order=1) and a network device (boot.order=2). The disk comes first, so the
    empty disk falls through to PXE, and later boots use the installed disk. The ISO-based install path needs no kernel-boot
    override for this, because the domain never gets one.

    Both boot.order values are needed. Once one device has a boot.order, libvirt uses per-device order and ignores the global
    device list in --boot. The --boot flag here selects only the firmware, uefi.

    The OVMF loader and NVRAM paths are set explicitly, to the non-Secure-Boot files. The plain --boot uefi shorthand picks the
    Secure Boot variant on this host, which blocks loading ipxe.efi, because that file is not signed for the VM's Secure Boot
    database.
    """
    # The hypervisor_* keys in cluster_cfg take priority over REMOTE_HOST, VIRT_SRV and VM_IMG_LOC in lab_creation.cfg. A Harvester
    # cluster can be built on a different hypervisor from the one that the shared config points at for ordinary lab VMs.
    remote_host = cluster_cfg.get("hypervisor_host") or config.get("REMOTE_HOST", "")
    virt_srv = cluster_cfg.get("hypervisor_virt_srv") or config.get("VIRT_SRV", "qemu:///system")
    vm_img_loc = cluster_cfg.get("vm_img_loc") or config.get("VM_IMG_LOC", "/var/lib/libvirt/images")

    # Stale host keys are purged here, as in setup_lab.py. A reused lab IP can have a host key for another VM on file, and then
    # StrictHostKeyChecking=accept-new refuses a connection to a node whose sshd is answering correctly.
    purge_known_host(node["ip"], node["name"])

    args = [
        "--name", node["name"], "--autostart",
        "--boot", "uefi,loader={0},loader.readonly=yes,loader.type=pflash,"
        "nvram.template={1}".format(
            cluster_cfg.get("ovmf_code", "/usr/share/qemu/ovmf-x86_64-code.bin"),
            cluster_cfg.get("ovmf_vars", "/usr/share/qemu/ovmf-x86_64-vars.bin")),
        "--vcpus", str(cluster_cfg.get("vm_cpu", 12)),
        "--memory", str(cluster_cfg.get("vm_mem", 36864)),
        "--os-variant", cluster_cfg.get("os_variant", "generic"),
        "--disk", "size={},path={}/{}.qcow2,sparse=no,bus=virtio,boot.order=1".format(
            cluster_cfg.get("vm_dsk", 260), vm_img_loc, node["name"]),
        "--network", "bridge={},mac.address={},model=virtio,boot.order=2".format(
            cluster_cfg.get("hypervisor_bridge", "br0"), node["mac"]),
        "--graphics", "spice,listen=0.0.0.0", "--noautoconsole",
    ]
    r = run_libvirt_tool("virt-install", remote_host, virt_srv, args)
    if r.returncode != 0:
        die("virt-install failed for '{}'".format(node["name"]))


def _fetch_harvester_kubeconfig(cluster_cfg, create_node):
    """
    Fetch the kubeconfig from the 'create' node once the cluster is Active, so that _apply_post_install_settings() can reach the
    cluster through kubectl. Like HarvesterBackend, this uses a local kubeconfig file, HARVESTER_KUBECONFIG.

    Harvester disables root SSH by default. The connection uses the "rancher" user, which has passwordless sudo. The kubeconfig is
    root-owned with mode 0600, so it is read with sudo.

    The server address in the kubeconfig is 127.0.0.1. The function rewrites it to the cluster VIP, which the automation VM can
    reach.
    """
    kubeconfig_text = ssh_run(
        create_node["ip"], "sudo cat /etc/rancher/rke2/rke2.yaml", user="rancher", capture=True).stdout
    # rke2.yaml points at 127.0.0.1 by default — rewrite to the cluster's
    # own VIP so the fetched kubeconfig is usable from the automation VM,
    # not just from the node itself.
    kubeconfig_text = kubeconfig_text.replace(
        "https://127.0.0.1:6443", "https://{}:6443".format(cluster_cfg["vip"]))
    dest = Path(cluster_cfg.get("kubeconfig_path") or
                "/etc/lab_creation/harvester-{}.kubeconfig".format(cluster_cfg["harvester_version"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(kubeconfig_text)
    dest.chmod(0o600)
    return dest


def _apply_post_install_settings(cluster_cfg, kubeconfig_path):
    """
    Apply cluster.json's optional "post_install_settings" dict as
    harvesterhci.io/v1beta1 Setting objects — the post-install counterpart
    to _build_system_settings_block()'s install-time passthrough, for
    whichever Settings turn out not to be settable at install time. Same
    "operator pre-configures it, we don't validate Setting semantics"
    stance throughout this file.
    """
    settings = cluster_cfg.get("post_install_settings")
    if not settings:
        return
    for name, value in settings.items():
        manifest = (
            "apiVersion: harvesterhci.io/v1beta1\n"
            "kind: Setting\n"
            "metadata:\n"
            "  name: {}\n"
            "value: {}\n"
        ).format(name, _yaml_scalar(value))
        log("- applying Harvester Setting '{}'".format(name))
        r = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"],
            input=manifest, universal_newlines=True)
        if r.returncode != 0:
            die("failed to apply Harvester Setting '{}'".format(name))


def main():
    if len(sys.argv) < 2:
        print("Usage: {} <cluster.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)

    cluster_cfg = primary.load_definition(sys.argv[1])
    defaults = primary.load_defaults()
    config = primary.load_config()

    cluster_cfg["_templ_addons_loc"] = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")

    # Harvester release assets are several GB, and the default LAB_SETUP_PATH may not have room for them. web_root and http_base
    # let a deployment serve large images from another location, such as the tree that ISO_LOC installs use. Both default to the
    # LAB_SETUP_PATH behaviour when omitted.
    web_root = Path(cluster_cfg["web_root"]) if cluster_cfg.get("web_root") else \
        Path(lab_setup_path) / "harvester" / cluster_cfg["harvester_version"]

    if cluster_cfg.get("http_base"):
        http_base = cluster_cfg["http_base"]
    else:
        # The netbooting VMs need to reach THIS automation VM over HTTP (it
        # serves the rendered configs/scripts/release assets) — NOT the
        # hypervisor (config's REMOTE_HOST), which is a different machine
        # and runs no HTTP server for any of this. "mysource" is
        # lab_creation.cfg's own existing key for "the hostname of the lab
        # automation server".
        http_host = cluster_cfg.get("automation_ip") or config.get("mysource")
        if not http_host:
            die("no 'mysource' in lab_creation.cfg and no 'automation_ip' set in the cluster config — "
                "need a real address the netbooting VMs can reach this automation VM at")
        http_base = "http://{}/lab_creation/harvester/{}".format(http_host, cluster_cfg["harvester_version"])

    if not any(n.get("role") == "create" for n in cluster_cfg["nodes"]):
        die("cluster config has no node with role 'create' — exactly one is required to bootstrap the cluster")

    log("Fetching Harvester {} release assets".format(cluster_cfg["harvester_version"]))
    _fetch_release_assets(cluster_cfg["harvester_version"], web_root)

    log("Rendering per-node install config + iPXE boot scripts")
    definition_nodes = {}
    for node in cluster_cfg["nodes"]:
        ipxe_url = _render_node_files(cluster_cfg, node, http_base, web_root)
        definition_nodes[node["name"]] = {"mymac": node["mac"], "pxe_ipxe_url": ipxe_url}

    definition = {
        "nodes": definition_nodes,
        "pxe": {
            "pxe_mode": "ipxe-uefi",
            # This is the interface that PXEService's dnsmasq binds to on the automation VM. It is not the hypervisor's bridge name
            # (hypervisor_bridge), because a nested automation VM has its own interface name. Set it to the automation VM's own
            # interface, as shown by `ip -o link show`.
            "pxe_bridge": cluster_cfg.get("pxe_bridge") or die(
                "cluster config has no 'pxe_bridge' — set it to this automation VM's own "
                "network interface name (see `ip -o link show`), not the hypervisor's bridge name"),
            # The default mode is proxy, as in PXEService. Lab bridges are often bridged to the physical LAN, and a full dnsmasq would
            # hand out leases to every device there. Proxy mode answers only PXE requests. Set "full" only for an isolated bridge with
            # no other DHCP server.
            "pxe_dhcp_mode": cluster_cfg.get("dhcp_mode", "proxy"),
            "pxe_dhcp_range_start": cluster_cfg.get("dhcp_range_start"),
            "pxe_dhcp_range_end": cluster_cfg.get("dhcp_range_end"),
            # required by "proxy" mode (the default) — see
            # PXEService's own _dnsmasq_conf() docstring for why this
            # must be a network address, not an interface name.
            "pxe_dhcp_proxy_subnet": cluster_cfg.get("dhcp_proxy_subnet"),
        },
    }

    log("Configuring PXE service (ipxe-uefi mode)")
    svc = services.get("pxe", lab_setup_path=lab_setup_path)
    svc.install()
    svc.configure(definition, config)
    svc.enable()

    if cluster_cfg.get("create_vms", True):
        log("Creating netboot VMs")
        for node in cluster_cfg["nodes"]:
            _create_netboot_vm(node, cluster_cfg, config)

    log("Waiting for the 'create' node's VIP ({}) to come up — this can take 15-30+ minutes "
        "(install + first-boot cluster bootstrap)".format(cluster_cfg["vip"]))
    check_ssh_conn(cluster_cfg["vip"], tcp_port=443, retry_interval=15, retry_limit=240)
    log("Harvester VIP is responding — check https://{}/ to confirm cluster health".format(cluster_cfg["vip"]))

    if cluster_cfg.get("post_install_settings"):
        create_node = next(n for n in cluster_cfg["nodes"] if n.get("role") == "create")
        log("Fetching kubeconfig from '{}' to apply post-install settings".format(create_node["name"]))
        kubeconfig_path = _fetch_harvester_kubeconfig(cluster_cfg, create_node)
        _apply_post_install_settings(cluster_cfg, kubeconfig_path)


if __name__ == "__main__":
    main()
