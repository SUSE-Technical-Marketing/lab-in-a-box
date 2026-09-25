#!/usr/bin/env python3
# Unit tests for webui/lib/discovery.py's schema()/discover() attaching
# addon PLUGIN capabilities, against the real repo checkout's addon scripts
# (no fixtures needed — scripts/install_*.py are real
# files). Run from 23_discovery_capabilities.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import discovery  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── _def_path(): finds a ported addon across BOTH addon_dirs(), not just
#    scripts_dir() alone (the bug this fix closes — install_mariadb.py only
#    lives in scripts/, not scripts/) ────────────────────────
try:
    path = discovery._def_path("install_mariadb")
    check("_def_path finds install_mariadb.py in the scripts/ addon dir",
          path.endswith("install_mariadb.py") and Path(path).is_file())
except FileNotFoundError:
    check("_def_path finds install_mariadb.py in the scripts/ addon dir", False)

try:
    path = discovery._def_path("install_ds389")
    check("_def_path finds install_ds389.py (finally ported 2026-09-21 — the bash original, "
          "the one addon that used to have no .py counterpart, moved to legacy_bash/)",
          path.endswith("install_ds389.py") and Path(path).is_file())
except FileNotFoundError:
    check("_def_path finds install_ds389.py (finally ported 2026-09-21 — the bash original, "
          "the one addon that used to have no .py counterpart, moved to legacy_bash/)", False)


# ── schema(): capabilities attached for a real kubernetes-layer addon ────────
sc = discovery.schema("install_mariadb")
check("schema('install_mariadb') has a capabilities.layers of ['kubernetes']",
      sc.get("capabilities", {}).get("layers") == ["kubernetes"])
check("schema('install_mariadb') keeps its own schema fields (section) too",
      sc.get("section") == "mariadb")

# ── schema(): capabilities attached for a real standalone-container addon ──
# install_suma's own install() runs `mgradm ... install podman` — a real
# podman container on the host, not a bare package/binary (which is what
# LAYER_OS_NATIVE means) — corrected 2026-08-29 after live-testing found
# this addon (and install_uyuni, same mgradm/podman mechanism) misclassified.
sc = discovery.schema("install_suma")
check("schema('install_suma') has a capabilities.layers of ['standalone-container']",
      sc.get("capabilities", {}).get("layers") == ["standalone-container"])

# ── discover(): every listed item carries a layers list ──────────────────────
items = discovery.discover()
check("discover() finds a reasonable number of addons (>= 30)", len(items) >= 30)
missing = [it["name"] for it in items if "layers" not in it]
check("every discover() item has a 'layers' key: {}".format(missing), missing == [])
mariadb_item = next((it for it in items if it["name"] == "install_mariadb"), None)
check("discover()'s install_mariadb entry has layers=['kubernetes']",
      mariadb_item is not None and mariadb_item["layers"] == ["kubernetes"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all discovery_capabilities checks passed")
