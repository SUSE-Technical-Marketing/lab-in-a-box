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
    destroy_vm.py <lab-file> <vm_hostname>
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import ipalloc  # noqa: E402
import overlay  # noqa: E402
import primary  # noqa: E402
from lab_creation import load_vm_vars, del_from_dns, warn, log  # noqa: E402
from targets import is_existing_node  # noqa: E402
import backends  # noqa: E402


_HUB_HOST = None


def _overlay_hub_host(config, defaults):
    """The overlay hub's address, or "" when there is no hub to talk to. Looked up once per process.

    A whole-lab teardown calls destroy_vm() once per node and every one of them asks the same question of the
    same account, where the answer cannot change: the lookup resolves a backend and makes two provider calls
    (overlay.existing_hub_host()), so a twenty-node lab would spend forty of them on one unchanging address.
    """
    global _HUB_HOST
    if _HUB_HOST is None:
        _HUB_HOST = overlay.existing_hub_host(config, lab_setup_path=defaults.get("LAB_SETUP_PATH"))
    return _HUB_HOST


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
    # DNS VM. create_vm() returns the real IP, so the entry matched here is the one it created.
    # Honours a per-node/common "cloud_account" (its cloudtype), same as get_backend().
    backend_name = backends.effective_backend_name(definition, config, vm_name)
    myip = env.get("myip", "")
    remote_dns_servers = env.get("REMOTE_DNS_SERVERS", "").split()
    if not myip and backend_name not in backends.CLOUD_BACKEND_NAMES:
        # A lab may leave myip out and let the run allocate one (libs/ipalloc.py). The address was registered in the
        # lab DNS before the VM was created, so the zone still knows it and the records can be removed; without this
        # they would be orphaned, since a libvirt host cannot be asked what address a VM was given.
        myip = ipalloc.dns_address(vm_name, env.get("mydomain", ""))
        if myip:
            log("- No myip in the lab for \"{}\" — the lab DNS has {}".format(vm_name, myip))
    if backend_name in backends.CLOUD_BACKEND_NAMES:
        if not myip:
            myip = backend.get_ip(vm_name) or ""
        # Best-effort: the DNS VM itself (see ensure_cloud_dns_vm()) is a shared, persistent
        # resource, never created here — only looked up, and skipped if it doesn't exist (nothing
        # to clean an entry off of). Per-account name when this node uses a named cloud_account.
        dns_vm_name = backends.cloud_dns_vm_name(backend, backend_name)
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

    # Cross-cloud WireGuard overlay (see libs/overlay.py). No peer entry is removed: an ordinary lab node is
    # never itself a WireGuard peer, only its site's shared gateway is, and the route this node held goes with
    # the node. Its line in the hub's node registry does have to go. setup_vm.py's overlay.register_node() put
    # it there so that a node could be told about a site that joined after it was built; left behind, every
    # later overlay.refresh_routes() keeps opening an SSH connection to a host that no longer exists, for as
    # long as the hub lives. Best effort, like the DNS removal above: no hub, or an unreachable one, is skipped.
    if str(env.get("overlay") or env.get("OVERLAY_ENABLED") or "").strip().lower() in ("1", "true", "yes"):
        hub_host = _overlay_hub_host(config, defaults)
        if hub_host:
            overlay.forget_node(hub_host, vm_name)

    backend.delete_vm(vm_name)
    print('#\t\tVM "{}" destroyed\n'.format(vm_name))


def main():
    if primary.wants_help(sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    # Applied and stripped before the positional arguments are read, so the option reads the same on every
    # command that creates or destroys a cloud node.
    sys.argv[1:] = backends.take_cloud_account_option(sys.argv[1:])

    if len(sys.argv) < 3:
        print("Usage:\n{} <configuration file> <vm_name>".format(sys.argv[0]))
        sys.exit(1)

    lab_file = sys.argv[1]
    vm_name = sys.argv[2]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(lab_file)

    destroy_vm(definition, config, defaults, vm_name)


if __name__ == "__main__":
    main()
