#!/usr/bin/env python3.11
# Part of lab-in-a-box — migrates configuration files across breaking schema versions
# Author/s: Raul Mahiques
# License: GPLv3

"""
migrate_config.py — bring a lab definition or configuration file up to the current schema versions.

Usage:
    migrate_config.py [--check] [--kind <schema>] <file>

    <file>           a lab definition (JSON or YAML), lab_creation.cfg, lab_creation.defaults, lab.cfg,
                     a harvester-cluster JSON file or a credentials file
    --kind <schema>  the file's schema when it cannot be told from the file: lab, config:lab_creation.cfg,
                     config:lab_creation.defaults, config:lab.cfg, config:harvester-cluster.json, config:credentials
    --check          print the changes the file needs and exit 1 when there are any; write nothing
    --help           print this help

Applies every migration step (libs/migrations.py) of a breaking version newer than the one the file records, prints
each change, records the current versions in the file, and writes the result to <file>.system_modified.<ext>. The
original file is never changed. A file that needs no change is not written. Exit 0 on success, 1 when --check finds
changes, 2 on a usage or read error.
"""

__version__ = "__LABVERSION__"

import json
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import migrations  # noqa: E402
import primary  # noqa: E402

SHELL_FILES = ("lab_creation.cfg", "lab_creation.defaults", "lab.cfg")
KINDS = ("lab",) + tuple("config:" + f for f in SHELL_FILES) + ("config:harvester-cluster.json", "config:credentials")


def _parse(text: str):
    """`text` parsed as JSON, else as YAML: (data, format)."""
    try:
        return json.loads(text), "json"
    except ValueError:
        import yaml
        return yaml.safe_load(text), "yaml"


def detect_kind(path: Path, text: str) -> str:
    """The schema of file `path` with content `text`."""
    if path.name in SHELL_FILES:
        return "config:" + path.name
    data, _fmt = _parse(text)
    keys = {k.lower() for k in data} if isinstance(data, dict) else set()
    if keys & {"cloudtype", "cloud_type", "credential_kind", "kind"}:
        return "config:credentials"
    if "harvester_version" in keys and isinstance(data.get("nodes"), list):
        return "config:harvester-cluster.json"
    return "lab"


def load(path: Path, kind: str):
    """File `path` of schema `kind` parsed for migrations: (data, writer), writer(data) writing the migrated copy."""
    if kind == "lab":
        definition = primary.load_definition(str(path))
        return definition, primary.save_definition
    text = path.read_text()
    out = "{}.system_modified.{}".format(path, path.suffix.lstrip(".") or "txt")
    if kind.startswith("config:") and kind[len("config:"):] in SHELL_FILES:
        def write_lines(lines):
            Path(out).write_text("\n".join(lines) + "\n")
            return out
        return text.splitlines(), write_lines
    data, fmt = _parse(text)

    def write_data(d):
        if fmt == "json":
            Path(out).write_text(json.dumps(d, indent=2) + "\n")
        else:
            import yaml
            Path(out).write_text(yaml.safe_dump(d, sort_keys=False))
        return out
    return data, write_data


def main(argv: list) -> int:
    if not argv or "--help" in argv:
        print(__doc__.strip())
        return 0 if argv else 2
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    kind = None
    if "--kind" in args:
        at = args.index("--kind")
        kind = args[at + 1] if at + 1 < len(args) else ""
        del args[at:at + 2]
        if kind not in KINDS:
            print("[ERROR] --kind must be one of: {}".format(", ".join(KINDS)), file=sys.stderr)
            return 2
    if len(args) != 1:
        print("[ERROR] give exactly one file; see --help", file=sys.stderr)
        return 2
    path = Path(args[0])
    if not path.is_file():
        print("[ERROR] {}: no such file".format(path), file=sys.stderr)
        return 2
    kind = kind or detect_kind(path, path.read_text())
    data, write = load(path, kind)
    record = migrations.load_record()
    if check:
        changes = migrations.needed_changes(kind, data, record)
        for change in changes:
            print(change)
        print("{}: {}".format(path, "needs migration ({})".format(kind) if changes else "up to date"))
        return 1 if changes else 0
    changes = migrations.migrate(kind, data, record)
    for change in changes:
        print(change)
    if not changes:
        print("{}: up to date".format(path))
        return 0
    migrations.stamp(kind, data, record)
    print("wrote {}".format(write(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
