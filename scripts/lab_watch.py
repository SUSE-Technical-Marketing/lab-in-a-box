#!/usr/bin/env python3
# Part of lab-in-a-box — keep the record of deployed labs, and their DNS, true to what the providers report
# Author/s: Raul Mahiques
# License: GPLv3
"""
lab_watch.py: check every lab this automation node has deployed, and bring its records back in line.

Run on a schedule (the installer ships a systemd timer, or a cron.d entry where there is no systemd). For each lab
in the registry (libs/lab_registry.py) it asks every node's own provider what is there now, and acts on the answer:

    the node is gone            somebody deleted it outside this tooling: it comes off the lab's entry and the
                                event log records it as vanished. A lab left with no nodes is forgotten entirely.
    the node is stopped         reported, and left alone: starting it is vm_power.py's job and a deliberate one.
    the node moved address      registered again at the address the provider reports now (libs/reconcile.py), so
                                every A record, hosts-file line and in-guest resolver names the live address.

    lab_watch.py                      every lab in the registry
    lab_watch.py --lab <name>         only this one
    lab_watch.py --dry-run            report what would change, change nothing
    lab_watch.py --list               print the registry and exit

Options:
    --dry-run       report and change nothing, neither the registry nor any DNS record.
    --lab <name>    limit the run to one lab, named as the registry holds it (its definition's base name).
    --list          print one line per recorded lab and exit.
    --quiet         print only what changed, for a scheduled run whose output is mail or a journal entry.

Exit code 1 when a node could not be brought current, 0 otherwise, so a scheduled run reports a real failure
rather than burying it.
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import lab_registry  # noqa: E402
import migrations  # noqa: E402
import primary  # noqa: E402
import reconcile  # noqa: E402
from lab_creation import _GREEN, _RED, _RESET, log, warn  # noqa: E402

from setup_vm import refresh_kept_node_dns  # noqa: E402


def node_states(definition, config, nodes):
    """
    What each of `nodes` is according to its own provider: {node: state}, where the state is the provider's own
    word for it ("running", "stopped", ...), "gone" for a node that no longer exists, and "unknown" for a backend
    that cannot say.

    A backend that raises is reported as "unknown" rather than taken as "gone": a credential that expired or an API
    that is down must never be read as "somebody deleted this lab".
    """
    states = {}
    for vm_name in nodes:
        try:
            backend = backends.get_backend(definition, config, vm_name, for_existing=True)
            if not backend.vm_exists(vm_name):
                states[vm_name] = "gone"
                continue
            try:
                states[vm_name] = backend.vm_state(vm_name) or "unknown"
            except NotImplementedError:
                states[vm_name] = "unknown"
        except (SystemExit, RuntimeError, OSError) as error:
            warn("- {}: its provider could not be asked, so it is left on the registry untouched: {}".format(
                vm_name, error))
            states[vm_name] = "unknown"
    return states


def check_lab(entry, config, dry_run=False, quiet=False) -> bool:
    """
    Check one registry entry: drop the nodes its provider no longer has, report the stopped ones, and reconcile the
    addresses of the rest. Returns whether everything it tried succeeded.
    """
    lab, definition_path = entry.get("lab", ""), entry.get("definition", "")
    if not Path(definition_path).is_file():
        warn("- lab '{}': its definition '{}' is gone, so its nodes cannot be checked. Destroy the lab, or put "
             "the file back".format(lab, definition_path))
        return False
    definition = primary.load_definition(definition_path)
    migrations.ensure_current(definition, definition_path)
    nodes = [n for n in (entry.get("nodes") or []) if n in (definition.get("nodes") or {})]

    states = node_states(definition, config, nodes)
    gone = sorted(name for name, state in states.items() if state == "gone")
    stopped = sorted(name for name, state in states.items()
                     if state not in backends.RUNNING_STATES and state not in ("gone", "unknown"))
    if gone:
        log("- lab '{}': {}{}{} no longer exist at their provider and were deleted outside this tooling: {}".format(
            lab, _RED, len(gone), _RESET, ", ".join(gone)))
        if not dry_run:
            lab_registry.drop_nodes(lab, gone, config, action="vanished")
    if stopped:
        log("- lab '{}': stopped, and left that way (vm_power.py start is the deliberate step): {}".format(
            lab, ", ".join("{} ({})".format(name, states[name]) for name in stopped)))

    # Only the nodes still there and still running: registering a stopped node would write the address it had
    # before it stopped, and one that is gone has no address at all.
    live = [name for name, state in states.items()
            if name not in gone and state not in ("unknown",) and state in backends.RUNNING_STATES]
    if not live:
        return True
    if dry_run:
        drift = reconcile.drift(definition, config, vm_names=live)
        for vm_name in sorted(drift):
            recorded, actual = drift[vm_name]
            log("- lab '{}': {} is recorded at {} and answers at {}".format(
                lab, vm_name, recorded or "no address", actual))
        return True
    report = reconcile.reconcile(definition, config, refresh_kept_node_dns, vm_names=live)
    failed = False
    for vm_name in sorted(report):
        note = report[vm_name]
        if "(not applied:" in note:
            failed = True
            warn("- lab '{}': {}: {}".format(lab, vm_name, note))
        else:
            log("- lab '{}': {} moved, {}, and was registered again".format(lab, vm_name, note))
    if not report and not quiet:
        log("- lab '{}': every node is recorded at the address its provider reports".format(lab))
    return not failed


def main():
    args = sys.argv[1:]
    if primary.wants_help(args):
        print(__doc__.strip())
        return 0
    if args[:1] in (["--version"], ["-v"]):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        return 0

    dry_run = "--dry-run" in args
    quiet = "--quiet" in args
    listing = "--list" in args
    only = ""
    if "--lab" in args:
        index = args.index("--lab")
        if index + 1 >= len(args):
            warn("--lab needs the name of a recorded lab")
            return 2
        only = args[index + 1]
        del args[index:index + 2]
    unknown = [a for a in args if a.startswith("-") and a not in ("--dry-run", "--quiet", "--list")]
    if unknown:
        warn("unknown option(s): {} — see lab_watch.py --help".format(" ".join(unknown)))
        return 2

    config = primary.load_config()
    recorded = lab_registry.entries(config)
    if only:
        recorded = [e for e in recorded if e.get("lab") == only]
        if not recorded:
            warn("no lab '{}' is recorded in {}".format(only, lab_registry.registry_dir(config)))
            return 2

    if listing:
        if not recorded:
            print("No lab is recorded in {}".format(lab_registry.registry_dir(config)))
            return 0
        for entry in recorded:
            print("{:<28} {:<16} {:>2} node(s)  updated {}  {}".format(
                entry.get("lab", "?"), entry.get("mydomain", "-"), len(entry.get("nodes") or []),
                entry.get("updated", "-"), entry.get("definition", "-")))
        return 0

    if not recorded:
        if not quiet:
            print("No lab is recorded in {}: nothing to check.".format(lab_registry.registry_dir(config)))
        return 0

    ok = True
    for entry in recorded:
        ok = check_lab(entry, config, dry_run=dry_run, quiet=quiet) and ok
    if ok and not quiet:
        print("{}Checked {} lab(s).{}{}".format(
            _GREEN, len(recorded), "  (not applied: --dry-run)" if dry_run else "", _RESET))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
