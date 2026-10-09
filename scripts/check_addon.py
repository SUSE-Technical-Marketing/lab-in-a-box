#!/usr/bin/env python3.11
"""
check_addon.py — check an add-on executable against the lab-in-a-box add-on contract.

Usage:
    check_addon.py [--quiet] <install_<name>[.ext]> [...]

For each add-on it runs --schema json, --capabilities, --version, --help and --validate (on a lab without the add-on's
section) and checks:
  * the file name is install_<name> (extension optional) and the file is executable;
  * --schema json exits 0 and prints a JSON object with section, description and fields; every field has a name, a
    known type and a boolean "required", and its default (if any) is valid for its type;
  * capabilities: targets and layers are known values, requires_kubernetes is null or known clu_types, aux_services
    is a list, versions is a well-formed version matrix (libs/versions.py);
  * --capabilities prints the same targets, layers, requires_kubernetes and aux_services as the schema;
  * --version and --help exit 0;
  * --validate prints only [ERROR] and [WARNING] lines, and exits non-zero only with an [ERROR] line.
Prints PASS/FAIL per check (only FAIL lines with --quiet) and exits 1 when any check fails.
"""

__version__ = "__LABVERSION__"

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Callable, List

_HERE = Path(__file__).resolve().parent
for _candidate in ("/usr/local/lib/lab_creation", str(_HERE.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from layers import ALL_LAYERS  # noqa: E402
from targets import TARGET_BAREMETAL, TARGET_CONTAINER, TARGET_VM  # noqa: E402

FIELD_TYPES = ("string", "integer", "boolean", "password", "port", "url", "namespace", "version", "array", "object")
TARGETS = (TARGET_CONTAINER, TARGET_VM, TARGET_BAREMETAL)
TIMEOUT = 120


def clu_types() -> List[str]:
    """The kcluster types of the base lab schema (lab_schema next to this script, else on PATH)."""
    path = _HERE / "lab_schema"
    if not path.is_file():
        path = Path("/usr/local/bin/lab_schema")
    loader = SourceFileLoader("lab_schema", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lab_schema", loader))
    loader.exec_module(mod)
    fields = mod.base_lab_schema()["sections"]["kclusters"]["fields"]
    return next(f["enum"] for f in fields if f["name"] == "clu_type")


def run(exe: str, *args: str) -> subprocess.CompletedProcess:
    """Run add-on `exe` with `args`; stdout and stderr captured as text."""
    try:
        return subprocess.run([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess([exe] + list(args), 127, "", str(e))


def json_object(text: str):
    """`text` parsed as a JSON object, or None."""
    try:
        out = json.loads(text)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def field_problems(fields: list) -> List[str]:
    """What is wrong with schema `fields`, as sentences."""
    out = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f.get("name"):
            out.append("fields[{}] has no name".format(i))
            continue
        if f.get("type") not in FIELD_TYPES:
            out.append("field {}: type {!r} is not one of {}".format(f["name"], f.get("type"), ", ".join(FIELD_TYPES)))
        if not isinstance(f.get("required"), bool):
            out.append("field {}: required must be true or false".format(f["name"]))
        default = f.get("default")
        if default not in (None, "") and not default_ok(f.get("type"), default):
            out.append("field {}: default {!r} is not a valid {}".format(f["name"], default, f.get("type")))
    return out


def default_ok(kind: str, value: object) -> bool:
    """True when `value` is a valid default for a field of type `kind`; types without a format always pass."""
    v = str(value).strip()
    if kind == "integer":
        return bool(re.match(r"^-?\d+$", v))
    if kind == "port":
        return v.isdigit() and 1 <= int(v) <= 65535
    if kind == "boolean":
        return v.lower() in ("true", "false")
    if kind == "namespace":
        return bool(re.match(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", v))
    if kind == "url":
        return bool(re.match(r"^https?://\S+$", v))
    if kind == "version":
        return bool(re.match(r"^\S+$", v))
    return True


def capability_problems(caps: dict, field_names: List[str], kinds: List[str]) -> List[str]:
    """What is wrong with `capabilities`, as sentences; `field_names` are the schema's fields, `kinds` the clu_types."""
    out = []
    targets = caps.get("targets")
    if not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets):
        out.append("targets {!r} must be a non-empty list of {}".format(targets, ", ".join(TARGETS)))
    layers = caps.get("layers")
    if not isinstance(layers, list) or any(t not in ALL_LAYERS for t in layers):
        out.append("layers {!r} must be a list of {}".format(layers, ", ".join(ALL_LAYERS)))
    req = caps.get("requires_kubernetes")
    if req is not None and (not isinstance(req, list) or any(k not in kinds for k in req)):
        out.append("requires_kubernetes {!r} must be null or a list of {}".format(req, ", ".join(kinds)))
    if not isinstance(caps.get("aux_services", []), list):
        out.append("aux_services must be a list")
    versions = caps.get("versions") or {}
    if not isinstance(versions, dict):
        return out + ["versions must be a mapping of version field to entries"]
    for field, entries in versions.items():
        if field not in field_names:
            out.append("versions: {} is not a field of the schema".format(field))
        if not isinstance(entries, list) or not entries:
            out.append("versions.{}: must be a non-empty list of entries".format(field))
            continue
        for e in entries:
            if not isinstance(e, dict) or not str(e.get("version") or ""):
                out.append("versions.{}: every entry needs a version".format(field))
                continue
            kube = e.get("kubernetes")
            if kube is not None and (not isinstance(kube, dict) or any(k not in kinds for k in kube)):
                out.append("versions.{} {}: kubernetes keys must be clu_types ({})".format(
                    field, e["version"], ", ".join(kinds)))
            elif kube:
                for k, rng in kube.items():
                    for end in ("min", "max"):
                        v = (rng or {}).get(end)
                        if v is not None and not re.match(r"^v?\d+\.\d+$", str(v)):
                            out.append("versions.{} {}: kubernetes.{}.{} {!r} must be major.minor".format(
                                field, e["version"], k, end, v))
            os_list = e.get("os")
            if os_list is not None and (not isinstance(os_list, list) or not all(isinstance(x, str) for x in os_list)):
                out.append("versions.{} {}: os must be a list of VM_OSVARIANT values".format(field, e["version"]))
    return out


def check(exe: str, report: Callable[[bool, str], None], kinds: List[str]) -> None:
    """Run every contract check on add-on `exe`, calling report(ok, description) for each."""
    name = os.path.basename(exe)
    report(bool(re.match(r"^install_[A-Za-z0-9_-]+(\.[A-Za-z0-9]+)?$", name)),
           "name is install_<name> with an optional extension")
    report(os.path.isfile(exe) and os.access(exe, os.X_OK), "file is executable")

    r = run(exe, "--schema", "json")
    schema = json_object(r.stdout) if r.returncode == 0 else None
    report(schema is not None, "--schema json exits 0 and prints a JSON object")
    schema = schema or {}
    report(isinstance(schema.get("section"), str) and bool(schema.get("section")), "schema has a section")
    report(isinstance(schema.get("description"), str), "schema has a description")
    fields = schema.get("fields")
    report(isinstance(fields, list), "schema has a fields list")
    problems = field_problems(fields if isinstance(fields, list) else [])
    report(not problems, "fields are well-formed" + "".join("\n      " + p for p in problems))
    caps = schema.get("capabilities")
    report(isinstance(caps, dict), "schema has capabilities")
    caps = caps if isinstance(caps, dict) else {}
    names = [f.get("name") for f in fields or [] if isinstance(f, dict)]
    problems = capability_problems(caps, names, kinds)
    report(not problems, "capabilities are well-formed" + "".join("\n      " + p for p in problems))

    r = run(exe, "--capabilities")
    printed = json_object(r.stdout) if r.returncode == 0 else None
    keys = ("targets", "layers", "requires_kubernetes", "aux_services")
    report(printed is not None and all(printed.get(k) == caps.get(k) for k in keys),
           "--capabilities matches the schema's capabilities")

    report(run(exe, "--version").returncode == 0, "--version exits 0")
    report(run(exe, "--help").returncode == 0, "--help exits 0")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"common": {}, "nodes": {}, "kclusters": {}}, f)
    try:
        r = run(exe, "--validate", f.name)
    finally:
        os.unlink(f.name)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    other = [ln for ln in lines if not ln.startswith(("[ERROR]", "[WARNING]"))]
    errors = [ln for ln in lines if ln.startswith("[ERROR]")]
    report(not other, "--validate prints only [ERROR]/[WARNING] lines" + "".join("\n      " + ln for ln in other[:3]))
    report(r.returncode == 0 or bool(errors), "--validate exits non-zero only with an [ERROR] line")


def main() -> int:
    args = sys.argv[1:]
    if args[:1] in (["--version"], ["-v"]):
        print("check_addon.py {}".format(__version__))
        return 0
    quiet = "--quiet" in args
    exes = [a for a in args if a != "--quiet"]
    if not exes or exes[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if exes else 2
    kinds = clu_types()
    failed = 0
    for exe in exes:
        def report(ok: bool, desc: str, exe: str = exe) -> None:
            nonlocal failed
            failed += 0 if ok else 1
            if not ok or not quiet:
                print("{} {}: {}".format("PASS" if ok else "FAIL", exe, desc))
        check(exe, report, kinds)
    if failed:
        print("{} check(s) failed".format(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
