#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will destroy all VMs defined in a lab definition file
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/destroy_lab.sh — calls the python libraries
# directly, in-process. No bash is sourced or executed by this script.

"""
destroy_lab.py — destroy every VM defined in a lab definition.

Usage:
    destroy_lab.py [--cloud-account <name>] <lab.json>
    destroy_lab.py [--cloud-account <name>] --example <name>

Options:
    --cloud-account <name>   use cloud account <name> (a credentials file under
                             /etc/lab_creation/credentials/) for every cloud node of this
                             run, taking precedence over "cloud_account" in the lab
                             definition. Required when several credentials files match a
                             node's backend.
    --example <name>         destroy the lab built by setup_lab.py --example <name>
                             (setup_lab.py --list-examples names them), for which no lab
                             file of your own exists. The definition is written to a
                             temporary file for this run and removed afterwards.
    --with-infra             also destroy the supporting VMs this lab's cloud sites brought
                             up for themselves, the DNS VM and the overlay gateway, which
                             are shared by every lab on the same cloud account and are
                             otherwise left running. Each one is destroyed only when nothing
                             else is using it: the shared VMs of a site whose DNS is served
                             by this automation node, a site another recorded lab also uses,
                             or a DNS server still holding another lab's zone are reported
                             and kept. Same flag, same meaning as vm_power.py --with-infra.
"""

__version__ = "__LABVERSION__"

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation  # noqa: E402
import lab_examples  # noqa: E402
import backends  # noqa: E402
import lab_registry  # noqa: E402
import dns_zone  # noqa: E402
import overlay  # noqa: E402
import ipalloc  # noqa: E402
import services  # noqa: E402
from lab_creation import load_vm_vars  # noqa: E402
from destroy_vm import destroy_vm  # noqa: E402


def forget_overlay_dns(definition, config, defaults):
    """
    Take this lab's lines off the overlay hub's DNS registry, one per (site, domain) the lab used.

    The registry holds one line per lab per site (overlay.register_dns_server()), so a destroyed lab's line is no
    longer replaced by the next lab to run there: left behind, it would have every later reconciliation try to
    reach that server for a zone nobody holds. Does nothing for a lab with no overlay, and nothing at all when the
    hub cannot be found without creating one (overlay.existing_hub_host()).
    """
    env = dict(defaults)
    env.update(config)
    env.update((definition.get("common") or {}))
    if str(env.get("overlay") or env.get("OVERLAY_ENABLED") or "").strip().lower() not in ("1", "true", "yes"):
        return
    hub_host = overlay.existing_hub_host(config, lab_setup_path=defaults.get("LAB_SETUP_PATH"))
    if not hub_host:
        return
    # Per site, because a lab's nodes can sit on different sites and its kclusters can name different domains.
    for site_name, (_vm_name, _account, zones) in sorted(lab_sites(definition, config, defaults).items()):
        for mydomain in sorted(zones):
            if not mydomain.endswith(".in-addr.arpa"):
                overlay.forget_dns_server(hub_host, site_name, mydomain)


def lab_sites(definition, config, defaults):
    """
    Every site this lab's nodes sit on, as {site name: (one node of it, its mydomain)}.

    One node per site is enough for everything below: a backend is resolved per node, and every node of a site
    resolves the same one. The zone names are this lab's own on that site's DNS server, which is what decides
    whether a zone found there belongs to another lab.
    """
    env = {}
    env.update(defaults)
    env.update(config)
    sites = {}
    for vm_name in (definition.get("nodes") or {}):
        node_env = dict(env)
        node_env.update(load_vm_vars(definition, vm_name))
        backend_name = backends.effective_backend_name(definition, config, vm_name)
        account, _cfg, _cloudtype = backends.resolve_cloud_account(definition, config, vm_name)
        mydomain = node_env.get("mydomain", "")
        mynet_reverse = node_env.get("mynet_reverse", "")
        zones = {mydomain} if mydomain else set()
        if mydomain and mynet_reverse:
            zones.add(dns_zone.reverse_zone(mynet_reverse))
        sites.setdefault(overlay.site_name_for(backend_name, account), (vm_name, account, zones))
    return sites


def other_labs_on_site(lab_file, site_name, config, defaults):
    """
    The names of other recorded labs whose own nodes sit on `site_name` (libs/lab_registry.py).

    Each definition is read rather than trusting a copy of its sites: the registry holds the definition's path for
    exactly this reason, and a definition the operator has edited since is still the truth about where that lab is.
    A lab whose definition has gone is counted as using the site, because it cannot be shown not to.
    """
    this_lab = lab_registry.lab_name(lab_file)
    found = []
    for entry in lab_registry.entries(config):
        if entry.get("lab") == this_lab:
            continue
        path = entry.get("definition", "")
        if not Path(path).is_file():
            found.append("{} (its definition is gone, so it cannot be ruled out)".format(entry.get("lab")))
            continue
        try:
            other = primary.load_definition(path)
        except SystemExit:
            found.append("{} (its definition cannot be read, so it cannot be ruled out)".format(entry.get("lab")))
            continue
        if site_name in lab_sites(other, config, defaults):
            found.append(entry.get("lab", "?"))
    return found


def destroy_site_infra(definition, config, defaults, lab_file):
    """
    Destroy the shared VMs of each of this lab's cloud sites, the DNS VM and the overlay gateway, where nothing
    else is still using them. What --with-infra does, after the lab's own nodes are gone.

    Three gates, each of which reports and keeps rather than destroys:

      1. the site's DNS server is this automation node itself, so there is no VM of its own to destroy. That is
         every libvirt and Harvester site, and a cloud lab whose automation node serves its own DNS.
      2. another recorded lab's nodes sit on the same site, so both shared VMs are still in use.
      3. the DNS server still declares a zone that is not this lab's, which is the only gate that can see a lab
         this automation node never deployed: the server is per cloud account and a registry is per automation
         node. The overlay gateway has no equivalent and rests on gates 1 and 2.

    The overlay hub is never touched: it serves every account, not this lab's sites, and retiring it is
    overlay_ctl.py's business.

    Destroyed with backend.delete_vm(), which is the only caller of release_stable_address(): these VMs reserve
    their addresses unconditionally (backends.ensure_infra_address()), and a reserved address is billed whether or
    not anything uses it, so any other path would leak one per account.
    """
    local = ipalloc.local_ips()
    hub_account = (config or {}).get("OVERLAY_HUB_ACCOUNT") or ""
    for site_name, (vm_name, account, zones) in sorted(lab_sites(definition, config, defaults).items()):
        backend_name = backends.effective_backend_name(definition, config, vm_name)
        if backend_name not in backends.CLOUD_BACKEND_NAMES:
            lab_creation.log("- Site '{}' is served by this automation node: it has no shared VM of its own to "
                             "destroy".format(site_name))
            continue
        backend = backends.get_backend(definition, config, vm_name, for_existing=True)
        dns_vm = backends.cloud_dns_vm_name(backend, backend_name)
        dns_ip = backend.get_ip(dns_vm) if backend.vm_exists(dns_vm) else ""
        if dns_ip and dns_ip in local:
            lab_creation.warn("- Site '{}': its DNS server is this automation node, which is NOT destroyed"
                              .format(site_name))
            continue
        others = other_labs_on_site(lab_file, site_name, config, defaults)
        if others:
            lab_creation.warn("- Site '{}': kept, because {} also use(s) it: {}".format(
                site_name, "another lab" if len(others) == 1 else "other labs", ", ".join(sorted(others))))
            continue
        if dns_ip:
            # One call: each one is an SSH round trip to that server.
            foreign = services.DNSService().declared_zones(dns_ip) - zones
            if foreign:
                lab_creation.warn("- Site '{}': its DNS server '{}' still holds zone(s) of another lab, so it is "
                                  "NOT destroyed: {}".format(site_name, dns_vm, ", ".join(sorted(foreign))))
                continue
            lab_creation.log("- Destroying this site's DNS VM '{}' ({})".format(dns_vm, dns_ip))
            backend.delete_vm(dns_vm)
        # hub_vm_name() names a site's gateway AND the hub itself, which differ only by the account they live in.
        # The hub serves every account rather than this lab's site, so retiring it is overlay_ctl.py's business.
        if account and account == hub_account:
            lab_creation.log("- Site '{}' is the overlay hub's own account: the hub is left running (retire it "
                             "with overlay_ctl.py)".format(site_name))
            continue
        gateway = overlay.hub_vm_name(backend_name, getattr(backend, "account", ""))
        if backend.vm_exists(gateway):
            lab_creation.log("- Destroying this site's overlay gateway '{}'".format(gateway))
            backend.delete_vm(gateway)


def destroy_lab(definition, config, defaults, lab_file, with_infra=False):
    lab_name = definition.get("common", {}).get("lab_name", "")
    lab_creation.log('Destroy lab "{}{}{}"'.format(lab_creation._RED, lab_name, lab_creation._RESET))
    lab_creation._level += 1
    for vm_name in definition.get("nodes", {}):
        lab_creation.log("Node: {}{}{}".format(lab_creation._RED, vm_name, lab_creation._RESET))
        lab_creation.purge_known_host(vm_name)
        # bash's `destroy_vm.sh` calls have no `||` error check anywhere —
        # one VM's destroy failing must never block tearing down the rest of
        # the lab. Matches the same wrapper in setup_lab.py's
        # phase_create_vms (there for the same reason, plus the existing
        # first-run "VM never existed" case).
        try:
            destroy_vm(definition, config, defaults, vm_name)
        except SystemExit:
            pass
        except RuntimeError as e:
            lab_creation.warn("destroy failed for '{}' (continuing): {}".format(vm_name, e))
    if with_infra:
        destroy_site_infra(definition, config, defaults, lab_file)
    lab_creation._level -= 1
    forget_overlay_dns(definition, config, defaults)
    # Off the registry once its nodes are gone, so a scheduled run stops walking it and the event log carries the
    # teardown. A node whose destroy failed above is left to the scheduled check, which drops it when the provider
    # agrees it is gone rather than on the strength of this run having tried.
    lab_registry.forget(lab_file, config)


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

    with_infra = "--with-infra" in sys.argv[1:]
    sys.argv[1:] = [a for a in sys.argv[1:] if a != "--with-infra"]

    try:
        sys.argv[1:], example = lab_examples.take_example_option(sys.argv[1:])
    except ValueError as e:
        lab_creation.die(str(e))

    if example:
        # The same definition setup_lab.py --example built from: an example is a fixed file, and an address it did
        # not name is allocated from the node's own name, so what is written here is what was created.
        example_dir = tempfile.mkdtemp(prefix="lab-example-")
        atexit.register(shutil.rmtree, example_dir, True)
        try:
            lab_file = lab_examples.materialise(example, directory=example_dir)
        except (ValueError, FileNotFoundError) as e:
            lab_creation.die("{} — setup_lab.py --list-examples lists the example labs".format(e))
    elif len(sys.argv) < 2:
        print("Usage:\n{} <configuration file>".format(sys.argv[0]))
        sys.exit(1)
    else:
        lab_file = sys.argv[1]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(lab_file)

    destroy_lab(definition, config, defaults, lab_file, with_infra=with_infra)


if __name__ == "__main__":
    main()
