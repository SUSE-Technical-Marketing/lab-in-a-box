#!/usr/bin/env python3
# Part of lab-in-a-box — schema versions and the compatibility record.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/schema_versions.py: the version of every schema lab-in-a-box reads, and the compatibility record of their changes.

A schema is named:

    lab                       the lab definition's base sections (lab_schema --base)
    addon:<name>              an add-on's section (install_<name> --schema json)
    config:<file>             a configuration file, one of CONFIGS

Each schema declares its version as MAJOR.MINOR: lab_schema's LAB_SCHEMA_VERSION, an add-on's schema_version (the
"# Schema version:" header line for Python add-ons), a configuration template's "# Schema version:" line or
"schema_version" key, and setup_credentials.py's CREDENTIALS_SCHEMA_VERSION.

schemas/compatibility.json lists every version of every schema: {"schemas": {<name>: [<entry>, ...]}}, oldest first,
each entry {"version": "M.m", "breaking": bool, "changes": "<text>"} plus "migration": "<libs/migrations.py step>"
when breaking. A breaking version raises MAJOR and has a migration step; any other version raises MINOR.
schemas/snapshots/<kind>/<name>-<version>.json (lab/lab-<version>.json for the lab definition) holds each version's snapshot: the schema's fields and the properties that
decide compatibility (FIELD_KEYS), descriptions left out.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPATIBILITY = REPO / "schemas" / "compatibility.json"
SNAPSHOTS = REPO / "schemas" / "snapshots"
FIELD_KEYS = ("type", "required", "default", "enum", "pattern", "min", "max")
VERSION_RE = re.compile(r"^\d+\.\d+$")
_VERSION_LINE = re.compile(r"^# Schema version:\s*(\S+)", re.M)
_SHELL_ASSIGNMENT = re.compile(r"^#?([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?=", re.M)

# Configuration files: schema name -> (kind, template the schema is read from, relative to the repo).
CONFIGS = {
    "config:lab_creation.cfg": ("shell", "templates/lab_creation.cfg.example"),
    "config:lab_creation.defaults": ("shell", "lab_creation.defaults"),
    "config:lab.cfg": ("shell", "setup_demo_server/lab.cfg.template"),
    "config:harvester-cluster.json": ("json", "templates/harvester-cluster.json.example"),
    "config:credentials": ("credentials", "scripts/setup_credentials.py"),
}


def parse_version(version: str) -> tuple:
    """(MAJOR, MINOR) of `version`; ValueError when it is not MAJOR.MINOR."""
    if not VERSION_RE.match(version or ""):
        raise ValueError("schema version {!r} is not MAJOR.MINOR".format(version))
    major, minor = version.split(".")
    return int(major), int(minor)


def _fields(field_list: list, prefix: str = "") -> dict:
    """Field name -> the FIELD_KEYS properties it sets, for schema field objects `field_list`."""
    return {prefix + f["name"]: {k: f[k] for k in FIELD_KEYS if k in f} for f in field_list}


def schema_snapshot(name: str, schema: dict) -> dict:
    """The snapshot of add-on or lab schema `schema` (lab_schema output) under schema name `name`."""
    if "sections" in schema:
        fields = {}
        for sec, body in schema["sections"].items():
            fields.update(_fields(body.get("fields", []), sec + "."))
    else:
        fields = _fields(schema.get("fields", []))
    return {"schema": name, "version": schema.get("schema_version", ""), "fields": fields}


def shell_snapshot(name: str, path: Path) -> dict:
    """The snapshot of shell-variable configuration template `path`: every variable it assigns, set or commented out."""
    text = path.read_text()
    vm = _VERSION_LINE.search(text)
    return {"schema": name, "version": vm.group(1) if vm else "",
            "fields": {m.group(1): {} for m in _SHELL_ASSIGNMENT.finditer(text)}}


def json_snapshot(name: str, path: Path) -> dict:
    """The snapshot of JSON configuration template `path`: its keys, and the keys of objects in its lists as key[].sub."""
    data = json.loads(path.read_text())
    fields = {}
    for key, value in data.items():
        if key.startswith("_comment") or key == "schema_version":
            continue
        fields[key] = {}
        for item in value if isinstance(value, list) else []:
            for sub in item if isinstance(item, dict) else {}:
                if not sub.startswith("_comment"):
                    fields["{}[].{}".format(key, sub)] = {}
    return {"schema": name, "version": data.get("schema_version", ""), "fields": fields}


def credentials_snapshot(name: str, path: Path) -> dict:
    """The snapshot of the credentials files setup_credentials.py (`path`) writes: <provider or kind>.<key> fields."""
    code = ("import json, runpy; g = runpy.run_path({!r}); "
            "print(json.dumps([g['CREDENTIALS_SCHEMA_VERSION'], g['PROVIDER_FIELDS'], g['SERVICE_CREDENTIAL_FIELDS']]))")
    out = subprocess.run([sys.executable, "-c", code.format(str(path))], stdout=subprocess.PIPE,
                         universal_newlines=True, check=True).stdout
    version, providers, kinds = json.loads(out.strip().splitlines()[-1])
    fields = {}
    for owner, rows in list(providers.items()) + list(kinds.items()):
        for key, required, _sensitive in rows:
            fields["{}.{}".format(owner, key)] = {"required": required}
    return {"schema": name, "version": version, "fields": fields}


def _run_json(cmd: list) -> dict:
    """The JSON that command `cmd` prints, run with scripts/ on PATH."""
    env = dict(os.environ, PATH="{}:{}".format(REPO / "scripts", os.environ.get("PATH", "")))
    out = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True, env=env, check=True).stdout
    return json.loads(out)


def addon_paths() -> list:
    """Every add-on in scripts/."""
    return sorted(str(p) for p in (REPO / "scripts").glob("install_*.py"))


def current_snapshots() -> dict:
    """Schema name -> snapshot of its current state, for the lab definition, every add-on and every config file."""
    snaps = {"lab": schema_snapshot("lab", _run_json([sys.executable, str(REPO / "scripts" / "lab_schema"), "--base"]))}
    for path in addon_paths():
        schema = _run_json([sys.executable, path, "--schema", "json"])
        name = "addon:" + schema["addon"]
        snaps[name] = schema_snapshot(name, schema)
    readers = {"shell": shell_snapshot, "json": json_snapshot, "credentials": credentials_snapshot}
    for name, (kind, rel) in CONFIGS.items():
        snaps[name] = readers[kind](name, REPO / rel)
    return snaps


def load_compatibility(path: Path = COMPATIBILITY) -> dict:
    """Schema name -> its entries in the compatibility record, oldest first."""
    return json.loads(path.read_text())["schemas"]


def snapshot_path(name: str, version: str) -> Path:
    """Where the snapshot of version `version` of schema `name` is stored."""
    kind, _sep, base = name.rpartition(":")
    return SNAPSHOTS / (kind or base) / "{}-{}.json".format(base, version)


def write_snapshot(snapshot: dict) -> Path:
    """Store `snapshot` as its schema's version snapshot; the path written."""
    path = snapshot_path(snapshot["schema"], snapshot["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    return path


def read_snapshot(name: str, version: str) -> dict:
    """The stored snapshot of version `version` of schema `name`, or {} when there is none."""
    path = snapshot_path(name, version)
    return json.loads(path.read_text()) if path.is_file() else {}


def breaking_changes(old: dict, new: dict) -> list:
    """Changes from snapshot `old` to snapshot `new` that a definition written for `old` may not survive."""
    out = []
    before, after = old.get("fields", {}), new.get("fields", {})
    for field, props in sorted(before.items()):
        now = after.get(field)
        if now is None:
            out.append("{} removed".format(field))
            continue
        if props.get("type") != now.get("type"):
            out.append("{} type {} -> {}".format(field, props.get("type"), now.get("type")))
        if now.get("required") and not props.get("required"):
            out.append("{} became required".format(field))
        dropped = [v for v in props.get("enum") or [] if now.get("enum") and v not in now["enum"]]
        if dropped:
            out.append("{} no longer accepts {}".format(field, ", ".join(dropped)))
    out += ["{} added as required".format(f) for f, p in sorted(after.items()) if f not in before and p.get("required")]
    return out


def other_changes(old: dict, new: dict) -> list:
    """Changes from snapshot `old` to snapshot `new` that breaking_changes() does not report."""
    out = []
    before, after = old.get("fields", {}), new.get("fields", {})
    out += ["{} added".format(f) for f, p in sorted(after.items()) if f not in before and not p.get("required")]
    for field, props in sorted(before.items()):
        now = after.get(field)
        if now is None:
            continue
        for key in ("default", "pattern", "min", "max"):
            if props.get(key) != now.get(key):
                out.append("{} {} {!r} -> {!r}".format(field, key, props.get(key), now.get(key)))
        added = [v for v in now.get("enum") or [] if v not in (props.get("enum") or [])]
        if added and props.get("enum"):
            out.append("{} also accepts {}".format(field, ", ".join(added)))
        if props.get("required") and not now.get("required"):
            out.append("{} became optional".format(field))
    return out


def _entry_problems(name: str, entry: dict, prev: tuple, prev_snap: dict, migrations: dict) -> list:
    """Problems with version entry `entry` of schema `name`, `prev` and `prev_snap` being the version before it."""
    ver = entry["version"]
    parsed = parse_version(ver)
    out = []
    stored = read_snapshot(name, ver)
    if not stored:
        out.append("{} {}: no snapshot at {}".format(name, ver, snapshot_path(name, ver)))
    if not entry.get("changes"):
        out.append("{} {}: entry has no changes text".format(name, ver))
    major_bump = prev is not None and parsed[0] > prev[0]
    if prev is not None and not major_bump and parsed != (prev[0], prev[1] + 1):
        out.append("{} {}: a compatible version raises MINOR by one".format(name, ver))
    if major_bump and parsed != (prev[0] + 1, 0):
        out.append("{} {}: a breaking version is MAJOR+1.0".format(name, ver))
    if bool(entry.get("breaking")) != major_bump:
        out.append("{} {}: \"breaking\" must be {} for this version number".format(
            name, ver, "true" if major_bump else "false"))
    step = entry.get("migration")
    if major_bump and not step:
        out.append("{} {}: a breaking version needs a migration step in libs/migrations.py".format(name, ver))
    if step and step not in migrations:
        out.append("{} {}: migration {} is not in libs/migrations.py MIGRATIONS".format(name, ver, step))
    if step and not major_bump:
        out.append("{} {}: only a breaking version has a migration".format(name, ver))
    if stored and prev_snap and not major_bump:
        out += ["{} {}: breaking change in a compatible version: {}".format(name, ver, c)
                for c in breaking_changes(prev_snap, stored)]
    return out


def _schema_problems(name: str, snap: dict, entries: list, migrations: dict) -> list:
    """Problems with the entries `entries` of schema `name`, whose current snapshot is `snap`."""
    if not snap["version"]:
        return ["{}: declares no schema version".format(name)]
    if not entries:
        return ["{}: version {} is not in schemas/compatibility.json".format(name, snap["version"])]
    out = []
    prev, prev_snap = None, {}
    for entry in entries:
        try:
            out += _entry_problems(name, entry, prev, prev_snap, migrations)
        except ValueError as e:
            return out + ["{}: {}".format(name, e)]
        prev, prev_snap = parse_version(entry["version"]), read_snapshot(name, entry["version"])
    last = entries[-1]["version"]
    if last != snap["version"]:
        out.append("{}: declares version {} but the last recorded version is {}".format(name, snap["version"], last))
    elif prev_snap and prev_snap["fields"] != snap["fields"]:
        changes = breaking_changes(prev_snap, snap) + other_changes(prev_snap, snap)
        out.append("{}: schema changed since version {} was recorded ({}); record a new version".format(
            name, last, "; ".join(changes[:5]) or "field properties changed"))
    return out


def check(current: dict, record: dict, migrations: dict) -> list:
    """
    Problems with the compatibility record `record` (load_compatibility()) for current snapshots `current`
    (current_snapshots()), given registered migration steps `migrations` (name -> function), as sentences.
    """
    problems = ["{}: in schemas/compatibility.json but no such schema exists".format(n)
                for n in sorted(set(record) - set(current))]
    for name, snap in sorted(current.items()):
        problems += _schema_problems(name, snap, record.get(name), migrations)
    named = {e.get("migration") for entries in record.values() for e in entries}
    problems += ["migration {} in libs/migrations.py is not named by any version".format(step)
                 for step in sorted(set(migrations) - named)]
    return problems

ABOUT = ("Every version of every schema lab-in-a-box reads, oldest first. A breaking version raises MAJOR and names "
         "the libs/migrations.py step that migrate_config.py applies; see CONTRIBUTING.md, Schema versions.")


def dump_record(record: dict) -> str:
    """Compatibility record `record` as the text of schemas/compatibility.json: one line per version entry."""
    blocks = []
    for name in sorted(record):
        lines = ",\n".join("   " + json.dumps(e, ensure_ascii=False) for e in record[name])
        blocks.append("  {}: [\n{}\n  ]".format(json.dumps(name), lines))
    return '{{\n "about": {},\n "schemas": {{\n{}\n }}\n}}\n'.format(json.dumps(ABOUT), ",\n".join(blocks))


def save_record(record: dict, path: Path = COMPATIBILITY) -> None:
    """Write compatibility record `record` to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record))
