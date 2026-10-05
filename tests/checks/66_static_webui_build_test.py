#!/usr/bin/env python3
# Checks a page built by scripts/build-cube-static.py: its embedded API data
# must list every scripts/install_*.py add-on with its full schema, the option
# count the palette shows, and the targets/layers its own PLUGIN literal
# declares; no hypervisor image data may be embedded; server-only controls
# must be hidden.
# Usage: 66_static_webui_build_test.py <page.html> <scripts dir>
# Run from 66_static_webui_build.sh, in its own container — see tests/run_tests.sh.
import ast
import json
import re
import sys
from pathlib import Path

page = Path(sys.argv[1]).read_text()
scripts = Path(sys.argv[2])
failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc, file=sys.stderr)


def plugin_literal(path):
    """The module-level PLUGIN = {...} dict literal of an add-on script, or None."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def fields_named(node, name):
    """Every schema field (a dict with name+type) called `name`, anywhere in node."""
    if isinstance(node, list):
        for n in node:
            yield from fields_named(n, name)
    elif isinstance(node, dict):
        if node.get("name") == name and isinstance(node.get("type"), str):
            yield node
        for v in node.values():
            yield from fields_named(v, name)


m = re.search(r'<script id="static-api-data" type="application/json">(.*?)</script>', page, re.S)
check("page embeds the static-api-data block", m is not None)
if m:
    data = json.loads(m.group(1))
    comps = {c["name"]: c for c in data["components"]["components"]}
    expected = sorted(p.stem for p in scripts.glob("install_*.py"))
    check("every install_*.py add-on is listed (missing: {})".format(sorted(set(expected) - set(comps))),
          sorted(comps) == expected)
    check("components count matches the list", data["components"]["count"] == len(comps))
    empty = sorted(n for n, c in comps.items() if not c["field_count"])
    check("every add-on has options (none with 0: {})".format(empty), empty == [])
    for name, c in sorted(comps.items()):
        sc = data["schemas"].get(name)
        check("{}: schema embedded".format(name), sc is not None)
        if sc is None:
            continue
        n_fields = len(sc.get("fields", []))
        check("{}: option count {} matches its schema's {} fields".format(name, c["field_count"], n_fields),
              c["field_count"] == n_fields)
        plugin = plugin_literal(scripts / (name + ".py")) or {}
        check("{}: targets {} match its PLUGIN {}".format(name, c["targets"], plugin.get("targets")),
              c["targets"] == plugin.get("targets"))
        check("{}: layers {} match its PLUGIN {}".format(name, c["layers"], plugin.get("layers")),
              c["layers"] == plugin.get("layers"))
    check("base schema embedded with its common section", "common" in data["base"].get("sections", {}))
    check("source label names the build, not a build-machine path",
          data["components"]["scripts_dir"].startswith("lab-in-a-box ") and "libs_dir" not in data["components"])
    check("no hypervisor image list embedded (ISO_IMAGE has no enum)",
          all("enum" not in f for f in fields_named(data, "ISO_IMAGE")))

hide = re.search(r"<style>([^<]*display:\s*none !important;[^<]*)</style>", page)
for sel in ("#statusPanel", "#refreshImagesBtn", "#editorImages", "#validateBtn", "#saveBtn"):
    check("{} hidden in the static page".format(sel), hide is not None and sel in hide.group(1))

if failures:
    print("{} check(s) failed".format(len(failures)), file=sys.stderr)
    sys.exit(1)
