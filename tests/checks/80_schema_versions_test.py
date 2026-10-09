#!/usr/bin/env python3
# Schema versions, the compatibility record and migrations — see 80_schema_versions.sh.
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import migrations  # noqa: E402
import schema_versions as sv  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def snap(version, **fields):
    return {"schema": "addon:x", "version": version, "fields": fields}


def entry(version, breaking=False, migration=None):
    e = {"version": version, "breaking": breaking, "changes": "text"}
    if migration:
        e["migration"] = migration
    return e


def problems_for(current, entries, stored, steps=None):
    """sv.check() for one schema addon:x with snapshots `stored` (version -> snapshot) in a temporary directory."""
    tmp = Path(tempfile.mkdtemp())
    with mock.patch.object(sv, "SNAPSHOTS", tmp):
        for s in stored.values():
            sv.write_snapshot(s)
        return sv.check({"addon:x": current}, {"addon:x": entries}, steps if steps is not None else {})


# ── The repository's record matches every current schema ──────────────────────────────────────────────────────────
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "schema_versions.py"), "check"], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, universal_newlines=True)
check("schema_versions.py check passes on the repository:\n" + r.stdout, r.returncode == 0)
current = sv.current_snapshots()
check("every add-on in scripts/ is a schema", len([n for n in current if n.startswith("addon:")]) == len(sv.addon_paths()))
check("the lab definition and five configuration files are schemas",
      {"lab", "config:lab_creation.cfg", "config:lab_creation.defaults", "config:lab.cfg",
       "config:harvester-cluster.json", "config:credentials"} <= set(current))
check("rancher declares 2.0", current["addon:rancher"]["version"] == "2.0")

# ── check(): what it catches ───────────────────────────────────────────────────────────────────────────────────────
a = {"type": "string", "required": False}
v10 = snap("1.0", f1=a, f2=a)
check("a recorded, unchanged schema has no problem", problems_for(v10, [entry("1.0")], {"1.0": v10}) == [])
changed = snap("1.0", f1=a, f2=dict(a, default="x"))
out = problems_for(changed, [entry("1.0")], {"1.0": v10})
check("a schema changed without a new version is reported", any("record a new version" in p for p in out))
v11 = snap("1.1", f1=a)
out = problems_for(v11, [entry("1.0"), entry("1.1")], {"1.0": v10, "1.1": v11})
check("a removed field in a compatible version is reported",
      any("breaking change in a compatible version: f2 removed" in p for p in out))
v11r = snap("1.1", f1=dict(a, required=True), f2=a)
out = problems_for(v11r, [entry("1.0"), entry("1.1")], {"1.0": v10, "1.1": v11r})
check("a field made required in a compatible version is reported", any("f1 became required" in p for p in out))
v20 = snap("2.0", f1=a)
out = problems_for(v20, [entry("1.0"), entry("2.0", breaking=True)], {"1.0": v10, "2.0": v20})
check("a breaking version without a migration is reported", any("needs a migration step" in p for p in out))
out = problems_for(v20, [entry("1.0"), entry("2.0", breaking=True, migration="m")], {"1.0": v10, "2.0": v20},
                   {"m": lambda d: []})
check("a breaking version with its migration has no problem", out == [])
out = problems_for(v20, [entry("1.0"), entry("2.0", migration="m")], {"1.0": v10, "2.0": v20}, {"m": lambda d: []})
check("a MAJOR raise not marked breaking is reported", any('"breaking" must be true' in p for p in out))
v12 = snap("1.2", f1=a, f2=a)
out = problems_for(v12, [entry("1.0"), entry("1.2")], {"1.0": v10, "1.2": v12})
check("a skipped MINOR is reported", any("raises MINOR by one" in p for p in out))
out = problems_for(snap("1.1", f1=a, f2=a), [entry("1.0")], {"1.0": v10})
check("a declared version that is not recorded is reported", any("last recorded version is 1.0" in p for p in out))
out = problems_for(snap("", f1=a), [entry("1.0")], {"1.0": v10})
check("a schema without a version is reported", any("declares no schema version" in p for p in out))
out = problems_for(v10, [entry("1.0")], {"1.0": v10}, {"unused": lambda d: []})
check("a migration no version names is reported", any("migration unused" in p for p in out))

# ── rancher_2_0 ────────────────────────────────────────────────────────────────────────────────────────────────────
STABLE = migrations.RANCHER_STABLE_URL
record = migrations.load_record()


def lab(section=None, override=None, versions=None):
    entry_ = {"rancher": override} if override is not None else "rancher"
    d = {"kclusters": {"c1": {"clu_type": "rke2", "addons": [entry_]}}}
    if section is not None:
        d["rancher"] = section
    if versions is not None:
        d["schema_versions"] = versions
    return d


d = lab({"rancher_rel": "rancher-stable", "rancher_helm_chart": "rancher-stable/rancher"})
changes = migrations.migrate("lab", d, record)
check("rancher_2_0 pins the stable repo URL where rancher_rel is set",
      d["rancher"]["rancher_repo_url"] == STABLE and len(changes) == 1)
check("rancher_2_0 changes nothing on a second run", migrations.migrate("lab", d, record) == [])
d = lab({"rancher_rel": "stable"})
migrations.migrate("lab", d, record)
check("rancher_2_0 sets the chart from rancher_rel when unset", d["rancher"]["rancher_helm_chart"] == "stable/rancher")
d = lab({"rancher_shorthn": "r"})
check("rancher_2_0 leaves a section without rancher_rel alone", migrations.migrate("lab", d, record) == [])
d = lab({"rancher_rel": "rancher-stable", "rancher_repo_url": "https://example.com/charts",
         "rancher_helm_chart": "rancher-stable/rancher"})
check("rancher_2_0 leaves a set repo URL alone", migrations.migrate("lab", d, record) == [])
d = lab({"rancher_repo_url": "https://example.com/charts"}, override={"rancher_rel": "mine"})
migrations.migrate("lab", d, record)
check("rancher_2_0 fixes an addons[] override and inherits the section's URL",
      d["kclusters"]["c1"]["addons"][0]["rancher"] == {"rancher_rel": "mine", "rancher_helm_chart": "mine/rancher"})
d = lab({"rancher_rel": "rancher-stable"}, versions={"addon:rancher": "2.0"})
check("a definition that records rancher 2.0 needs no migration", migrations.needed_changes("lab", d, record) == [])
d = lab({"rancher_rel": "rancher-stable"})
check("needed_changes leaves the definition unchanged",
      migrations.needed_changes("lab", d, record) and "rancher_repo_url" not in d["rancher"])
migrations.stamp("lab", d, record)
check("stamp records the current versions of the schemas a lab uses",
      d["schema_versions"] == {"lab": record["lab"][-1]["version"], "addon:rancher": "2.0"})
lines = ["# Schema version: 0.9", "A=1"]
migrations.stamp("config:lab.cfg", lines, record)
check("stamp rewrites a shell file's version line", lines[0] == "# Schema version: " + record["config:lab.cfg"][-1]["version"])
with mock.patch.object(migrations, "die", side_effect=SystemExit("needs migration")) as died:
    try:
        migrations.require_current(lab({"rancher_rel": "rancher-stable"}), "lab.json")
    except SystemExit:
        pass
    check("require_current stops on a definition that needs a migration",
          died.called and "migrate_config.py lab.json" in died.call_args[0][0])

# ── migrate_config.py ──────────────────────────────────────────────────────────────────────────────────────────────
work = Path(tempfile.mkdtemp())
tool = [sys.executable, str(_REPO / "scripts" / "migrate_config.py")]
path = work / "lab.json"
path.write_text(json.dumps(lab({"rancher_rel": "rancher-stable", "rancher_helm_chart": "rancher-stable/rancher"})))
r = subprocess.run(tool + ["--check", str(path)], stdout=subprocess.PIPE, universal_newlines=True)
check("migrate_config.py --check exits 1 and names the change", r.returncode == 1 and "rancher_repo_url" in r.stdout)
r = subprocess.run(tool + [str(path)], stdout=subprocess.PIPE, universal_newlines=True)
out = work / "lab.json.system_modified.json"
check("migrate_config.py writes the migrated copy next to the file", r.returncode == 0 and out.is_file())
migrated = json.loads(out.read_text())
check("the migrated copy has the change and records the versions",
      migrated["rancher"]["rancher_repo_url"] == STABLE and migrated["schema_versions"]["addon:rancher"] == "2.0")
check("migrate_config.py never changes the original", "rancher_repo_url" not in json.loads(path.read_text())["rancher"])
r = subprocess.run(tool + ["--check", str(out)], stdout=subprocess.PIPE, universal_newlines=True)
check("the migrated copy is up to date", r.returncode == 0 and "up to date" in r.stdout)
cfg = work / "lab_creation.cfg"
shutil.copy(str(_REPO / "templates" / "lab_creation.cfg.example"), str(cfg))
r = subprocess.run(tool + [str(cfg)], stdout=subprocess.PIPE, universal_newlines=True)
check("migrate_config.py reads lab_creation.cfg", r.returncode == 0 and "up to date" in r.stdout)
r = subprocess.run(tool + ["--kind", "nope", str(cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   universal_newlines=True)
check("migrate_config.py rejects an unknown --kind", r.returncode == 2)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all schema version checks passed")
