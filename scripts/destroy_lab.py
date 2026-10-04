#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will destroy all VMs defined in a lab JSON file
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/destroy_lab.sh — calls the python libraries
# directly, in-process. No bash is sourced or executed by this script.

"""
destroy_lab.py — destroy every VM defined in a lab definition.

Usage:
    destroy_lab.py <lab.json>
"""

__version__ = "6be996d"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation  # noqa: E402
from destroy_vm import destroy_vm  # noqa: E402


def destroy_lab(definition, config, defaults, json_file):
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage:\n{} <configuration file>".format(sys.argv[0]))
        sys.exit(1)

    json_file = sys.argv[1]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    destroy_lab(definition, config, defaults, json_file)


if __name__ == "__main__":
    main()
