#!/usr/bin/env python3
# Checks a page built by scripts/build-cube-static.py: its embedded API data
# must list every scripts/install_* add-on (named without its file extension)
# with its full schema, the option count the palette shows, and, for a Python
# add-on, the targets/layers its own PLUGIN literal declares; no hypervisor image data may be embedded; server-only controls
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "libs"))
import apps  # noqa: E402  (loaded from libs/, for addon_files())
import cloud_images  # noqa: E402  (loaded from libs/, for the product names an enum may carry)
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
    items = data["components"]["components"]
    comps = {c["name"]: c for c in items if c["kind"] == "addon"}
    expected = sorted({p.name.split(".", 1)[0] for p in scripts.glob("install_*")})
    check("every install_* add-on is listed (missing: {})".format(sorted(set(expected) - set(comps))),
          sorted(comps) == expected)
    check("components count matches the list", data["components"]["count"] == len(items))
    check("every item has name, kind and targets",
          all(isinstance(c.get("name"), str) and c.get("kind") in ("addon", "infrastructure", "kcluster")
              and isinstance(c.get("targets"), list) for c in items))
    check("pxe is listed as infrastructure",
          [c["name"] for c in items if c["kind"] == "infrastructure"] == ["pxe"])
    clu_enum = next(f["enum"] for f in data["base"]["sections"]["kclusters"]["fields"] if f["name"] == "clu_type")
    check("every clu_type is listed as a kcluster",
          [c["name"] for c in items if c["kind"] == "kcluster"] == clu_enum)
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
        source = Path(apps.addon_files(str(scripts)).get(name) or "")
        if not source.is_file():
            continue
        plugin = plugin_literal(source) or {}
        check("{}: targets {} match its PLUGIN {}".format(name, c["targets"], plugin.get("targets")),
              c["targets"] == plugin.get("targets"))
        check("{}: layers {} match its PLUGIN {}".format(name, c["layers"], plugin.get("layers")),
              c["layers"] == plugin.get("layers"))
    check("base schema embedded with its common section", "common" in data["base"].get("sections", {}))
    check("source label names the build, not a build-machine path",
          data["components"]["scripts_dir"].startswith("lab-in-a-box ") and "libs_dir" not in data["components"])
    # A public static page has no hypervisor behind it, so no image file name of any build machine may be baked
    # into it. The product names a cloud backend resolves for itself are not machine-specific and are expected.
    products = set(cloud_images.product_names())
    for field in fields_named(data, "ISO_IMAGE"):
        entries = field.get("enum") or []
        check("no hypervisor image list embedded: every ISO_IMAGE choice is a product name",
              all(isinstance(e, dict) and e.get("value") in products for e in entries))

hide = re.search(r"<style>([^<]*display:\s*none !important;[^<]*)</style>", page)
for sel in ("#statusPanel", "#refreshImagesBtn", "#editorImages", "#validateBtn", "#saveBtn"):
    check("{} hidden in the static page".format(sel), hide is not None and sel in hide.group(1))

if failures:
    print("{} check(s) failed".format(len(failures)), file=sys.stderr)
    sys.exit(1)
