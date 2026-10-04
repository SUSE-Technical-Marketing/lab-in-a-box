#!/usr/bin/env python3.11
# Part of lab-in-a-box. Delivers a completed lab onto a USB stick, so it can run standalone on other hardware.
#
# Design: one ordinary VM, the lab-host VM, is created with the normal VM pipeline and bootstrapped with the NAT-mode automation
# VM flow (setup_kvm_node.py and setup_lab_automation.sh, _network_mode=nat). The lab's own setup_lab.py then runs unchanged on
# that nested automation VM. The lab definition is copied with its node addresses moved into the internal NAT range, in memory
# only (see libs/lab_usb.py). When the lab-host VM is shut down, its raw disk is a complete, bootable image of the whole lab.
#
# The design and the task breakdown are described in the repo TODO.
#
# Usage:
#   build_lab_usb.py <lab.json> [--build-only]
#
# --build-only stops when the lab-host VM's raw disk is a complete, shut-down image, and prints its path. It does not write to a
# USB device. Writing to a real device (selection, confirmation, dd and growing the partition) is not implemented yet, so pass
# --build-only until it is.
__version__ = "6be996d"

import ipaddress
import subprocess
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import backends  # noqa: E402
import lab_usb  # noqa: E402
from lab_creation import (  # noqa: E402
    log, die, ssh_run, ssh_output, run_libvirt_tool, check_ssh_conn,
    prepare_cloud_init, copy_to_hypervisor,
    total_lab_resources, ensure_lab_ssh_key, distribute_lab_ssh_key,
    _generate_unused_mac, purge_known_host,
)

# The lab-host VM uses openSUSE Leap 16.0 Cloud, not Leap Micro. Leap Micro has an immutable root, so virt-customize cannot write
# its log in the guest, and it ships an interactive first-boot wizard that blocks a headless boot. The lab-host VM is a normal
# writable Linux host. Every other node in this project defaults to SLE Micro, which needs SCC credentials.
# cloud-init makes the DHCP path work (templates/cloud-init.template_network-config-dhcp, and the DHCP branch of
# lab_creation.prepare_cloud_init).
# The 16.0 filename has no openSUSE- prefix, and it uses the Cloud variant rather than kvm-and-xen.
_DEFAULT_LAB_HOST_ISO_IMAGE = "Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2"
_DEFAULT_LAB_HOST_OS_VARIANT = "opensuse16.0"

# Headroom is added to the lab's totals (total_lab_resources) for the lab-host VM's own OS and for the nested automation VM.
# The nested VM runs the Kubernetes installs and the web UI, so it needs real resources of its own.
_CPU_OVERHEAD = 4
_MEM_OVERHEAD_MIB = 8192
_DISK_OVERHEAD_GIB = 80

# The nested automation VM has a static IP inside the internal NAT network. The address is host 2, because host 1 is the gateway.
_NESTED_AUTOMATION_IP = "192.168.150.2"
_NAT_NETWORK_NAME = "labnat"
_NAT_NETWORK_CIDR = "192.168.150.0/24"


def _find_repo_root():
    """
    Locate the lab-in-a-box git checkout. The checkout is needed to copy setup_demo_server/ and libs/ to the lab-host VM, and to
    rsync the whole tree to the nested automation VM.

    install_automation_node_scripts.sh deploys scripts one by one, so the sibling directories are not next to an installed copy.
    An installed copy in /usr/local/bin resolves its parent directories to /usr/local, which has no setup_demo_server/ directory.
    The function therefore cannot rely on a fixed relative path.
    """
    result = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    die("build_lab_usb.py must be run from inside a real lab-in-a-box git checkout — it needs "
        "the full repo tree (setup_demo_server/, libs/, the whole thing to rsync onto the nested "
        "automation VM), not just what install_automation_node_scripts.sh deploys individually. "
        "Run it as e.g. 'cd /path/to/lab-in-a-box && python3.11 scripts/build_lab_usb.py <lab.json>', "
        "not the installed /usr/local/bin/build_lab_usb.py copy.")


def _lab_host_name(definition):
    common = definition.get("common", {}) or {}
    domain = common.get("mydomain") or "mydemo.lab"
    return "labhost.{}".format(domain)


def create_lab_host_vm(definition, config, defaults, host_name):
    """
    Creates the lab-host VM itself via this project's existing create_vm —
    nothing special (per the user's own framing): DHCP (myip omitted),
    disk_format="raw" (so its own disk can be dd'd onto a USB stick
    afterward — see create_vm's docstring), sized to hold the whole lab
    plus headroom for its own OS and the nested automation VM.

    Returns (backend, remote_host, virt_srv) — the caller needs these to
    keep talking to the OUTER hypervisor (to discover the lab-host VM's
    DHCP-assigned IP, and later to shut it down / locate its disk file).
    """
    total_cpu, total_mem, total_disk = total_lab_resources(definition)
    vm_cpu = total_cpu + _CPU_OVERHEAD
    vm_mem = total_mem + _MEM_OVERHEAD_MIB
    vm_dsk = total_disk + _DISK_OVERHEAD_GIB
    log("Lab-host VM sized at {} vCPU, {} MiB RAM, {} GiB disk "
        "(lab totals {}/{}/{} + overhead {}/{}/{})".format(
            vm_cpu, vm_mem, vm_dsk, total_cpu, total_mem, total_disk,
            _CPU_OVERHEAD, _MEM_OVERHEAD_MIB, _DISK_OVERHEAD_GIB))

    remote_host = config.get("REMOTE_HOST", "")
    virt_srv = config.get("VIRT_SRV", "qemu:///system")
    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    iso_loc = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")

    backend = backends.LibvirtBackend(
        virt_srv, remote_host=remote_host, vm_img_loc=vm_img_loc,
        iso_loc=iso_loc, lab_setup_path=lab_setup_path,
    )

    _, mac_by_domain = backend.list_used_macs()
    mymac = _generate_unused_mac(set(mac_by_domain.values()))
    network = "{},mac.address={}".format(config.get("NETWORK", "bridge=br0"), mymac)
    log("- Lab-host VM '{}' will use MAC {}".format(host_name, mymac))

    backend.copy_vm_image(_DEFAULT_LAB_HOST_ISO_IMAGE, host_name, str(vm_dsk),
                           config_method="cloud-init", disk_format="raw")

    env = dict(defaults)
    env.update(config)
    env["myip"] = ""  # DHCP — see prepare_cloud_init's dhcp-when-empty-myip branch
    env["mymac"] = mymac
    env["mydomain"] = (definition.get("common", {}) or {}).get("mydomain") or "mydemo.lab"
    prepare_cloud_init(host_name, lab_setup_path, env)
    copy_to_hypervisor(remote_host, lab_setup_path, host_name, config_method="cloud-init", vm_img_loc=vm_img_loc)

    backend.create_vm(
        host_name, str(vm_cpu), str(vm_mem), str(vm_dsk), network,
        os_variant=_DEFAULT_LAB_HOST_OS_VARIANT, boot="uefi", config_method="cloud-init",
        disk_format="raw",
    )
    return backend, remote_host, virt_srv, mymac


def discover_lab_host_ip(host_name, remote_host, virt_srv, mymac, bridge, retry_limit=30, retry_interval=10):
    """
    Find the IP address of the lab-host VM. The VM gets a DHCP lease rather than a known static address.

    The primary method is `virsh domifaddr --source agent`, which needs qemu-guest-agent. The openSUSE Leap 15.6 Cloud image ships
    it and starts it by default. The Leap Micro image is not used, because it has no usable guest agent.

    The fallback reads the hypervisor's ARP table after a broadcast ping. Many guests ignore broadcast ICMP, and DHCP traffic does
    not always fill the neighbour cache. The fallback is therefore best effort, not the primary method.
    """
    log("Waiting for the lab-host VM's DHCP-assigned IP (via the QEMU guest agent)…")
    for _ in range(retry_limit):
        result = run_libvirt_tool(
            "virsh", remote_host, virt_srv,
            ["domifaddr", host_name, "--source", "agent"],
            capture_output=True, text=True, check=False,
        )
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[-2] == "ipv4" and "/" in parts[-1]:
                ip = parts[-1].split("/")[0]
                if not ip.startswith("127."):
                    return ip
        time.sleep(retry_interval)

    log("Guest agent never responded — falling back to the hypervisor's own ARP table")
    import ipaddress as _ipaddress
    addr_out = ssh_output(remote_host, "ip -4 -o addr show dev {} | awk '{{print $4}}'".format(bridge))
    if not addr_out:
        die("could not determine {}'s own IPv4 address/CIDR on {}".format(bridge, remote_host))
    broadcast = str(_ipaddress.ip_interface(addr_out.splitlines()[0]).network.broadcast_address)
    for _ in range(retry_limit):
        ssh_run(remote_host, "ping -b -c 2 -w 2 {} >/dev/null 2>&1; true".format(broadcast), check=False)
        result = ssh_run(remote_host, "ip neigh show", capture=True, check=False)
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[4].lower() == mymac.lower():
                return parts[0]
        time.sleep(retry_interval)
    die("Timed out waiting for the lab-host VM's DHCP-assigned IP via both the guest agent "
        "and {}'s ARP table (MAC {})".format(remote_host, mymac))


def bootstrap_lab_host_vm(lab_host_ip, defaults):
    """
    Turn the freshly booted lab-host VM into a KVM host that runs a nested automation VM in NAT mode. The function reuses
    setup_kvm_node.py and setup_lab_automation.sh unchanged. This is the NAT-mode automation VM flow, one level deeper than usual.
    Beyond checking that the copy and the remote invocation work, the bootstrap logic needs no new verification.
    """
    # install_demo_server_scripts.sh only checks for python3.11, and it does not install it. The Leap 15.6 Cloud image does not ship
    # it, so the package is installed here.
    #
    # nc is also missing from that image. setup_kvm_node.py probes the host with nc when it runs without a target, and it fails
    # with an uncaught FileNotFoundError when nc is not installed. The package is installed here, as python311 is.
    log("Installing python3.11 + nc (prerequisites setup_kvm_node.py itself only checks for, doesn't install)")
    # Leap 16.0 has no python311 package. Its system python3 is 3.13. setup_kvm_node.py calls python3.11 by name, so a symlink
    # python3.11 to python3 is created when the package is missing and python3 is new enough.
    r = ssh_run(lab_host_ip, "zypper --gpg-auto-import-keys install -y python311", check=False)
    if r.returncode != 0:
        ssh_run(
            lab_host_ip,
            "command -v python3.11 >/dev/null 2>&1 || "
            "{ v=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3, 7))'); "
            "[ \"$v\" = True ] && ln -sf \"$(command -v python3)\" /usr/local/bin/python3.11; }",
        )
    ssh_run(lab_host_ip, "zypper --gpg-auto-import-keys install -y netcat-openbsd")

    # A Leap Cloud image may ship kernel-default-base, a kernel without KVM modules. /dev/kvm is then missing, and nested VMs would
    # use QEMU emulation. The real kernel-default package replaces it, and a reboot follows. The step is skipped once /dev/kvm
    # exists, because a reboot on every run is slow and can break an SSH command that is running at the time.
    has_kvm = ssh_run(lab_host_ip, "test -e /dev/kvm", check=False).returncode == 0
    if not has_kvm:
        log("Installing kernel-default (kernel-default-base ships no KVM modules) and rebooting")
        ssh_run(
            lab_host_ip,
            "zypper --gpg-auto-import-keys --non-interactive install --force-resolution kernel-default",
            input_text="1\n",
        )
        ssh_run(lab_host_ip, "reboot", check=False)
        check_ssh_conn(lab_host_ip)

    log("Copying setup_demo_server/ + libs/ onto the lab-host VM")
    repo_root = _find_repo_root()
    ssh_run(lab_host_ip, "mkdir -p /root/setup_demo_server /root/libs")
    # setup_kvm_node.py resolves its own libs/ as a SIBLING directory
    # (_SCRIPT_DIR.parent / "libs") — mirroring the real repo layout, not
    # just the deployed /usr/local/lib/lab_creation one, which doesn't
    # exist yet on a brand-new host. Both dirs need to land as actual
    # siblings under /root for that resolution to work.
    for src, dst in (("setup_demo_server", "/root/setup_demo_server/"), ("libs", "/root/libs/")):
        r = subprocess.run([
            "rsync", "-aq", "{}/".format(repo_root / src), "root@{}:{}".format(lab_host_ip, dst),
        ])
        if r.returncode != 0:
            die("failed to rsync {}/ to the lab-host VM".format(src))

    # setup_lab_automation.sh and setup_kvm_node.py load lab.cfg only, with no defaults. A minimal lab.cfg misses required variables,
    # such as _qemu_addr and ROOT_SSH_PUB_KEY. The complete lab.cfg.template from setup_demo_server/ is therefore used, and only the
    # keys this flow changes are overridden.
    template_path = repo_root / "setup_demo_server" / "lab.cfg.template"
    local_pubkey = Path("/root/.ssh/id_rsa.pub")
    if not local_pubkey.is_file():
        die("no local {} found — needed to seed the lab-host VM's "
            "ROOT_SSH_PUB_KEY".format(local_pubkey))
    # setup_lab_automation.sh uses _mygw, _mydns, _mynet and _mynetrev directly, with no NAT-aware derivation. configure_nat_network()
    # gives the NAT range's first host address to the libvirt gateway and DNS forwarder, so all four values are derived from the
    # same CIDR as the network itself.
    _nat_net = ipaddress.ip_network(_NAT_NETWORK_CIDR, strict=False)
    _nat_gateway = str(list(_nat_net.hosts())[0])
    overrides = {
        "_network_mode": "\"nat\"",
        "_nat_network_name": "\"{}\"".format(_NAT_NETWORK_NAME),
        "_nat_network_cidr": "\"{}\"".format(_NAT_NETWORK_CIDR),
        "_myip": "\"{}\"".format(_NESTED_AUTOMATION_IP),
        "_mygw": _nat_gateway,
        "_mydns": _nat_gateway,
        "_mynet": _NAT_NETWORK_CIDR,
        "_mynetrev": ".".join(reversed(str(_nat_net.network_address).split(".")[:3])),
        "AUTOMATION_HOSTNAME": "'automation.lab'",
        "_QCOW_IMAGE": defaults.get(
            "_QCOW_IMAGE",
            "/var/lib/libvirt/images/sources/openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2",
        ),
        "ROOT_SSH_PUB_KEY": "'{}'".format(local_pubkey.read_text().strip()),
        # The template's default graphics type, spice, needs QEMU built with spice support, which a minimal host does not have. none
        # does not work either: with --graphics=none the openSUSE appliance image busy-loops during boot and never brings up its
        # network. A real graphics device lets it boot. vnc needs no extra packages.
        "_automation_graphics": "\"vnc\"",
    }
    lab_cfg_lines = []
    seen = set()
    for line in template_path.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        if key in overrides:
            lab_cfg_lines.append("{}={}".format(key, overrides[key]))
            seen.add(key)
        else:
            lab_cfg_lines.append(line)
    # Any override key lab.cfg.template didn't already have a line for
    # (shouldn't happen today, but fail loudly rather than silently drop it
    # if the template ever changes shape).
    missing = set(overrides) - seen
    if missing:
        die("lab.cfg.template is missing expected key(s): {}".format(", ".join(sorted(missing))))
    ssh_run(lab_host_ip, "cat > /root/setup_demo_server/lab.cfg", input_text="\n".join(lab_cfg_lines) + "\n")

    log("Running setup_kvm_node.py on the lab-host VM "
        "(creates the NAT'd libvirt network + the nested automation VM — unchanged, already-tested code)")
    ssh_run(
        lab_host_ip,
        "cd /root/setup_demo_server && python3.11 setup_kvm_node.py -y",
        check=True,
    )
    # _NESTED_AUTOMATION_IP is a fixed constant, reused unchanged across
    # every run of this script — a stale host key from a PREVIOUS run's
    # nested automation VM would otherwise collide here every single time,
    # not just occasionally like a real DHCP-assigned address would.
    purge_known_host(_NESTED_AUTOMATION_IP)
    check_ssh_conn(_NESTED_AUTOMATION_IP)
    log("Nested automation VM is up at {}".format(_NESTED_AUTOMATION_IP))


def deploy_lab_on_nested_automation(definition, host_name_hint):
    """
    Installs the lab_creation toolchain onto the nested automation VM
    (install_automation_node_scripts.sh, unchanged), points its own
    REMOTE_HOST back at the lab-host VM itself (which is "the hypervisor"
    from the nested automation VM's point of view), then runs setup_lab.py
    on it — unchanged — against a remapped copy of the original lab
    definition. This is where every lab VM actually gets created; no new
    VM-creation code exists in this file at all for that.
    """
    log("Installing lab_creation onto the nested automation VM")
    ssh_run(_NESTED_AUTOMATION_IP, "mkdir -p /root/lab-in-a-box")
    repo_root = str(_find_repo_root())
    r = subprocess.run([
        "rsync", "-aq", "--exclude=.git",
        "{}/".format(repo_root), "root@{}:/root/lab-in-a-box/".format(_NESTED_AUTOMATION_IP),
    ])
    if r.returncode != 0:
        die("failed to rsync the lab-in-a-box repo to the nested automation VM")
    ssh_run(_NESTED_AUTOMATION_IP, "cd /root/lab-in-a-box && ./install_automation_node_scripts.sh")

    lab_creation_cfg = (
        "REMOTE_HOST=\"{gw}\"\n"
        "VIRT_SRV=\"qemu+ssh://root@{gw}/system?keyfile=.ssh/id_rsa\"\n"
        "ROOT_SSH_KEY=\"$(cat /root/.ssh/id_rsa.pub)\"\n"
        "NETWORK=\"network={nat_name}\"\n"
    ).format(gw=_NESTED_AUTOMATION_IP.rsplit(".", 1)[0] + ".1", nat_name=_NAT_NETWORK_NAME)
    ssh_run(_NESTED_AUTOMATION_IP,
            "cp /etc/lab_creation.cfg.example /etc/lab_creation.cfg 2>/dev/null; "
            "cat > /etc/lab_creation.cfg", input_text=lab_creation_cfg)

    remapped = lab_usb.remap_lab_definition_to_nat(
        definition, nat_cidr=_NAT_NETWORK_CIDR, nested_automation_ip=_NESTED_AUTOMATION_IP)
    remapped_json = __import__("json").dumps(remapped, indent=2)
    ssh_run(_NESTED_AUTOMATION_IP, "cat > /root/lab.json", input_text=remapped_json)

    log("Running setup_lab.py on the nested automation VM — every lab VM is created from here on, "
        "using the same unchanged code path as any other deployment")
    ssh_run(_NESTED_AUTOMATION_IP, "setup_lab.py /root/lab.json")


def configure_handoff(definition):
    """
    Task 3's remaining pieces (SSH key generation/distribution is already
    done — ensure_lab_ssh_key/distribute_lab_ssh_key, called by the
    caller): the informational site on port 6969, MOTD, and a random
    8-digit numeric root password written into /etc/issue.
    """
    import random
    password = "".join(str(random.randint(0, 9)) for _ in range(8))
    ssh_run(_NESTED_AUTOMATION_IP, "echo 'root:{}' | chpasswd".format(password))

    nodes = list((definition.get("nodes", {}) or {}).keys())
    site_html = (
        "<!doctype html><html><head><title>Lab info</title></head><body>"
        "<h1>lab-in-a-box — delivered lab</h1>"
        "<p>Nodes in this lab: {}</p>"
        "<p>Root password for this appliance: see /etc/issue on the console.</p>"
        "</body></html>"
    ).format(", ".join(nodes) or "(none)")
    ssh_run(_NESTED_AUTOMATION_IP, "mkdir -p /root/lab-info-site && cat > /root/lab-info-site/index.html",
            input_text=site_html)
    ssh_run(_NESTED_AUTOMATION_IP,
            "cd /root/lab-info-site && nohup python3.11 -m http.server 6969 "
            ">/var/log/lab-info-site.log 2>&1 & disown")

    issue_text = (
        "\\S\n"
        "This is a lab-in-a-box delivered lab appliance.\n"
        "Root password: {}\n"
        "Lab info: http://<this-host>:6969/\n"
    ).format(password)
    ssh_run(_NESTED_AUTOMATION_IP, "cat > /etc/issue", input_text=issue_text)
    ssh_run(_NESTED_AUTOMATION_IP,
            "echo 'Lab info available at http://\\$(hostname -I | awk \"{print \\$1}\"):6969/' > /etc/motd")
    log("Root password (also written to /etc/issue on the nested automation VM): {}".format(password))


def main():
    if len(sys.argv) < 2:
        print("Usage: {} <lab.json> [--build-only]".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    build_only = "--build-only" in sys.argv[2:]
    if "--build-only" not in sys.argv[2:] and len(sys.argv) > 2:
        die("Unknown argument(s): {}".format(" ".join(a for a in sys.argv[2:] if a != "--build-only")))

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    total_cpu, total_mem, total_disk = total_lab_resources(definition)
    log("Lab totals: {} vCPU, {} MiB RAM, {} GiB disk across {} node(s)".format(
        total_cpu, total_mem, total_disk, len(definition.get("nodes", {}))))

    host_name = _lab_host_name(definition)
    backend, remote_host, virt_srv, mymac = create_lab_host_vm(definition, config, defaults, host_name)
    bridge = config.get("NETWORK", "bridge=br0").split("=", 1)[-1]
    lab_host_ip = discover_lab_host_ip(host_name, remote_host, virt_srv, mymac, bridge)
    log("Lab-host VM reachable at {}".format(lab_host_ip))
    # The lab-host VM gets a DHCP address, which can belong to another VM's old known_hosts entry. The entry is purged before the
    # first ssh_run(), as purge_known_host() does for other nodes.
    purge_known_host(lab_host_ip, host_name)
    check_ssh_conn(lab_host_ip)

    bootstrap_lab_host_vm(lab_host_ip, defaults)
    deploy_lab_on_nested_automation(definition, host_name)

    pubkey = ensure_lab_ssh_key(_NESTED_AUTOMATION_IP)
    remapped = lab_usb.remap_lab_definition_to_nat(
        definition, nat_cidr=_NAT_NETWORK_CIDR, nested_automation_ip=_NESTED_AUTOMATION_IP)
    target_ips = [n.get("myip") for n in remapped.get("nodes", {}).values() if n.get("myip")]
    distribute_lab_ssh_key(_NESTED_AUTOMATION_IP, pubkey, target_ips)

    configure_handoff(definition)

    log("Shutting down the lab-host VM")
    run_libvirt_tool("virsh", remote_host, virt_srv, ["shutdown", host_name], check=False)
    for _ in range(60):
        result = run_libvirt_tool("virsh", remote_host, virt_srv, ["domstate", host_name],
                                   capture_output=True, text=True, check=False)
        if "shut off" in (result.stdout or ""):
            break
        time.sleep(5)
    else:
        die("lab-host VM did not shut down cleanly within 5 minutes")

    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    image_path = "{}/{}.raw".format(vm_img_loc, host_name)
    log("Lab-host VM shut down. Raw image ready at {}:{}".format(remote_host, image_path))

    if build_only:
        print(image_path if not remote_host else "{}:{}".format(remote_host, image_path))
        return

    die("Real USB device write is not yet implemented — pass --build-only "
        "and copy {}:{} manually for now".format(remote_host, image_path))


if __name__ == "__main__":
    main()
