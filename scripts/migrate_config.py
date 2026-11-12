#!/usr/bin/env python3.11
# Part of lab-in-a-box — migrates configuration files across breaking schema versions
# Author/s: Raul Mahiques
# License: GPLv3

"""
migrate_config.py — bring a lab definition or configuration file up to the current schema versions.

Usage:
    migrate_config.py [--check] [--kind <schema>] <file>

    <file>           a lab definition (JSON or YAML), lab_creation.cfg, lab_creation.defaults,
                     automation.cfg, kvm_host.cfg, a harvester-cluster JSON file or a credentials file
    --kind <schema>  the file's schema when it cannot be told from the file: lab, config:lab_creation.cfg,
                     config:lab_creation.defaults, config:automation.cfg, config:kvm_host.cfg,
                     config:harvester-cluster.json, config:credentials
    --check          print the changes the file needs and exit 1 when there are any; write nothing
    --help           print this help

Applies every migration step (libs/migrations.py) of a breaking version newer than the one the file records, prints
each change, records the current versions in the file, and writes the result to <file>.system_modified.<ext>. The
original file is never changed. A file that needs no change is not written. Exit 0 on success, 1 when --check finds
changes, 2 on a usage or read error.

A pre-split, single lab.cfg (named exactly "lab.cfg") is a special one-time case: it can no longer be migrated
field-by-field in place, since its fields now belong to two separate files. Pass it directly (no --kind needed,
detected from the filename) and it is split into <file>.automation.cfg.system_modified and
<file>.kvm_host.cfg.system_modified, with any SUSE_regcode/SUSE_email/SUSE_url moved into the encrypted
credential store (scripts/setup_credentials.py's "scc" kind) instead of either file.
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

SHELL_FILES = ("lab_creation.cfg", "lab_creation.defaults", "automation.cfg", "kvm_host.cfg")
KINDS = ("lab",) + tuple("config:" + f for f in SHELL_FILES) + ("config:harvester-cluster.json", "config:credentials")

# A pre-split lab.cfg's fields, by which of the two new files they moved to — see
# setup_demo_server/automation.cfg.template / kvm_host.cfg.template, the current shape both
# split from. Anything not listed here (an operator's own custom addition, a stale/renamed key)
# is kept in automation.cfg, the more general-purpose of the two, rather than silently dropped.
_SPLIT_KVM_HOST_KEYS = {"_QCOW_IMAGE", "_bridge_name", "_bridge_nic", "_network_mode",
                        "_nat_network_name", "_nat_network_cidr", "_nat_forwarded_ports",
                        "_extra_host_pkgs", "_automation_mac", "_automation_graphics",
                        "_virt_srv", "_qemu_addr", "_disk_size"}
_SPLIT_CREDENTIAL_KEYS = {"SUSE_regcode", "SUSE_email", "SUSE_url"}


def _parse(text: str):
    """`text` parsed as JSON, else as YAML: (data, format)."""
    try:
        return json.loads(text), "json"
    except ValueError:
        import yaml
        return yaml.safe_load(text), "yaml"


def detect_kind(path: Path, text: str) -> str:
    """The schema of file `path` with content `text`."""
    if path.name == "lab.cfg":
        return "legacy:lab.cfg"
    if path.name in SHELL_FILES:
        return "config:" + path.name
    data, _fmt = _parse(text)
    keys = {k.lower() for k in data} if isinstance(data, dict) else set()
    if keys & {"cloudtype", "cloud_type", "credential_kind", "kind"}:
        return "config:credentials"
    if "harvester_version" in keys and isinstance(data.get("nodes"), list):
        return "config:harvester-cluster.json"
    return "lab"


def split_legacy_lab_cfg(path: Path, automation_out: Path = None, kvm_out: Path = None) -> list:
    """
    One-time split of a pre-automation.cfg/kvm_host.cfg lab.cfg into both new files, with
    SUSE_regcode/SUSE_email/SUSE_url moved into the encrypted credential store instead of either
    one. Never touches the original. Writes to automation_out/kvm_out when given — the installer
    (setup_demo_server/setup_kvm_node.py's _auto_split_legacy_lab_cfg()) passes the real live
    automation.cfg/kvm_host.cfg paths there, so the split lands directly where it's used, with
    nothing left to promote by hand; the default (<path>.automation.cfg.system_modified /
    <path>.kvm_host.cfg.system_modified) is for this module's own --check-then-review CLI use.
    Prints each change, same as the generic migration path. Returns the list of change lines
    printed (empty only if the file had nothing to move, which in practice never happens for a
    real pre-split lab.cfg).
    """
    import setup_credentials  # noqa: E402 (scripts/ sibling — see sys.path setup above)

    automation_out = automation_out or Path("{}.automation.cfg.system_modified".format(path))
    kvm_out = kvm_out or Path("{}.kvm_host.cfg.system_modified".format(path))
    automation_lines, kvm_lines, changes = [], [], []
    scc_fields = {}
    for line in path.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        if key in _SPLIT_CREDENTIAL_KEYS:
            value = line.split("=", 1)[1].strip("'\"")
            if value:
                scc_fields["scc_" + key[len("SUSE_"):].lower()] = value
            changes.append("{}: moved to the encrypted credential store, out of any config file".format(key))
            continue
        (kvm_lines if key in _SPLIT_KVM_HOST_KEYS else automation_lines).append(line)

    automation_out.write_text("\n".join(automation_lines) + "\n")
    kvm_out.write_text("\n".join(kvm_lines) + "\n")
    changes.append("split into {} and {}".format(automation_out, kvm_out))
    if scc_fields:
        out_dir = Path(primary.credentials_dirs()[0])
        setup_credentials.write_credential_file(out_dir / "scc.yaml", "scc", scc_fields, encrypt=False)
        changes.append("wrote {} ({})".format(out_dir / "scc.yaml", ", ".join(sorted(scc_fields))))
    return changes


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
    if not argv or primary.wants_help(argv):
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
    if kind == "legacy:lab.cfg":
        if check:
            print("{}: needs the one-time automation.cfg/kvm_host.cfg split".format(path))
            return 1
        changes = split_legacy_lab_cfg(path)
        for change in changes:
            print(change)
        return 0
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
