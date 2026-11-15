#!/usr/bin/env python3
# Every field the shipped example labs use must be declared in the base lab schema, in the section it is used in.
# The schema is the contract other tools read — the lab-builder builds its own field list from it, and an external
# consumer warns the operator about fields the release does not know. A shipped example using an undeclared field
# turns that warning into a false alarm on lab-in-a-box's own material, which teaches people to ignore it.
# A field a migration still renames counts as known, and is reported separately: it works, but the example is in
# an obsolete shape and should be brought current. Run from 126_example_schema_fields.sh, in its own container —
# see tests/run_tests.sh.
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_schema_base  # noqa: E402
import migrations  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


SECTIONS = lab_schema_base.base_lab_schema()["sections"]
DECLARED = {name: {f["name"] for f in body["fields"]} for name, body in SECTIONS.items()}

# A field an applied migration renames away: known to the release, but obsolete in a file that still uses it.
MIGRATED = {old for old, _new in migrations._LAB_3_0_RENAMES}

examples = sorted(Path(_REPO / "examples").glob("*.json*"))
check("there are example labs to check", len(examples) >= 5)

for path in examples:
    try:
        definition = json.loads(path.read_text())
    except ValueError as e:
        check("{}: is valid JSON".format(path.name), False)
        print("   ", e)
        continue

    # {section of the schema: the field dicts in the example that section governs}
    used = {"common": [definition.get("common") or {}],
            "kclusters": list((definition.get("kclusters") or {}).values()),
            "cluster": [definition.get("cluster") or {}],       # the older single-kcluster form
            "pxe": [definition.get("pxe") or {}],
            "nodes": (list((definition.get("nodes") or {}).values())
                      + list((definition.get("disabled_nodes") or {}).values()))}
    for section, bodies in used.items():
        # The old "cluster" form is governed by the kclusters section's own fields, plus "clu_name": that form has
        # no key to take the kcluster's name from, so it carries the name as a field. The schema documents the
        # current "kclusters" shape only, where the key supplies it (libs/primary.py accepts both).
        declared = DECLARED.get("kclusters" if section == "cluster" else section)
        if section == "cluster" and declared is not None:
            declared = declared | {"clu_name"}
        if declared is None:
            check("{}: the schema has a '{}' section to check against".format(path.name, section), False)
            continue
        for body in bodies:
            for field in (body or {}):
                if field in declared:
                    continue
                if field in MIGRATED:
                    check("{}: {}.{} is obsolete — a migration renames it, so the example should use the "
                          "current name".format(path.name, section, field), False)
                    continue
                check("{}: {}.{} is declared in the base lab schema".format(path.name, section, field), False)

# The field this check was written for: an AWS-only knob two code paths read and a shipped example sets, which the
# schema did not list (libs/backends.py's run-instances --cpu-options, libs/harvester.py's own advice).
for section in ("common", "nodes"):
    check("aws_nested_virtualization is declared in the schema's '{}' section".format(section),
          "aws_nested_virtualization" in DECLARED[section])
    field = next(f for f in SECTIONS[section]["fields"] if f["name"] == "aws_nested_virtualization")
    check("...as the same true/false string shape as the other AWS knobs ({})".format(section),
          field["type"] == "string" and field["enum"] == ["true", "false"] and not field["required"])
check("the common entry carries the off-by-default that leaves existing labs unchanged",
      next(f for f in SECTIONS["common"]["fields"]
           if f["name"] == "aws_nested_virtualization").get("default") == "false")
check("the per-node entry declares no default of its own, so it falls back to common",
      "default" not in next(f for f in SECTIONS["nodes"]["fields"]
                            if f["name"] == "aws_nested_virtualization"))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all example-schema-field checks passed")
