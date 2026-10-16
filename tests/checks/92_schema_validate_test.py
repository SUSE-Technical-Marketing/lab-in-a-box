#!/usr/bin/env python3.11
# Schema-driven value validation — see 92_schema_validate.sh.
import contextlib
import glob
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_schema_base  # noqa: E402
import migrations  # noqa: E402
import primary  # noqa: E402
import schema_validate as sv  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def where_of(findings):
    return sorted(f.where for f in findings)


# ── value_finding: one value against one field ────────────────────────────────
check("an absent value is never a finding",
      sv.value_finding({"name": "VM_MEM", "type": "integer", "min": 256}, None) is None
      and sv.value_finding({"name": "VM_MEM", "type": "integer", "min": 256}, "") is None)
check("a ??name placeholder is never checked",
      sv.value_finding({"name": "myip", "type": "string", "pattern": r"^\d+$"}, "??myip") is None)
check("a whitespace-padded placeholder is still a placeholder",
      sv.value_finding({"name": "myip", "type": "string", "pattern": r"^\d+$"}, "  ??myip") is None)

check("an integer given as a string is accepted",
      sv.value_finding({"name": "VM_MEM", "type": "integer"}, "8192") is None)
check("a non-numeric integer is an error",
      sv.value_finding({"name": "VM_MEM", "type": "integer"}, "abc").severity == "error")
check("an integer below its minimum is an error",
      "below the minimum" in sv.value_finding({"name": "VM_MEM", "type": "integer", "min": 256}, 128).message)
check("an integer above its maximum is an error",
      "above the maximum" in sv.value_finding({"name": "r", "type": "integer", "max": 3}, 9).message)
check("a boolean is not an integer",
      sv.value_finding({"name": "VM_CPU", "type": "integer"}, True).severity == "error")

check("a port outside 1-65535 is an error without the field declaring bounds",
      sv.value_finding({"name": "x_port", "type": "port"}, 99999) is not None
      and sv.value_finding({"name": "x_port", "type": "port"}, 0) is not None)
check("a port inside the range is accepted", sv.value_finding({"name": "x_port", "type": "port"}, "8080") is None)

check("a real boolean is accepted",
      sv.value_finding({"name": "existing", "type": "boolean"}, True) is None
      and sv.value_finding({"name": "existing", "type": "boolean"}, False) is None)
check("a quoted boolean is a warning, not an error",
      sv.value_finding({"name": "existing", "type": "boolean"}, "true").severity == "warning")
check("a quoted false warns that it can read as true",
      "read any non-empty value as true" in sv.value_finding({"name": "existing", "type": "boolean"}, "false").message)
check("a word that is not a boolean is an error",
      sv.value_finding({"name": "existing", "type": "boolean"}, "maybe").severity == "error")

check("an array given a string is an error",
      sv.value_finding({"name": "open_ports", "type": "array"}, "443").severity == "error")
check("an array given a list is accepted",
      sv.value_finding({"name": "open_ports", "type": "array"}, ["443", "69/udp"]) is None)
check("an object given a list is an error",
      sv.value_finding({"name": "harvester_config", "type": "object"}, ["a"]).severity == "error")
# An add-on documents a structured field as [{...}] while its declared type stays the inferred "string"
# (smlm_config_channels, smlm_activation_keys), so a list in a text field is not a finding on its own.
check("a list in a plain string field is not a finding",
      sv.value_finding({"name": "mydomain", "type": "string"}, ["a", "b"]) is None)
check("a list is still checked against an enum the field declares",
      sv.value_finding({"name": "clu_type", "type": "string", "enum": ["rke2"]}, ["rke2"]) is not None)
check("a list is still checked against a pattern the field declares",
      sv.value_finding({"name": "myip", "type": "string", "pattern": r"^\d"}, ["10.0.0.1"]) is not None)
check("a list in an integer field is still an error",
      sv.value_finding({"name": "VM_MEM", "type": "integer"}, [8192]).severity == "error")
check("a list in a boolean field is still an error",
      sv.value_finding({"name": "existing", "type": "boolean"}, [True]).severity == "error")

enum_field = {"name": "clu_type", "type": "string", "enum": ["rke2", "k3s", "harvester"]}
check("a value outside the enum is an error", sv.value_finding(enum_field, "rke3").severity == "error")
check("the error names the declared values", "rke2, k3s, harvester" in sv.value_finding(enum_field, "rke3").message)
check("a declared value is accepted", sv.value_finding(enum_field, "k3s") is None)
labelled = {"name": "config_method", "type": "string",
            "enum": [{"value": "", "label": "(default)"}, "cloud-init", "virt_customize"]}
check("a {value,label} enum entry is matched by its value", sv.value_finding(labelled, "cloud-init") is None)
check("a {value,label} enum still rejects an undeclared value", sv.value_finding(labelled, "virt-customize") is not None)
check("enum_values() unwraps {value,label} entries",
      sv.enum_values(labelled) == ["", "cloud-init", "virt_customize"])

pattern_field = {"name": "myip", "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}$"}
check("a value that does not match the pattern is an error", sv.value_finding(pattern_field, "192.168.1") is not None)
check("a value that matches the pattern is accepted", sv.value_finding(pattern_field, "192.168.1.10") is None)

# ── section_findings / definition_findings: a whole definition ────────────────
fields = [{"name": "myip", "type": "string", "pattern": r"^\d+$"}, {"name": "VM_MEM", "type": "integer"}]
check("an undeclared key in the config is left alone",
      sv.section_findings(fields, {"something_else": "whatever"}, "nodes.n1") == [])
check("skip leaves a field to the caller",
      sv.section_findings(fields, {"VM_MEM": "abc"}, "nodes.n1", skip=("VM_MEM",)) == [])
check("a finding is reported under its dotted path",
      sv.section_findings(fields, {"VM_MEM": "abc"}, "nodes.n1")[0].where == "nodes.n1.VM_MEM")

BASE = lab_schema_base.base_lab_schema()
bad = {
    "common": {"VM_MEM": "abc", "mygw": "not-an-ip", "VM_DSK_BUS": "nvme", "open_ports": "443", "overlay": "false"},
    "nodes": {"n1.lab": {"myip": "192.168.1", "mymac": "zz:00", "VM_MEM": 128}},
    "kclusters": {"c1": {"clu_type": "rke3", "clu_token": "short", "harvester_replica_count": 9}},
    "pxe": {"pxe_dhcp_mode": "nonsense"},
}
found = sv.definition_findings(bad, BASE)
check("every bad value in common, nodes, kclusters and pxe is found", where_of(found) == [
    "common.VM_DSK_BUS", "common.VM_MEM", "common.mygw", "common.open_ports", "common.overlay",
    "kclusters.c1.clu_token", "kclusters.c1.clu_type", "kclusters.c1.harvester_replica_count",
    "nodes.n1.lab.VM_MEM", "nodes.n1.lab.myip", "nodes.n1.lab.mymac", "pxe.pxe_dhcp_mode"])
check("the quoted boolean is the only warning",
      [f.where for f in found if f.severity == "warning"] == ["common.overlay"])
check("definition_findings honours skip",
      "common.VM_DSK_BUS" not in where_of(sv.definition_findings(bad, BASE, skip=("VM_DSK_BUS",))))
check("an empty definition has no findings", sv.definition_findings({}, BASE) == [])
check("a definition whose sections are not mappings is not a crash",
      sv.definition_findings({"common": "x", "nodes": {"n1": "x"}}, BASE) == [])

# ── addon_findings: an add-on's section and its per-node overrides ────────────
addon_fields = [{"name": "grafana_port", "type": "port"}, {"name": "grafana_ns", "type": "string"}]
definition = {
    "grafana": {"grafana_port": "99999"},
    "nodes": {"n1": {"addons": [{"grafana": {"grafana_port": "0"}}, "longhorn"]},
              "n2": {"addons": ["grafana"]}},
}
check("an add-on's own section and each per-node override are checked",
      where_of(sv.addon_findings(definition, "grafana", addon_fields))
      == ["grafana.grafana_port", "nodes.n1.addons.grafana.grafana_port"])
check("a plain-string addons entry is not mistaken for an override",
      sv.addon_findings({"nodes": {"n2": {"addons": ["grafana"]}}}, "grafana", addon_fields) == [])

# ── the preflight reports them ────────────────────────────────────────────────
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"common": {"SOURCE_IMAGE": "x.qcow2", "VM_MEM": "abc", "VM_DSK": "80", "VM_CPU": "6",
                          "mygw": "not-an-ip"},
               "nodes": {"n1.lab": {"myip": "192.168.1"}}}, fh)
    lab_path = fh.name
import lab_creation as lc  # noqa: E402  (after libs is on sys.path)

buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    ok = lc.validate_lab_definition(primary.load_definition(lab_path), {}, "/nonexistent", "/nonexistent",
                                    host_checks=False)
report = re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
check("the preflight fails a lab whose values do not match the schema", ok is False)
for expected in ("common.VM_MEM: 'abc' is not a whole number",
                 "common.mygw: 'not-an-ip' does not match",
                 "nodes.n1.lab.myip: '192.168.1' does not match"):
    check("the preflight reports " + expected, expected in report)
check("the preflight reports one line per bad value, not two",
      report.count("common.VM_MEM:") == 1)
Path(lab_path).unlink()

# ── every add-on's --validate runs them ──────────────────────────────────────
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"grafana": {"grafana_port": "99999"},
               "nodes": {"n1": {"addons": [{"grafana": {"grafana_port": "no-such-port"}}]}}}, fh)
    addon_path = fh.name
run = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_grafana"), "--validate", addon_path],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
check("an add-on's --validate fails on a bad value of its own", run.returncode != 0)
check("...and names the section field", "grafana.grafana_port" in run.stdout)
check("...and the per-node override", "nodes.n1.addons.grafana.grafana_port" in run.stdout)
# install_uyuni has no checks of its own: the schema ones must still run.
run = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_uyuni"), "--validate", addon_path],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
check("an add-on with no checks of its own still exits 0 on a lab that does not configure it",
      run.returncode == 0)
Path(addon_path).unlink()

# ── nothing the repository ships becomes invalid ─────────────────────────────
# The lab definitions the repository ships are the examples/ templates; a lab file in the working copy is a
# runtime artifact, not something this check may depend on.
shipped = sorted(glob.glob(str(_REPO / "examples" / "**" / "*.json*"), recursive=True)
                 + glob.glob(str(_REPO / "examples" / "**" / "*.yaml"), recursive=True))
checked = 0
for path in shipped:
    definition, parse_error = primary.try_load_definition(path)
    if parse_error or not isinstance(definition, dict) or "nodes" not in definition:
        continue
    checked += 1
    errors = [f for f in sv.definition_findings(definition, BASE) if f.severity == "error"]
    check("{} has no schema-invalid value: {}".format(
        Path(path).name, "; ".join("{}: {}".format(f.where, f.message) for f in errors)), not errors)
check("the shipped lab definitions were actually found and checked", checked > 0)

# ── the lab 2.0 migration ────────────────────────────────────────────────────
record = migrations.load_record()
lab_2_0 = [e for e in record.get("lab", []) if e.get("version") == "2.0"]
check("lab 2.0 is recorded with its migration step",
      len(lab_2_0) == 1 and lab_2_0[0].get("migration") == "open_ports_2_0")
check("the step is registered in MIGRATIONS", "open_ports_2_0" in migrations.MIGRATIONS)
old = {"common": {"open_ports": "443 69/udp"}, "nodes": {"n1": {"aws_open_ports": "443,8080"},
                                                         "n2": {"open_ports": ["443"]}}}
changes = migrations.open_ports_2_0(old)
check("a string of ports becomes a list, space- or comma-separated",
      old["common"]["open_ports"] == ["443", "69/udp"] and old["nodes"]["n1"]["aws_open_ports"] == ["443", "8080"])
check("a list is left alone", old["nodes"]["n2"]["open_ports"] == ["443"])
check("every change is reported", len(changes) == 2)
check("running it again changes nothing", migrations.open_ports_2_0(old) == [])
check("a lab that already uses lists needs no migration",
      migrations.needed_changes("lab", {"common": {"open_ports": ["443"]}, "nodes": {}}, record) == [])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all schema-validation checks passed")
