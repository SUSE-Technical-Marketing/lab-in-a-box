#!/usr/bin/env python3.11
# Part of lab-in-a-box — schema versions and the compatibility record, for contributors
# Author/s: Raul Mahiques
# License: GPLv3

"""
schema_versions.py — check and record the versions of every schema (lab definition, add-ons, configuration files).

Usage:
    schema_versions.py check
        Check schemas/compatibility.json and schemas/snapshots/ against every schema's current state; print one line
        per problem and exit 1 when there is one.
    schema_versions.py diff <schema>
        Print the changes of <schema> since its last recorded version: breaking ones, then compatible ones.
    schema_versions.py record <schema> <changes> [--migration <step>]
        Record <schema>'s declared version: store its snapshot and add its entry, with <changes> as the text. A
        version that raises MAJOR is breaking and needs --migration, the libs/migrations.py step for it.
    schema_versions.py list
        Print every schema with its declared version.
    schema_versions.py --help
        Print this help.

<schema> is lab, addon:<name> or config:<file> (see libs/schema_versions.py). Run from a repository checkout.
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "libs"))

import migrations  # noqa: E402
import schema_versions as sv  # noqa: E402


def cmd_check() -> int:
    problems = sv.check(sv.current_snapshots(), sv.load_compatibility(), migrations.MIGRATIONS)
    for p in problems:
        print("FAIL: " + p)
    print("{} problem(s)".format(len(problems)) if problems else "schema versions are consistent")
    return 1 if problems else 0


def _current(name: str) -> dict:
    snaps = sv.current_snapshots()
    if name not in snaps:
        print("[ERROR] no schema {}; see: schema_versions.py list".format(name), file=sys.stderr)
        sys.exit(2)
    return snaps[name]


def cmd_diff(name: str) -> int:
    snap = _current(name)
    entries = sv.load_compatibility().get(name) or []
    last = sv.read_snapshot(name, entries[-1]["version"]) if entries else {"fields": {}}
    for change in sv.breaking_changes(last, snap):
        print("breaking:   " + change)
    for change in sv.other_changes(last, snap):
        print("compatible: " + change)
    return 0


def cmd_record(name: str, changes: str, step: str) -> int:
    snap = _current(name)
    record = sv.load_compatibility()
    entries = record.setdefault(name, [])
    if entries and entries[-1]["version"] == snap["version"]:
        print("[ERROR] {} {} is already recorded; raise the declared version first".format(name, snap["version"]),
              file=sys.stderr)
        return 1
    prev = sv.parse_version(entries[-1]["version"]) if entries else None
    breaking = prev is not None and sv.parse_version(snap["version"])[0] > prev[0]
    entry = {"version": snap["version"], "breaking": breaking, "changes": changes}
    if step:
        entry["migration"] = step
    entries.append(entry)
    sv.write_snapshot(snap)
    sv.save_record(record)
    problems = [p for p in sv.check({name: snap}, {name: entries}, migrations.MIGRATIONS) if not p.startswith("migration ")]
    for p in problems:
        print("FAIL: " + p)
    return 1 if problems else 0


def cmd_list() -> int:
    for name, snap in sorted(sv.current_snapshots().items()):
        print("{:40} {}".format(name, snap["version"] or "(none)"))
    return 0


def main(argv: list) -> int:
    if not argv or argv[0] == "--help":
        print(__doc__.strip())
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "check" and not rest:
        return cmd_check()
    if cmd == "diff" and len(rest) == 1:
        return cmd_diff(rest[0])
    if cmd == "record" and len(rest) in (2, 4) and (len(rest) == 2 or rest[2] == "--migration"):
        return cmd_record(rest[0], rest[1], rest[3] if len(rest) == 4 else "")
    if cmd == "list" and not rest:
        return cmd_list()
    print("[ERROR] unknown command; see --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
