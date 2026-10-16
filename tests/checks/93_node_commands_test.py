#!/usr/bin/env python3.11
# pre_commands / post_commands — see 93_node_commands.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_creation as lc  # noqa: E402
import lab_schema_base  # noqa: E402
import schema_validate as sv  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


DEF = {
    "common": {"pre_commands": ["common-pre"], "post_commands": ["common-post"]},
    "nodes": {
        "inherits.lab": {},
        "own.lab": {"pre_commands": ["own-pre-1", "own-pre-2"]},
        "optsout.lab": {"pre_commands": []},
        "single.lab": {"pre_commands": "one-string"},
    },
}

# ── which commands a node runs ────────────────────────────────────────────────
check("a node without a list of its own inherits common's",
      lc.node_commands(DEF, "inherits.lab", "pre") == ["common-pre"])
check("a node's own list replaces common's, in order",
      lc.node_commands(DEF, "own.lab", "pre") == ["own-pre-1", "own-pre-2"])
check("an empty list on a node opts it out", lc.node_commands(DEF, "optsout.lab", "pre") == [])
check("a single command may be written as a string", lc.node_commands(DEF, "single.lab", "pre") == ["one-string"])
check("the phases are independent",
      lc.node_commands(DEF, "own.lab", "post") == ["common-post"])
check("no commands anywhere is an empty list", lc.node_commands({"nodes": {"n": {}}}, "n", "pre") == [])
check("an unknown node falls back to common", lc.node_commands(DEF, "nope.lab", "pre") == ["common-pre"])
check("blank entries are dropped",
      lc.node_commands({"common": {"pre_commands": ["a", "", None]}, "nodes": {"n": {}}}, "n", "pre") == ["a"])

# ── running them ──────────────────────────────────────────────────────────────
ran = []
with mock.patch.object(lc, "ssh_run", side_effect=lambda host, cmd, **kw: ran.append((host, cmd))):
    out = lc.run_node_commands(DEF, "own.lab", "pre")
check("each command runs on the VM itself, in order",
      ran == [("own.lab", "own-pre-1"), ("own.lab", "own-pre-2")] and out == [])

ran = []
with mock.patch.object(lc, "ssh_run", side_effect=lambda host, cmd, **kw: ran.append(cmd)):
    check("a node that opts out runs nothing", lc.run_node_commands(DEF, "optsout.lab", "pre") == [] and ran == [])


def _fail_second(host, cmd, **kw):
    ran.append(cmd)
    if cmd == "own-pre-1":
        raise RuntimeError("exit 1")


ran = []
with mock.patch.object(lc, "ssh_run", side_effect=_fail_second):
    out = lc.run_node_commands(DEF, "own.lab", "pre")
check("a failing command is reported", len(out) == 1 and "own-pre-1" in out[0] and "own.lab" in out[0])
check("...and the commands after it still run", ran == ["own-pre-1", "own-pre-2"])

# ── the schema declares them ──────────────────────────────────────────────────
BASE = lab_schema_base.base_lab_schema()
for section in ("common", "nodes"):
    names = [f["name"] for f in BASE["sections"][section]["fields"]]
    check("{} declares both fields".format(section),
          "pre_commands" in names and "post_commands" in names)
    for field in BASE["sections"][section]["fields"]:
        if field["name"].endswith("_commands"):
            check("{}.{} is declared as a list".format(section, field["name"]), field["type"] == "array")
# The field is a list, so the preflight rejects a lab that writes one command as a bare string; node_commands()
# still treats such a value as that one command rather than splitting it into characters.
check("a bare string is a validation error",
      len([f for f in sv.definition_findings({"nodes": {"n": {"pre_commands": "one"}}}, BASE)
           if f.where.endswith("pre_commands")]) == 1)
check("a mapping is not a list of commands",
      len([f for f in sv.definition_findings({"nodes": {"n": {"pre_commands": {"a": 1}}}}, BASE)
           if f.where.endswith("pre_commands")]) == 1)

# ── both entry points run them ───────────────────────────────────────────────
import setup_lab  # noqa: E402
import setup_vm  # noqa: E402

source = Path(setup_vm.__file__).read_text()
check("setup_vm.py runs pre_commands once the VM is up and reachable",
      'run_node_commands(definition, vm_name, "pre")' in source)
check("setup_vm.py on its own also runs post_commands",
      'run_node_commands(definition, vm_name, "post")' in source)

_report = setup_lab._RunReport()
_report.add_node("ok.lab", "created")
_report.add_node("broken.lab", "FAILED")
definition = {"common": {"post_commands": ["after"]}, "nodes": {"ok.lab": {}, "broken.lab": {}}}
calls = []
with mock.patch.object(setup_lab, "_report", _report), \
     mock.patch.object(setup_lab.lc, "run_node_commands",
                       side_effect=lambda d, vm, phase: calls.append((vm, phase)) or []):
    setup_lab.phase_post_commands(definition)
check("setup_lab.py runs post_commands for a node it created", ("ok.lab", "post") in calls)
check("...and skips a node whose VM creation failed", ("broken.lab", "post") not in calls)

_report = setup_lab._RunReport()
_report.add_node("ok.lab", "created")
with mock.patch.object(setup_lab, "_report", _report), \
     mock.patch.object(setup_lab.lc, "run_node_commands", return_value=["post_commands on 'ok.lab': boom"]):
    setup_lab.phase_post_commands(definition)
check("a failed post_command is an error in the run summary",
      any("boom" in e for e in _report.errors) and _report.failed)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all node-command checks passed")
