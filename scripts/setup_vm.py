#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will setup a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/setup_vm.sh — calls the python libraries
# (lab_creation, k8s, primary) directly, in-process. No bash is sourced or
# executed by this script.

"""
setup_vm.py — provision a single VM from a lab definition.

Usage:
    setup_vm.py <lab.json> <vm_hostname>
"""

__version__ = "ca2d2d5"

import sys
from pathlib import Path

# Installed location (mirrors bash's _lib_path=/usr/local/lib/lab_creation);
# fall back to the repo copy for local development.
for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
from lab_creation import (  # noqa: E402
    die, log, warn,
    validate_lab_definition, load_vm_vars,
    prepare_ignition_combustion, prepare_cloud_init,
    prepare_virt_customize_for_vm, prepare_install_iso,
    add_to_dns, clean_ssh_keys, check_ssh_conn,
)
from targets import is_existing_node  # noqa: E402
import backends  # noqa: E402
import overlay  # noqa: E402
from destroy_vm import destroy_vm  # noqa: E402


def provision_vm(definition, config, defaults, vm_name):
    """
    Provision one VM. Mirrors setup_vm.sh end to end.

    definition : the loaded lab definition — a primary.LabDefinition (see
                 primary.py), so it already knows its own source path and
                 format. Nothing here needs a separate path argument:
                 validate_lab_definition() reads definition.source_path for
                 its banner, and check_or_generate_mac() reads it too (via
                 primary.save_definition()) if a MAC conflict needs to be
                 persisted — neither re-reads the file itself.
    config     : lab_creation.cfg dict (REMOTE_HOST, VIRT_SRV, KVM_HOSTS, ROOT_SSH_KEY, …).
    defaults   : lab_creation.defaults dict (LAB_SETUP_PATH, VM_IMG_LOC, ISO_LOC, …).
    vm_name    : the node key in definition["nodes"] to provision.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    if is_existing_node(node_cfg):
        die("'{}' is marked \"existing\" — it is a pre-provisioned host and must not "
            "be provisioned by setup_vm.py".format(vm_name))

    iso_loc        = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")
    vm_img_loc     = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")

    # New VM placement — resolves whichever backend this VM uses (libvirt by
    # default; see backends.get_backend()'s docstring for the selection
    # precedence and each backend's own resolve() for how it finds its
    # target: a KVM host for libvirt, a kubeconfig/cluster for harvester).
    backend = backends.get_backend(definition, config, vm_name, for_existing=False,
                                    vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)

    if not validate_lab_definition(definition, config, iso_loc, lab_setup_path,
                                    target_node=vm_name, vm_img_loc=vm_img_loc):
        sys.exit(1)

    # bash: defaults, then cfg, then JSON (load_vm_vars) last — JSON wins on
    # any name collision, since it's sourced/exported last in that pipeline.
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(load_vm_vars(definition, vm_name))

    ign_file = "{}.ign".format(vm_name)
    com_file = vm_name

    mymac, network = backend.check_or_generate_mac(
        vm_name, env.get("mymac", ""), definition,
        bridge=env.get("BRIDGE", "br0"),
        vm_net_model=env.get("VM_NET_MODEL", "virtio"),
    )
    env["mymac"] = mymac

    config_method = env.get("config_method", "") or ""

    backend.copy_vm_image(env.get("ISO_IMAGE", ""), vm_name,
                           env.get("VM_DSK", ""), config_method=config_method)

    if config_method == "":
        prepare_ignition_combustion(
            vm_name, lab_setup_path,
            env.get("ROOT_PWD_HASH", ""), env.get("ROOT_SSH_KEY", ""),
            env.get("mysource", ""), env.get("sourcepath", ""),
            env.get("mydns", ""), env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("SUSE_email", ""), env.get("SUSE_regcode", ""), env.get("SUSE_url", ""),
        )
    elif config_method == "cloud-init":
        prepare_cloud_init(vm_name, lab_setup_path, env)
    elif config_method == "virt_customize":
        # virt_customize is a libvirt-only config_method — HarvesterBackend's
        # copy_vm_image() (already called above) already died on any
        # non-cloud-init config_method for that backend, so backend.remote_host
        # is only ever reached here for a genuine LibvirtBackend.
        prepare_virt_customize_for_vm(
            backend.remote_host, vm_img_loc, vm_name,
            env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("mydns", ""), env.get("mydomain", ""), mymac,
            vm_root_pass=env.get("VM_ROOT_PASS"), root_pwd_hash=env.get("ROOT_PWD_HASH"),
            root_ssh_key_path=env.get("ROOT_SSH_KEY"),
        )
    elif config_method == "install_iso":
        prepare_install_iso(
            vm_name, lab_setup_path, env.get("install_type", ""), env.get("ISO_IMAGE", ""),
            mymac, env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("mydns", ""), env.get("mydomain", ""),
            env.get("ROOT_PWD_HASH", ""), root_ssh_key=env.get("ROOT_SSH_KEY"),
        )
    # else: "iso-cloud-init" — nothing to prepare (mirrors bash: no prepare_* branch for it)

    backend.push_provisioning_files(vm_name, config_method=config_method, vm_img_loc=vm_img_loc)

    # DNS registration moved to AFTER create_vm(), 2026-09-09 (found live-testing AWSBackend — see
    # TODO): a cloud backend's real IP is only known once the provider assigns it, not from the
    # lab JSON's own (for a cloud node, deliberately empty — see README) `myip` field the way
    # libvirt/Harvester's is. create_vm()'s return value (see VMBackend.create_vm()'s own
    # docstring) reports that real IP for cloud backends; libvirt/Harvester return None and env's
    # already-known static myip is used unchanged, exactly as before this change.
    created_ip = backend.create_vm(
        vm_name,
        env.get("VM_CPU", ""), env.get("VM_MEM", ""), env.get("VM_DSK", ""),
        network,
        os_variant=env.get("VM_OSVARIANT", "slem5.4"),
        boot=env.get("VM_BOOT", "uefi"),
        config_method=config_method,
        extra_disks=env.get("extra_dsk", "").split() or None,
        extra_filesystems=env.get("extra_fs", "").split() or None,
        vm_dsk_bus=env.get("VM_DSK_BUS", "virtio"),
        ign_file=ign_file, com_file=com_file,
        salt_states=env.get("salt_states", ""),
        install_type=env.get("install_type", ""), iso_image=env.get("ISO_IMAGE", ""),
        iso_loc=iso_loc, mydns=env.get("mydns", ""),
        vcluster=env.get("vcluster", ""),
        mymac=mymac,
        vm_machine=env.get("VM_MACHINE", ""),
        # cloud_instance_type: an explicit per-node/common lab-JSON override for a cloud
        # backend's instance type/server type/plan/flavor — added 2026-09-10 per explicit user
        # request that no provider's sizing catalog be a hardcoded ceiling. Ignored by
        # libvirt/Harvester (absorbed by their own **kwargs, same as every other cloud-only
        # kwarg here). See README's Compute backends table.
        cloud_instance_type=env.get("cloud_instance_type", ""),
        # aws_open_ports: an explicit per-node/common lab-JSON list of extra ports
        # (e.g. ["443", "4505", "4506"], or "69/udp" for non-tcp) AWSBackend.create_vm()
        # opens on the security group, in addition to always opening SSH from this
        # automation node's own IP — added 2026-09-13, see AWSBackend._ensure_
        # security_group_access()'s own docstring for the real bug this fixes.
        # Ignored by every other backend (absorbed by their own **kwargs).
        open_ports=env.get("aws_open_ports") or [],
    )
    if created_ip:
        env["myip"] = created_ip

    # Honours a per-node/common "cloud_account" (its cloudtype), same as get_backend() above —
    # so a multi-account cloud lab still routes through the cloud-DNS-VM path below.
    backend_name = backends.effective_backend_name(definition, config, vm_name)

    remote_dns_servers = env.get("REMOTE_DNS_SERVERS", "").split()
    if backend_name in backends.CLOUD_BACKEND_NAMES:
        # A cloud node generally can't reach automation.mydemo.lab's own BIND (behind the home
        # lab's NAT) — a real multi-node cloud cluster needs a DNS server living inside that same
        # cloud network to resolve its own nodes. See ensure_cloud_dns_vm()'s own docstring for
        # exactly what this does and does not yet cover (nodes don't yet point their own
        # resolution at it — a known, separately tracked follow-up, not silently glossed over).
        dns_vm_ip = backends.ensure_cloud_dns_vm(
            backend, backend_name, Path("/root/.ssh/id_rsa.pub").read_text().strip(),
            env.get("mydomain", ""), env.get("ISO_IMAGE", ""), lab_setup_path,
        )
        remote_dns_servers.append(dns_vm_ip)

    if env.get("myip"):
        add_to_dns(vm_name, env["myip"], env.get("mydomain", ""), env.get("mynet_reverse", ""),
                   remote_dns_servers=remote_dns_servers or None)
    else:
        warn("- No myip known for \"{}\" — backend never reported one — skipping DNS registration".format(vm_name))

    clean_ssh_keys(vm_name, env.get("myip", ""))

    # Wait for it to come online, reboot, then wait again — matches bash
    # exactly. check_ssh_conn() already dies internally on timeout (mirrors
    # fail_with_error inside bash's check_ssh_conn), so — as in bash — the
    # "VM failed to come online" messages there are effectively unreachable;
    # any real timeout aborts from inside check_ssh_conn itself.
    check_ssh_conn(vm_name)
    backend.reboot_vm(vm_name)
    check_ssh_conn(vm_name)

    # Cross-cloud WireGuard overlay (see libs/overlay.py) — opt-in via
    # common.overlay/OVERLAY_ENABLED, 2026-09-18. Joins this node to the
    # overlay AFTER it's confirmed reachable over SSH (the check_ssh_conn()
    # calls just above), same as every other post-boot provisioning step
    # here. The hub itself is either an operator-designated existing host
    # (OVERLAY_HUB_HOST) or a dedicated cloud VM auto-created/reused in
    # OVERLAY_HUB_ACCOUNT — see overlay.ensure_overlay_hub()'s own
    # docstring for why it needs its own account, independent of this
    # node's. OVERLAY_HUB_ACCOUNT may ALSO be set alongside OVERLAY_HUB_HOST
    # (added 2026-09-18, live-testing found the gap): an existing host still
    # needs its cloud firewall/security-group opened for the WireGuard port
    # — HUB_HOST alone never touches the cloud API at all (that's the
    # point, for a host on a backend this tool has no account for), so
    # naming the account too is what makes that port-opening automatic
    # instead of a manual step.
    overlay_enabled = str(env.get("overlay") or env.get("OVERLAY_ENABLED") or "").strip().lower() in (
        "1", "true", "yes")
    if overlay_enabled:
        overlay_cidr = env.get("OVERLAY_CIDR") or overlay.DEFAULT_OVERLAY_CIDR
        wg_port = int(env.get("OVERLAY_WG_PORT") or overlay.DEFAULT_WG_PORT)
        hub_host = env.get("OVERLAY_HUB_HOST")
        hub_account = env.get("OVERLAY_HUB_ACCOUNT")
        if hub_host:
            if hub_account:
                hub_backend, _hub_backend_name = backends.get_backend_for_account(
                    hub_account, config, vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)
                hub_backend.ensure_ports_open(["{}/udp".format(wg_port)])
            hub_overlay_ip, hub_pubkey = overlay.ensure_overlay_hub_ready(
                hub_host, wg_port=wg_port, overlay_cidr=overlay_cidr)
        else:
            if not hub_account:
                die("overlay is enabled (\"overlay\": true) but neither OVERLAY_HUB_HOST nor "
                    "OVERLAY_HUB_ACCOUNT is set in lab_creation.cfg — the overlay hub needs one "
                    "or the other to know where to run")
            hub_backend, hub_backend_name = backends.get_backend_for_account(
                hub_account, config, vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)
            hub_host, hub_overlay_ip, hub_pubkey = overlay.ensure_overlay_hub(
                hub_backend, hub_backend_name, env.get("ISO_IMAGE", ""), lab_setup_path,
                wg_port=wg_port, overlay_cidr=overlay_cidr)
        overlay.ensure_overlay_spoke(vm_name, vm_name, hub_host, hub_pubkey, wg_port,
                                      overlay_cidr=overlay_cidr)

    log("\t\tVM \"{}\" created".format(vm_name))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print(
            "Usage: setup_vm.py <lab.json> <vm_hostname>\n\n"
            "Provisions a single VM: copies the disk image, generates provisioning files\n"
            "(Ignition+Combustion or cloud-init), registers DNS, and calls virt-install.\n\n"
            "Run 'setup_lab.py --input-definition [json|yaml]' for the full lab definition schema."
        )
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] in ("--input-definition", "--schema"):
        import subprocess
        fmt = sys.argv[2] if len(sys.argv) > 2 else "json"
        sys.exit(subprocess.run(["setup_lab.py", "--input-definition", fmt]).returncode)

    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if len(sys.argv) < 3:
        die("Usage:\n{} <configuration file> <vm_name>".format(sys.argv[0]))

    json_file = sys.argv[1]
    vm_name = sys.argv[2]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    # Destroy-before-recreate: setup_lab.py's own orchestration already does this
    # (destroy_vm() unconditionally before provision_vm(), for every node, unless
    # --keep says it's reusable) — this standalone single-VM entrypoint never did,
    # forcing a manual `destroy_vm.py` call before every retry or it would die with
    # "Disk ... is already in use by other guests"/"Domain already exists" instead
    # of just doing the right thing. Mirrors setup_lab.py's own try/except shape:
    # destroy_vm() is already safe to call unconditionally (backend.delete_vm()
    # itself no-ops if the VM doesn't exist, and warns+returns for an "existing"
    # pre-provisioned node rather than touching it) — the only real difference here
    # is that a genuine destroy failure should abort outright (die()), since unlike
    # setup_lab.py's multi-node loop there is no "next node" to continue on to.
    try:
        destroy_vm(definition, config, defaults, vm_name)
    except SystemExit:
        pass  # "existing" node refusal, or nothing to destroy on a first run
    except RuntimeError as e:
        die("destroy before recreate failed for '{}': {}".format(vm_name, e))

    provision_vm(definition, config, defaults, vm_name)


if __name__ == "__main__":
    main()
