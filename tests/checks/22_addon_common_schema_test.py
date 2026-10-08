#!/usr/bin/env python3
# Unit tests for addon_common.py's --schema dispatch merging PLUGIN
# capabilities into the emitted schema (json and yaml). Uses the real
# scripts/lab_schema (found via a mocked shutil.which, not a fixture
# reimplementation) against a small fixture addon script. Run from
# 22_addon_common_schema.sh, in its own container — see tests/run_tests.sh.
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


_FIXTURE_SCRIPT = '''#!/bin/bash
# JSON section: "fixture" — a fixture addon for schema tests
#
#   fixture_field : [OPTIONAL] a fixture field (default: none)
'''

with tempfile.NamedTemporaryFile(mode="w", suffix="", delete=False) as f:
    f.write(_FIXTURE_SCRIPT)
    fixture_path = f.name

_LAB_SCHEMA_PATH = str(_REPO / "scripts" / "lab_schema")


def _print_schema(fmt, plugin):
    ac._lab_schema_mod = None  # reset the module-level cache between calls
    buf = io.StringIO()
    with mock.patch.object(shutil, "which", return_value=_LAB_SCHEMA_PATH), \
         redirect_stdout(buf):
        rc = ac.print_schema(fixture_path, fmt, plugin=plugin)
    return rc, buf.getvalue()


plugin = {"name": "fixture", "targets": ["container"], "layers": ["kubernetes"],
          "requires_kubernetes": ["rke2", "k3s"], "aux_services": []}

rc, out = _print_schema("json", plugin)
check("print_schema (json) returns 0", rc == 0)
parsed = json.loads(out)
check("print_schema (json) keeps the addon's own schema fields",
      parsed.get("section") == "fixture")
check("print_schema (json) attaches capabilities.layers from the plugin",
      parsed.get("capabilities", {}).get("layers") == ["kubernetes"])
check("print_schema (json) attaches capabilities.targets from the plugin",
      parsed.get("capabilities", {}).get("targets") == ["container"])

try:
    import yaml as _yaml_available  # noqa: F401
    _has_yaml = True
except ImportError:
    _has_yaml = False

if _has_yaml:
    rc, out = _print_schema("yaml", plugin)
    check("print_schema (yaml) returns 0", rc == 0)
    check("print_schema (yaml) output mentions the capabilities block",
          "capabilities" in out and "kubernetes" in out)
else:
    print("SKIP: yaml format checks (pyyaml not installed in this test image — "
          "matches lab_schema's own optional-yaml-support behavior)")

rc, out = _print_schema("json", None)
check("print_schema with no plugin passed still succeeds, capabilities are empty/None, not an error",
      rc == 0 and json.loads(out).get("capabilities") == {
          "targets": [], "layers": [], "requires_kubernetes": None, "aux_services": [], "versions": {},
      })


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all addon_common_schema checks passed")
