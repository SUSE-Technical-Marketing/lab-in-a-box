#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will destroy a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/destroy_vm.sh — calls the python libraries
# directly, in-process. No bash is sourced or executed by this script.

"""
destroy_vm.py — destroy a single VM from a lab definition.

Usage:
    destroy_vm.py <lab.json> <vm_hostname>
"""

__version__ = "ca2d2d5"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
from lab_creation import load_vm_vars, del_from_dns, warn  # noqa: E402
from targets import is_existing_node  # noqa: E402
import backends  # noqa: E402
import overlay  # noqa: E402


def destroy_vm(definition, config, defaults, vm_name):
    """Destroy one VM: remove its DNS entries, then delete it. Mirrors destroy_vm.sh.

    Resolves via get_backend(..., for_existing=True) — this acts on a VM
    that may already exist, so it must find whichever host/cluster actually
    has it rather than resource-select a fresh one (see each backend's own
    resolve() for how — locate_kvm_host() for libvirt, a fixed kubeconfig
    for harvester).

    Existing (pre-provisioned) nodes are never destroyed — this tool never
    created them, so it has nothing to delete_vm() and no business removing
    their DNS entries either.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    if is_existing_node(node_cfg):
        warn("'{}' is marked \"existing\" — refusing to destroy a pre-provisioned host".format(vm_name))
        return

    backend = backends.get_backend(definition, config, vm_name, for_existing=True)
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(load_vm_vars(definition, vm_name))

    # A cloud node's lab JSON deliberately has no myip (see README) — its real IP is only ever
    # known live, from the backend itself. Must be resolved HERE, before delete_vm() below removes
    # the instance and makes get_ip() unable to find it — otherwise del_from_dns() would try to
    # remove a record built from an empty IP, matching nothing, leaving the real entry (registered
    # with the real IP at create time) permanently orphaned in both the local zone and the cloud
    # DNS VM. Found alongside create_vm()'s own real-IP return contract, 2026-09-09 — see TODO.
    # Honours a per-node/common "cloud_account" (its cloudtype), same as get_backend().
    backend_name = backends.effective_backend_name(definition, config, vm_name)
    myip = env.get("myip", "")
    remote_dns_servers = env.get("REMOTE_DNS_SERVERS", "").split()
    if backend_name in backends.CLOUD_BACKEND_NAMES:
        if not myip:
            myip = backend.get_ip(vm_name) or ""
        # Best-effort: the DNS VM itself (see ensure_cloud_dns_vm()) is a shared, persistent
        # resource, never created here — only looked up, and skipped if it doesn't exist (nothing
        # to clean an entry off of). Per-account name when this node uses a named cloud_account.
        _acct = getattr(backend, "account", "") or ""
        dns_vm_name = ("lab-dns-{}".format(backend_name) if _acct in ("", "default")
                       else "lab-dns-{}-{}".format(backend_name, _acct))
        if backend.vm_exists(dns_vm_name):
            dns_vm_ip = backend.get_ip(dns_vm_name)
            if dns_vm_ip:
                remote_dns_servers.append(dns_vm_ip)

    if myip:
        del_from_dns(
            vm_name, myip, env.get("mydomain", ""), env.get("mynet_reverse", ""),
            remote_dns_servers=remote_dns_servers or None,
        )
    else:
        warn("- No myip known for \"{}\" — skipping DNS cleanup".format(vm_name))

    # Cross-cloud WireGuard overlay cleanup — best-effort, mirrors the DNS-VM
    # lookup-only convention just above: the hub is shared/persistent and
    # never destroyed here, only looked up (so a lab that never used the
    # overlay, or whose hub is already gone, is a silent no-op). See
    # libs/overlay.py's remove_overlay_spoke()/remove_peer_from_hub() —
    # neither ever contacts the node itself, only the hub's own recorded
    # peer state, so this is safe even after delete_vm() below.
    overlay_enabled = str(env.get("overlay") or env.get("OVERLAY_ENABLED") or "").strip().lower() in (
        "1", "true", "yes")
    if overlay_enabled:
        hub_host = env.get("OVERLAY_HUB_HOST")
        if not hub_host:
            hub_account = env.get("OVERLAY_HUB_ACCOUNT")
            if hub_account:
                hub_backend, hub_backend_name = backends.get_backend_for_account(hub_account, config)
                _hub_acct = getattr(hub_backend, "account", "") or ""
                hub_vm_name = ("lab-overlay-hub-{}".format(hub_backend_name) if _hub_acct in ("", "default")
                               else "lab-overlay-hub-{}-{}".format(hub_backend_name, _hub_acct))
                if hub_backend.vm_exists(hub_vm_name):
                    hub_host = hub_backend.get_ip(hub_vm_name)
        if hub_host:
            overlay.remove_overlay_spoke(hub_host, vm_name)

    backend.delete_vm(vm_name)
    print('#\t\tVM "{}" destroyed\n'.format(vm_name))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Usage:\n{} <configuration file> <vm_name>".format(sys.argv[0]))
        sys.exit(1)

    json_file = sys.argv[1]
    vm_name = sys.argv[2]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    destroy_vm(definition, config, defaults, vm_name)


if __name__ == "__main__":
    main()
