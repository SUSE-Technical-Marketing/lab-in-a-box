#!/usr/bin/env python3
# Part of lab-in-a-box — bring a lab's records back in line with what its providers report
# Author/s: Raul Mahiques
# License: GPLv3
"""
reconcile_lab.py: make a lab's own records agree with the addresses its providers report now.

A cloud provider that releases an instance's public address when it stops hands back a different one when it
starts. Every A record of the lab, its hosts-file lines and the resolver inside each guest then name an address
that answers nothing. Starting the lab with vm_power.py already fixes that; a restart from the provider's own
console, or an instance that crashed and came back by itself, does not. This is the command for that case.

    reconcile_lab.py <lab-file>              every cloud node of the lab
    reconcile_lab.py <lab-file> <vm>...      only these nodes
    reconcile_lab.py --dry-run <lab-file>    report what would change, change nothing
    reconcile_lab.py --all <lab-file>        register every node again, not only the ones that moved

Options:
    --all                   do not compare first: register every cloud node of the lab again. Use it after adding
                            static_ip to a definition, to reserve the addresses of nodes that already exist, and
                            when a guest's own resolver configuration was lost rather than its address.
    --cloud-account <name>  use cloud account <name> for every cloud node of this run, taking precedence over
                            "cloud_account" in the definition.
    --dry-run               print the differences and exit without touching anything.

Prints one line per node whose address moved, "<node>: <recorded> -> <current>", and nothing when the lab is
already current. The exit code is 1 when a node could not be brought current, 0 otherwise, so a scheduled run
reports a real failure.

Only cloud nodes are looked at: a libvirt or Harvester VM answers at the address its lab definition gives it.
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import migrations  # noqa: E402
import primary  # noqa: E402
import reconcile  # noqa: E402
from lab_creation import _GREEN, _RED, _RESET, die  # noqa: E402

from setup_vm import refresh_kept_node_dns  # noqa: E402


def main():
    args = sys.argv[1:]
    if primary.wants_help(args) or not args:
        print(__doc__.strip())
        return 0
    if args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        return 0

    args = backends.take_cloud_account_option(args)
    dry_run = "--dry-run" in args
    every = "--all" in args
    args = [a for a in args if a not in ("--dry-run", "--all")]
    if not args:
        die("no lab definition given — reconcile_lab.py <lab-file> [vm ...]")

    lab_file, vm_names = args[0], args[1:]
    definition = primary.load_definition(lab_file)
    # Same as every other entrypoint: an old-shape definition is brought current before it is used, not refused.
    migrations.ensure_current(definition, lab_file)
    config = primary.load_config()

    unknown = [vm for vm in vm_names if vm not in (definition.get("nodes") or {})]
    if unknown:
        die("not a node of this lab: {}".format(", ".join(unknown)))

    if dry_run:
        report = {vm: "{} -> {}".format(recorded or "no record", actual)
                  for vm, (recorded, actual) in reconcile.drift(definition, config, vm_names or None).items()}
    else:
        report = reconcile.reconcile(definition, config, refresh_kept_node_dns,
                                     vm_names=vm_names or None, always=every)
    if not report:
        print("{}Every cloud node of '{}' is recorded at the address its provider reports.{}".format(
            _GREEN, lab_file, _RESET))
        return 0

    failed = False
    for vm_name in sorted(report):
        note = report[vm_name]
        if "(not applied:" in note:
            failed = True
            print("{}{}: {}{}".format(_RED, vm_name, note, _RESET))
        else:
            print("{}: {}{}".format(vm_name, note, "" if not dry_run else "  (not applied: --dry-run)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
