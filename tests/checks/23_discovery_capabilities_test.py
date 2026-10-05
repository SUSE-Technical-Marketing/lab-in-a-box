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
    check("_def_path finds install_ds389.py (a Python addon script)",
          path.endswith("install_ds389.py") and Path(path).is_file())
except FileNotFoundError:
    check("_def_path finds install_ds389.py (a Python addon script)", False)


# ── schema(): capabilities for a kubernetes-layer addon ──────────────────────
# install_mariadb supports the kubernetes layer and the OS-native layer, and it is the example for a kubernetes addon.
sc = discovery.schema("install_mariadb")
check("schema('install_mariadb') has 'kubernetes' among its capabilities.layers",
      "kubernetes" in sc.get("capabilities", {}).get("layers", []))
check("schema('install_mariadb') keeps its own schema fields (section) too",
      sc.get("section") == "mariadb")

# ── schema(): capabilities for a standalone-container addon ──────────────────
# install_grafana runs Grafana as a podman container on the host, so it is a container addon, not an OS-native package.
sc = discovery.schema("install_grafana")
check("schema('install_grafana') has a capabilities.layers of ['standalone-container']",
      sc.get("capabilities", {}).get("layers") == ["standalone-container"])

# ── discover(): every listed item carries a layers list ──────────────────────
items = discovery.discover()
check("discover() finds a reasonable number of addons (>= 30)", len(items) >= 30)
missing = [it["name"] for it in items if "layers" not in it]
check("every discover() item has a 'layers' key: {}".format(missing), missing == [])
mariadb_item = next((it for it in items if it["name"] == "install_mariadb"), None)
check("discover()'s install_mariadb entry has 'kubernetes' among its layers",
      mariadb_item is not None and "kubernetes" in mariadb_item["layers"])

# ── discover(): every listed item carries its PLUGIN targets ─────────────────
missing = [it["name"] for it in items if not it.get("targets")]
check("every discover() item has a non-empty 'targets' list: {}".format(missing), missing == [])
check("discover()'s install_mariadb entry targets container, vm and baremetal",
      mariadb_item is not None and mariadb_item["targets"] == ["container", "vm", "baremetal"])
grafana_item = next((it for it in items if it["name"] == "install_grafana"), None)
check("discover()'s install_grafana entry targets vm and baremetal only",
      grafana_item is not None and grafana_item["targets"] == ["vm", "baremetal"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all discovery_capabilities checks passed")
