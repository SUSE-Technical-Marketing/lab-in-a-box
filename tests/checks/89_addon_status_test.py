#!/usr/bin/env python3.11
# Add-on status — see 89_addon_status.sh.
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))
os.environ["PATH"] = "{}:{}".format(_REPO / "scripts", os.environ.get("PATH", ""))
os.environ["LAB_ADDON_CACHE"] = os.path.join(tempfile.mkdtemp(), "addons.json")

import apps  # noqa: E402
import check_addon  # noqa: E402

failures = []


def check(desc, cond):
    if cond:
        print("PASS:", desc)
    else:
        failures.append(desc)
        print("FAIL:", desc)


NOTE = "the chart's CRDs fail on K3s"
ADDON = '''#!/usr/bin/env python3.11
# Schema version: 1.0
#
# JSON section: "{name}" — test add-on
#
#   {name}_ns : [OPTIONAL] Namespace (default: demo)

__version__ = "__LABVERSION__"

PLUGIN = {{
    "name": "{name}",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],{extra}
}}

import sys
sys.path.insert(0, {libs!r})
import addon_common as ac  # noqa: E402


def main():
    ac.handle_common_args(__file__, __version__, plugin=PLUGIN)
    print("RAN")


if __name__ == "__main__":
    main()
'''

tmp = Path(tempfile.mkdtemp())
for name, extra in (("uc_demo", '\n    "status": "under-construction",\n    "status_note": {!r},'.format(NOTE)),
                    ("ok_demo", "")):
    p = tmp / "install_{}.py".format(name)
    p.write_text(ADDON.format(name=name, extra=extra, libs=str(_REPO / "libs")))
    p.chmod(0o755)
    (tmp / "install_{}".format(name)).symlink_to(p.name)
UC, OK = str(tmp / "install_uc_demo.py"), str(tmp / "install_ok_demo.py")


def run(*args):
    return subprocess.run(list(args), capture_output=True, text=True)


# ── apps.status_warning ────────────────────────────────────────────────────
check("status_warning: a stable add-on (status absent) has no warning", apps.status_warning({"name": "x"}) == "")
check("status_warning: an explicit stable status has no warning", apps.status_warning({"status": "stable"}) == "")
w = apps.status_warning({"name": "x", "status": "under-construction", "status_note": NOTE})
check("status_warning: an under-construction add-on names itself, the status and the note",
      "'x'" in w and "UNDER CONSTRUCTION" in w and NOTE in w)

# ── the add-on itself ──────────────────────────────────────────────────────
r = run(UC, "--help")
check("--help: an under-construction add-on starts with the STATUS line",
      r.returncode == 0 and r.stdout.startswith("STATUS: ") and NOTE in r.stdout.splitlines()[0])
r = run(OK, "--help")
check("--help: a stable add-on prints no STATUS line", r.returncode == 0 and "STATUS:" not in r.stdout)

r = run(UC, "lab.json")
check("run: an under-construction add-on warns on stderr and still runs",
      r.returncode == 0 and "RAN" in r.stdout and "UNDER CONSTRUCTION" in r.stderr and NOTE in r.stderr)
r = run(OK, "lab.json")
check("run: a stable add-on runs without a warning", r.returncode == 0 and "RAN" in r.stdout and "WARNING" not in r.stderr)

r = run(UC, "--validate", "lab.json")
check("--validate: prints no status warning (the preflight prints it once)",
      r.returncode == 0 and "UNDER CONSTRUCTION" not in r.stdout + r.stderr)

caps = json.loads(run(UC, "--capabilities").stdout)
check("--capabilities: carries status and status_note",
      caps.get("status") == "under-construction" and caps.get("status_note") == NOTE)
sc = json.loads(run(UC, "--schema", "json").stdout)
check("--schema json: capabilities carry status and status_note",
      sc["capabilities"].get("status") == "under-construction" and sc["capabilities"].get("status_note") == NOTE)
sc = json.loads(run(OK, "--schema", "json").stdout)
check("--schema json: a stable add-on's capabilities default to status stable and an empty note",
      sc["capabilities"].get("status") == "stable" and sc["capabilities"].get("status_note") == "")

# ── check_addon ────────────────────────────────────────────────────────────
base = {"targets": ["container"], "layers": ["kubernetes"], "requires_kubernetes": ["rke2"], "aux_services": []}
kinds = ["rke2", "k3s"]


def status_problems(extra):
    return [p for p in check_addon.capability_problems(dict(base, **extra), [], kinds) if "status" in p]


check("check_addon: no status is fine", status_problems({}) == [])
check("check_addon: under-construction with a note is fine",
      status_problems({"status": "under-construction", "status_note": NOTE}) == [])
check("check_addon: an unknown status is reported", len(status_problems({"status": "beta"})) == 1)
check("check_addon: under-construction without a note is reported",
      len(status_problems({"status": "under-construction"})) == 1)
r = run(sys.executable, str(_REPO / "scripts" / "check_addon.py"), "--quiet", UC)
check("check_addon.py: the under-construction add-on passes the whole contract", r.returncode == 0)

# ── setup_lab preflight ────────────────────────────────────────────────────
os.environ["PATH"] = "{}:{}".format(tmp, os.environ["PATH"])
import setup_lab  # noqa: E402

err, out = io.StringIO(), io.StringIO()
with redirect_stderr(err), redirect_stdout(out):
    ok = setup_lab.validate_addon_configs({"kclusters": {"c1": {"addons": ["uc_demo", "ok_demo"]}}, "nodes": {}}, "lab.json")
check("preflight: an under-construction add-on is a warning, not an error", ok is True)
check("preflight: the warning names the add-on and its note, once",
      err.getvalue().count("UNDER CONSTRUCTION") == 1 and "'uc_demo'" in err.getvalue() and NOTE in err.getvalue())

# ── web UI catalogue ───────────────────────────────────────────────────────
os.environ["LABBUILDER_SCRIPTS_DIR"] = str(tmp)
import discovery  # noqa: E402

items = {it["name"]: it for it in discovery.discover()}
check("discovery: the under-construction add-on carries its status and note",
      items.get("install_uc_demo", {}).get("status") == "under-construction" and items["install_uc_demo"].get("status_note") == NOTE)
check("discovery: a stable add-on reports status stable and no note",
      items.get("install_ok_demo", {}).get("status") == "stable" and items["install_ok_demo"].get("status_note") == "")
app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("web UI: the palette badge shows the note on hover",
      "status-badge" in app_js and "b.title = note" in app_js and 'c.status === "under-construction"' in app_js)

if failures:
    print("\n{} add-on status check(s) failed".format(len(failures)))
    sys.exit(1)
print("\nall add-on status checks passed")
