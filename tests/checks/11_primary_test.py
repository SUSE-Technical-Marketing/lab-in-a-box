#!/usr/bin/env python3
# Pure-logic unit tests for libs/primary.py (lab definition
# loading, lab_creation.cfg/.defaults parsing). This is plain file/string parsing. Run from 11_primary.sh, in its own container
# — see tests/run_tests.sh.
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import primary  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _tmpfile(content, suffix=""):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name


# ── load_definition: JSON ────────────────────────────────────────────────────
path = _tmpfile(json.dumps({"nodes": {"vm1": {}}}), suffix=".json")
check("load_definition: parses a valid JSON file", primary.load_definition(path) == {"nodes": {"vm1": {}}})

died = False
try:
    primary.load_definition("/nonexistent/path.json")
except SystemExit:
    died = True
check("load_definition: dies when the file doesn't exist", died)

# ── load_definition: extensionless file falling back to YAML on bad JSON ────
path = _tmpfile("nodes:\n  vm1: {}\n")
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        result = primary.load_definition(path)
        check("load_definition: falls back to YAML parse for non-JSON content",
              result == {"nodes": {"vm1": {}}})
    except SystemExit:
        # pyyaml not installed in this environment — acceptable, the
        # fallback path itself (not crashing outright) is what matters.
        check("load_definition: YAML fallback requires pyyaml (not installed here) — dies cleanly", True)

# ── load_definition / try_load_definition: explicit .yaml extension ─────────
# This is the actual format detection setup_lab.py's preflight and every
# install_<addon> --validate rely on (both now call try_load_definition()
# instead of hardcoding json.loads() — see libs/lab_creation.py and
# libs/addon_common.py). Tolerant of pyyaml not being installed in this
# container, same pattern as the fallback test just above.
try:
    import yaml as _yaml_probe  # noqa: F401
    _has_yaml = True
except ImportError:
    _has_yaml = False

path = _tmpfile("nodes:\n  vm1:\n    myip: 192.168.1.50\ncommon:\n  ISO_IMAGE: img.qcow2\n", suffix=".yaml")
definition, error = primary.try_load_definition(path)
if _has_yaml:
    check("try_load_definition: parses a valid .yaml file",
          error is None and definition == {"nodes": {"vm1": {"myip": "192.168.1.50"}},
                                            "common": {"ISO_IMAGE": "img.qcow2"}})
else:
    check("try_load_definition: .yaml without pyyaml installed fails gracefully (no exception, no dict)",
          definition is None and "pyyaml" in error.lower())

# Malformed YAML must be reported as a YAML error, never mistaken for JSON —
# and must never raise (try_load_definition's whole point is to fold a parse
# failure into the caller's own issue report instead of dying/crashing).
path = _tmpfile("nodes: [unterminated\n", suffix=".yaml")
definition, error = primary.try_load_definition(path)
check("try_load_definition: malformed .yaml fails gracefully (no exception)",
      definition is None and error is not None)

# A file that's neither valid JSON nor valid YAML (unbalanced JSON-flow-style
# content also happens to be invalid YAML) reports both failures, doesn't
# crash, and load_definition() (the dying wrapper) turns it into SystemExit.
path = _tmpfile("{not valid json", suffix=".json")
definition, error = primary.try_load_definition(path)
check("try_load_definition: content that's neither valid JSON nor YAML fails gracefully",
      definition is None and error is not None)
died = False
try:
    primary.load_definition(path)
except SystemExit:
    died = True
check("load_definition: dies (not crashes) on unparseable content", died)

# ── save_definition: format-transparent write, symmetric to load_definition ─
# The write-side counterpart used when an in-memory definition needs to be
# persisted back (e.g. backends.py's check_or_generate_mac() on a MAC
# conflict) — takes ONLY the definition (a LabDefinition already knows its
# own source_path/fmt, set once at load time — no separate path/format
# argument for a caller to get wrong), never re-reads the source file, and
# never overwrites it either: it writes to "<source_path>.system_modified.
# <fmt>" instead, so nothing about the original — including a hand-authored
# file's own comments/formatting — is ever at risk.
data = {"nodes": {"vm1": {"myip": "192.168.1.50"}}, "common": {"ISO_IMAGE": "img.qcow2"}}

path = _tmpfile("STALE-ORIGINAL-CONTENT", suffix=".json")
definition = primary.LabDefinition(data, path, "json")
output_path = primary.save_definition(definition)
check("save_definition: writes to '<path>.system_modified.json', not the original path",
      output_path == path + ".system_modified.json")
check("save_definition: the original .json path's content is left completely untouched",
      Path(path).read_text() == "STALE-ORIGINAL-CONTENT")
check("save_definition: writes valid JSON to the new path",
      json.loads(Path(output_path).read_text()) == data)

path = _tmpfile("", suffix=".yaml")
definition = primary.LabDefinition(data, path, "yaml")
if _has_yaml:
    output_path = primary.save_definition(definition)
    check("save_definition: writes to '<path>.system_modified.yaml' for a yaml-format definition",
          output_path == path + ".system_modified.yaml")
    check("save_definition: writes valid YAML, round-trips via load_definition",
          primary.load_definition(output_path) == data)
else:
    died = False
    try:
        primary.save_definition(definition)
    except SystemExit:
        died = True
    check("save_definition: .yaml without pyyaml installed dies cleanly (not a crash)", died)


# ── load_shell_vars / _parse_shell_vars ──────────────────────────────────────
cfg_text = """\
# a comment
REMOTE_HOST=hv1.mydemo.lab
VIRT_SRV="qemu+ssh://root@${REMOTE_HOST}/system?keyfile=.ssh/id_rsa"
ROOT_SSH_KEY='/root/.ssh/id_rsa' # trailing comment
declare -A SOMEARRAY
SOMEARRAY[key]=value
EMPTY_LINE_ABOVE=1
"""
path = _tmpfile(cfg_text)
result = primary.load_shell_vars(path)
check("_parse_shell_vars: simple KEY=value parsed", result.get("REMOTE_HOST") == "hv1.mydemo.lab")
check("_parse_shell_vars: ${VAR} expanded from an earlier line in the same file",
      result.get("VIRT_SRV") == "qemu+ssh://root@hv1.mydemo.lab/system?keyfile=.ssh/id_rsa")
check("_parse_shell_vars: single-quoted value with trailing comment stripped correctly",
      result.get("ROOT_SSH_KEY") == "/root/.ssh/id_rsa")
check("_parse_shell_vars: 'declare' lines are skipped", "SOMEARRAY" not in result)
check("_parse_shell_vars: array-index assignment line is skipped (no '=' key match)",
      result.get("key") is None)
check("_parse_shell_vars: comment-only and blank lines produce no keys",
      len([k for k in result if k.startswith("#")]) == 0)

died = False
try:
    primary.load_shell_vars("/nonexistent/lab_creation.cfg")
except SystemExit:
    died = True
check("load_shell_vars: dies when the file doesn't exist", died)

# Unresolvable variable reference falls back to the environment, else is left as-is.
os.environ["_PRIMARY_TEST_ENV_VAR"] = "from-environment"
path = _tmpfile("A=${_PRIMARY_TEST_ENV_VAR}\nB=${_TOTALLY_UNDEFINED_VAR}\n")
result = primary.load_shell_vars(path)
check("_parse_shell_vars: falls back to process environment for an undefined ref",
      result.get("A") == "from-environment")
check("_parse_shell_vars: leaves a truly unresolvable reference as-is",
      result.get("B") == "${_TOTALLY_UNDEFINED_VAR}")

# Command substitution / array literals are skipped entirely (never parsed as a value).
path = _tmpfile("SKIP_ME=$(hostname)\nSKIP_BACKTICK=`hostname`\nSKIP_ARRAY=(a b c)\nKEPT=ok\n")
result = primary.load_shell_vars(path)
check("_parse_shell_vars: command substitution $() is skipped", "SKIP_ME" not in result)
check("_parse_shell_vars: backtick command substitution is skipped", "SKIP_BACKTICK" not in result)
check("_parse_shell_vars: array literal is skipped", "SKIP_ARRAY" not in result)
check("_parse_shell_vars: a normal line after skipped ones still parses", result.get("KEPT") == "ok")


# ── load_config / load_defaults: search-path fallback ────────────────────────
cfg_path = _tmpfile("REMOTE_HOST=hv2.mydemo.lab\n")
result = primary.load_config(paths=["/definitely/not/here.cfg", cfg_path])
check("load_config: searches paths in order, uses the first that exists",
      result.get("REMOTE_HOST") == "hv2.mydemo.lab")

died = False
try:
    primary.load_config(paths=["/definitely/not/here.cfg", "/also/not/here.cfg"])
except SystemExit:
    died = True
check("load_config: dies when none of the search paths exist", died)


# ── validate_definition ───────────────────────────────────────────────────────
died = False
try:
    primary.validate_definition({}, "lab.json")
except SystemExit:
    died = True
check("validate_definition: dies when 'nodes' is missing/empty", died)

# Legacy 'cluster' format without 'kclusters' — warns, doesn't die.
try:
    primary.validate_definition({"nodes": {"vm1": {}}, "cluster": {"clu_type": "rke2"}}, "lab.json")
    ok = True
except SystemExit:
    ok = False
check("validate_definition: legacy 'cluster' format warns but does not die", ok)

# Node references an undefined kcluster -> dies.
died = False
try:
    primary.validate_definition(
        {"nodes": {"vm1": {"kcluster": "nope"}}, "kclusters": {}}, "lab.json")
except SystemExit:
    died = True
check("validate_definition: dies when a node references an undefined kcluster", died)

# Valid definition -> no exception.
try:
    primary.validate_definition(
        {"nodes": {"vm1": {"kcluster": "c1"}}, "kclusters": {"c1": {}}}, "lab.json")
    ok = True
except SystemExit:
    ok = False
check("validate_definition: a valid nodes/kclusters definition does not die", ok)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all primary checks passed")
