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
import overlay  # noqa: E402
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
    env = {}
    env.update(defaults)
    env.update(config)
    if str(env.get("overlay") or env.get("OVERLAY_ENABLED") or "").strip().lower() not in ("1", "true", "yes"):
        return
    hub_host = overlay.existing_hub_host(config, lab_setup_path=defaults.get("LAB_SETUP_PATH"))
    if not hub_host:
        return
    # Per node, because a lab's nodes can sit on different sites and its kclusters can name different domains.
    pairs = set()
    for vm_name in (definition.get("nodes") or {}):
        node_env = dict(env)
        node_env.update(load_vm_vars(definition, vm_name))
        mydomain = node_env.get("mydomain", "")
        if not mydomain:
            continue
        backend_name = backends.effective_backend_name(definition, config, vm_name)
        account, _cfg, _cloudtype = backends.resolve_cloud_account(definition, config, vm_name)
        pairs.add((overlay.site_name_for(backend_name, account), mydomain))
    for site_name, mydomain in sorted(pairs):
        overlay.forget_dns_server(hub_host, site_name, mydomain)


def destroy_lab(definition, config, defaults, lab_file):
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

    destroy_lab(definition, config, defaults, lab_file)


if __name__ == "__main__":
    main()
