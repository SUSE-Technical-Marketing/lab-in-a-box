#!/usr/bin/env python3
# Pure-logic unit tests for libs/layers.py and apps.py's
# layers-related additions (load_plugin_from_path, attach_capabilities).
# No PATH/shutil.which lookups — fixture files used directly by path. Run
# from 21_apps_layers.sh, in its own container — see tests/run_tests.sh.
import glob
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


# ── load_plugin_from_path(): a real PLUGIN dict ────────────────────────────────
with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
    f.write('PLUGIN = {"name": "fixture", "targets": ["container"], '
            '"layers": ["kubernetes", "standalone-container"], '
            '"requires_kubernetes": ["rke2"], "aux_services": []}\n')
    fixture_path = f.name

plugin = apps.load_plugin_from_path(fixture_path, name="fixture")
check("load_plugin_from_path reads a real PLUGIN dict from an explicit path",
      plugin.get("layers") == ["kubernetes", "standalone-container"])

# ── load_plugin_from_path(): missing file / non-Python (bash) file ────────────
plugin = apps.load_plugin_from_path("/nonexistent/install_bogus.py", name="bogus")
check("load_plugin_from_path falls back to DEFAULT_PLUGIN's layers for a missing file",
      plugin.get("layers") == [layers.LAYER_KUBERNETES])

with tempfile.NamedTemporaryFile(mode="w", suffix="", delete=False) as f:
    f.write("#!/bin/bash\necho not python\n")
    bash_fixture = f.name

plugin = apps.load_plugin_from_path(bash_fixture, name="install_ds389")
check("load_plugin_from_path falls back gracefully for a bash-shaped file "
      "(regression guard for install_ds389, the one addon with no PLUGIN dict)",
      plugin.get("name") == "install_ds389" and plugin.get("layers") == [layers.LAYER_KUBERNETES])


# ── attach_capabilities(): merge shape ────────────────────────────────────────
schema = {"section": "fixture", "fields": []}
apps.attach_capabilities(schema, {
    "targets": ["container"], "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"], "aux_services": ["pxe"],
})
check("attach_capabilities adds a capabilities key without disturbing existing schema keys",
      schema["section"] == "fixture" and schema["fields"] == [])
check("attach_capabilities's capabilities dict has all four fields",
      schema["capabilities"] == {
          "targets": ["container"], "layers": ["kubernetes"],
          "requires_kubernetes": ["rke2", "k3s"], "aux_services": ["pxe"],
      })

schema2 = {}
apps.attach_capabilities(schema2, {})
check("attach_capabilities on an empty plugin dict fills in empty/None defaults, never KeyErrors",
      schema2["capabilities"] == {
          "targets": [], "layers": [], "requires_kubernetes": None, "aux_services": [],
      })


# ── every real install_*.py PLUGIN declares a non-empty layers (regression
#    guard for the mechanical 40-file sweep — catches anything left on the
#    DEFAULT_PLUGIN fallback by accident) ──────────────────────────────────────
# Count last updated 2026-09-06: +10 AI/ML addons (suse_ai, apertus, kimi, open_webui, milvus,
# qdrant, weaviate, gpu_operator, anthropic, openai) on top of the prior 40, then +11 more
# (qwen, mistral, codellama, starcoder2, agones, suse_observability, games, mailman,
# mediagoblin, colt, wikimusic), then the single bundled "games" addon was replaced with one
# addon per self-hosted game (-1 games, +3: supertux_classic, skynet_simulator, open_saber),
# then +1 more (home_assistant), then +1 more (prometheus, 2026-09-16).
scripts_dir = _REPO / "scripts"
addon_files = sorted(glob.glob(str(scripts_dir / "install_*.py")))
check("found the expected 65 python addon scripts to check", len(addon_files) == 65)
missing_layers = []
for path in addon_files:
    plugin = apps.load_plugin_from_path(path)
    if not plugin.get("targets") or not plugin.get("layers"):
        missing_layers.append(Path(path).name)
check("every install_*.py PLUGIN has both non-empty targets and non-empty layers: {}".format(
      missing_layers), missing_layers == [])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all apps_layers checks passed")
