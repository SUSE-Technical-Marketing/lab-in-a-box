#!/usr/bin/env python3
# Part of lab-in-a-box — run every add-on's own deployment check for one lab
# Author/s: Raul Mahiques
# License: GPLv3
"""
check_lab.py: what a lab's add-ons say about their own deployment.

An add-on answers `install_<addon> --check <lab.json>` with 0 when what is deployed matches the lab definition, 1
when it does not, and 2 when it implements no check at all (libs/addon_common.py's Checker). This walks a lab's
add-ons the way setup_lab.py installs them, runs the check of each on the target that add-on was installed on, and
reports per add-on.

    check_lab.py <lab.json>              every add-on of the lab
    check_lab.py <lab.json> <addon>...   only these
    check_lab.py --quiet <lab.json>      one line per add-on, no [OK] detail

An add-on that implements no check is listed as exactly that, and the summary repeats how many there were, because a
report that calls a lab healthy while half of it was never looked at is worse than no report. The exit code is 0
only when every add-on that has a check matched; add-ons without one never make it non-zero on their own.

Each check runs against the live lab over SSH, so this needs the same access setup_lab.py has.
"""

__version__ = "__LABVERSION__"

import os
import subprocess
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import apps  # noqa: E402
import primary  # noqa: E402
from addon_common import CHECK_OK, CHECK_UNSUPPORTED  # noqa: E402
from lab_creation import _GREEN, _RED, _YELLOW, _RESET, die  # noqa: E402

USAGE = "Usage: check_lab.py [--quiet] <lab.json> [<addon>...]\n"


def placements(definition):
    """
    Every (addon, vm_name, clu_name) this lab installed, in the order setup_lab.py installs them: each kcluster's
    add-ons on that cluster's management node (mgm_node, else its first node), then each node's own add-ons on that
    node. One add-on listed in two places is two placements, because each has its own target to look at.
    """
    found = []
    nodes = definition.get("nodes") or {}
    for clu_name, clu_cfg in (definition.get("kclusters") or {}).items():
        clu_cfg = clu_cfg or {}
        target = clu_cfg.get("mgm_node", "") or next(
            (name for name, cfg in nodes.items() if (cfg or {}).get("kcluster") == clu_name), "")
        for entry in clu_cfg.get("addons") or []:
            found.append((apps.addon_entry_name(entry), target, clu_name))
    for vm_name, node_cfg in nodes.items():
        node_cfg = node_cfg or {}
        for entry in node_cfg.get("addons") or []:
            found.append((apps.addon_entry_name(entry), vm_name, node_cfg.get("kcluster", "") or ""))
    return found


def run_one(addon, json_file, vm_name, clu_name):
    """
    Run one add-on's --check on its own target. Returns (code, output); the code is CHECK_OK / CHECK_MISMATCH /
    CHECK_UNSUPPORTED, or None when the add-on's installer is not there at all.
    """
    installer = apps.addon_installer(addon)
    if not installer:
        return None, "install_{} not found".format(addon)
    env = dict(os.environ)
    env["_vm_name"] = vm_name or ""
    env["clu_name"] = clu_name or ""
    r = subprocess.run([installer, "--check", json_file], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    return r.returncode, r.stdout or ""


def main():
    args = sys.argv[1:]
    if primary.wants_help(args) or not args:
        print(__doc__.strip() + "\n\n" + USAGE)
        return 0
    if "--version" in args:
        print(__version__)
        return 0
    quiet = False
    for flag in ("--quiet", "-q"):
        if flag in args:
            quiet = True
            args.remove(flag)
    if not args:
        print(USAGE, file=sys.stderr)
        die("no lab definition given")

    json_file = args[0]
    wanted = set(args[1:])
    definition = primary.load_definition(json_file)

    todo = [p for p in placements(definition) if not wanted or p[0] in wanted]
    if wanted:
        for name in sorted(wanted - {p[0] for p in todo}):
            die("addon '{}' is not in this lab".format(name))
    if not todo:
        print("This lab has no add-ons to check.")
        return 0

    print("── Checking {} add-on placement(s) of '{}' ──".format(len(todo), json_file))
    matched, mismatched, unsupported, missing = [], [], [], []
    for addon, vm_name, clu_name in todo:
        where = clu_name or vm_name or "?"
        code, output = run_one(addon, json_file, vm_name, clu_name)
        if code is None:
            missing.append(addon)
            print("  {}[GONE]{} {:<22} {:<24} {}".format(_RED, _RESET, addon, where, output.strip()))
            continue
        if code == CHECK_UNSUPPORTED:
            unsupported.append(addon)
            print("  {}[NONE]{} {:<22} {:<24} implements no check".format(_YELLOW, _RESET, addon, where))
            continue
        colour, label, bucket = ((_GREEN, "[OK]  ", matched) if code == CHECK_OK
                                 else (_RED, "[FAIL]", mismatched))
        bucket.append(addon)
        print("  {}{}{} {:<22} {:<24}".format(colour, label, _RESET, addon, where))
        if output.strip() and (code != CHECK_OK or not quiet):
            for line in output.strip().splitlines():
                print("      " + line)

    print("── {} matched, {} did not, {} implement no check, {} missing ──".format(
        len(matched), len(mismatched), len(unsupported), len(missing)))
    if unsupported:
        print("{}Nothing was verified for: {}{}".format(_YELLOW, ", ".join(sorted(set(unsupported))), _RESET))
    if mismatched:
        print("{}Does not match the lab definition: {}{}".format(_RED, ", ".join(sorted(set(mismatched))), _RESET))
    return 1 if (mismatched or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
