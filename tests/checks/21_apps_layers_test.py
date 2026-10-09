#!/usr/bin/env python3
# Unit tests for libs/layers.py and apps.py's add-on contract (describe, load_plugin_from_path, addon_files,
# attach_capabilities). No PATH/shutil.which lookups — fixture executables used directly by path. Run from
# 21_apps_layers.sh, in its own container — see tests/run_tests.sh.
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import apps  # noqa: E402
import layers  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── LAYER_* constants / DEFAULT_PLUGIN ─────────────────────────────────────────
check("three layer constants are distinct strings",
      len({layers.LAYER_OS_NATIVE, layers.LAYER_STANDALONE_CONTAINER, layers.LAYER_KUBERNETES}) == 3)
check("ALL_LAYERS lists all three", set(layers.ALL_LAYERS) ==
      {layers.LAYER_OS_NATIVE, layers.LAYER_STANDALONE_CONTAINER, layers.LAYER_KUBERNETES})
check("DEFAULT_PLUGIN declares kubernetes only (matches today's implicit assumption)",
      apps.DEFAULT_PLUGIN["layers"] == [layers.LAYER_KUBERNETES])


os.environ["LAB_ADDON_CACHE"] = os.path.join(tempfile.mkdtemp(), "addons.json")
_tmp = Path(tempfile.mkdtemp())


def _executable(name, body, mode=0o755):
    path = _tmp / name
    path.write_text(body)
    path.chmod(mode)
    return str(path)


_caps = {"targets": ["container"], "layers": ["kubernetes", "standalone-container"],
         "requires_kubernetes": ["rke2"], "aux_services": []}

# ── load_plugin_from_path(): capabilities from a non-Python add-on's own --schema json ──
bash_addon = _executable("install_fixture", "#!/bin/bash\n[ \"$1 $2\" = \"--schema json\" ] && echo '{}'\n".format(
    json.dumps({"section": "fixture", "fields": [], "capabilities": _caps})))
plugin = apps.load_plugin_from_path(bash_addon, name="fixture")
check("load_plugin_from_path reads the capabilities a bash add-on prints for --schema json",
      plugin.get("layers") == ["kubernetes", "standalone-container"] and plugin.get("requires_kubernetes") == ["rke2"]
      and plugin.get("name") == "fixture")
check("describe returns the add-on's whole --schema json output", apps.describe(bash_addon).get("section") == "fixture")
with open(os.environ["LAB_ADDON_CACHE"]) as f:
    check("describe caches the output", len(json.load(f)) == 1)
Path(bash_addon).write_text("#!/bin/bash\necho '{\"section\": \"changed\"}'\n")
check("describe runs the add-on again once the file changes", apps.describe(bash_addon).get("section") == "changed")

# ── load_plugin_from_path(): fallbacks ────────────────────────────────────────
plugin = apps.load_plugin_from_path("/nonexistent/install_bogus.py", name="bogus")
check("load_plugin_from_path falls back to DEFAULT_PLUGIN's layers for a missing file",
      plugin.get("layers") == [layers.LAYER_KUBERNETES])
for label, path in (
        ("a non-executable file", _executable("install_noexec", "#!/bin/bash\necho '{}'\n", mode=0o644)),
        ("an add-on printing no JSON", _executable("install_garbage", "#!/bin/bash\necho not json\n")),
        ("an add-on exiting non-zero", _executable("install_fails", "#!/bin/bash\necho '{\"section\": \"x\"}'; exit 3\n"))):
    plugin = apps.load_plugin_from_path(path, name="x")
    check("load_plugin_from_path falls back to DEFAULT_PLUGIN for {}".format(label),
          plugin.get("layers") == [layers.LAYER_KUBERNETES] and plugin.get("name") == "x")

# ── addon_files(): names without extension, the deployed file wins ─────────────
_dir = Path(tempfile.mkdtemp())
for fname in ("install_a.py", "install_b", "install_c.sh", "install_c", "install_c.py", "lab_schema", "notes.txt"):
    (_dir / fname).write_text("")
found = apps.addon_files(str(_dir))
check("addon_files lists add-ons by name without extension: {}".format(sorted(found)),
      sorted(found) == ["install_a", "install_b", "install_c"])
check("addon_files keeps the last file in sorted order for a duplicated name, as the installer deploys it",
      found["install_c"].endswith("install_c.sh"))

# ── attach_capabilities(): merge shape ────────────────────────────────────────
schema = {"section": "fixture", "fields": []}
apps.attach_capabilities(schema, {
    "targets": ["container"], "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"], "aux_services": ["pxe"],
})
check("attach_capabilities adds a capabilities key without disturbing existing schema keys",
      schema["section"] == "fixture" and schema["fields"] == [])
check("attach_capabilities's capabilities dict has all five fields",
      schema["capabilities"] == {
          "targets": ["container"], "layers": ["kubernetes"],
          "requires_kubernetes": ["rke2", "k3s"], "aux_services": ["pxe"], "versions": {},
      })

schema2 = {}
apps.attach_capabilities(schema2, {})
check("attach_capabilities on an empty plugin dict fills in empty/None defaults, never KeyErrors",
      schema2["capabilities"] == {
          "targets": [], "layers": [], "requires_kubernetes": None, "aux_services": [], "versions": {},
      })


# ── every tracked add-on is executable and declares non-empty targets and layers ──
# This catches an add-on that falls back to DEFAULT_PLUGIN by accident. Each one must declare its own capabilities.
tracked = subprocess.run(["git", "-c", "safe.directory={}".format(_REPO), "-C", str(_REPO), "ls-files", "-s", "scripts/install_*"],
                         stdout=subprocess.PIPE, universal_newlines=True).stdout.split("\n")
modes = {line.split()[3]: line.split()[0] for line in tracked if line.strip()}
check("found the expected 71 tracked add-ons to check", len(modes) == 71)
check("every tracked add-on has the executable bit in git: {}".format(
      sorted(p for p, m in modes.items() if m != "100755")), all(m == "100755" for m in modes.values()))
described = apps.describe_many(str(_REPO / p) for p in modes)
missing = sorted(Path(p).name for p, out in described.items()
                 if not (out.get("capabilities") or {}).get("targets") or not (out.get("capabilities") or {}).get("layers"))
check("every tracked add-on's --schema json has non-empty targets and layers: {}".format(missing), missing == [])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all apps_layers checks passed")
