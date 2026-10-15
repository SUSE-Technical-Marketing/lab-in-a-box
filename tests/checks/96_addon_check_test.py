#!/usr/bin/env python3.11
# An add-on's --check: is what is deployed what the lab asked for? See 96_addon_check.sh.
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _lab(obj):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(obj, fh)
    return fh.name


# ── Checker: how an add-on reports ───────────────────────────────────────────
chk = ac.Checker({"longhorn": {"lh_version": "1.12.1"}}, vm_name="n1.lab", clu_name="c1")
check("the target is on the checker", chk.vm_name == "n1.lab" and chk.clu_name == "c1")
check("get() reads the lab's own value", chk.get("longhorn", "lh_version") == "1.12.1")
check("get() falls back to the default", chk.get("longhorn", "lh_rel", "longhorn") == "longhorn")
check("an unset value is the default, not an empty string",
      chk.get("longhorn", "nothing", "fallback") == "fallback")

chk.expect("the chart version", "1.12.1", "1.12.1")
check("expect() on a match is an [OK] line and no problem",
      chk.notes == ["[OK] the chart version is '1.12.1'"] and chk.problems == [])
chk.expect("the chart version", "1.12.1", "1.11.0")
check("expect() on a difference says both values",
      len(chk.problems) == 1 and "expected '1.12.1', found '1.11.0'" in chk.problems[0])
chk2 = ac.Checker({})
chk2.expect("padding is ignored", "deployed", "  deployed\n")
check("values are compared trimmed", chk2.problems == [])
chk2.fail("a plain problem")
check("fail() without values is the message alone", chk2.problems == ["[FAIL] a plain problem"])

# ── run_check: the three exit codes ──────────────────────────────────────────
path = _lab({"common": {}, "nodes": {}})
check("an add-on with no check answers 'not implemented'",
      ac.run_check(path, None, plugin={"name": "x"}) == ac.CHECK_UNSUPPORTED)
check("a check that finds nothing wrong answers 'matches'",
      ac.run_check(path, lambda c: c.ok("looked at nothing"), plugin={"name": "x"}) == ac.CHECK_OK)
check("a check that finds a difference answers 'does not match'",
      ac.run_check(path, lambda c: c.fail("not deployed"), plugin={"name": "x"}) == ac.CHECK_MISMATCH)
check("the three codes are 0, 1 and 2",
      (ac.CHECK_OK, ac.CHECK_MISMATCH, ac.CHECK_UNSUPPORTED) == (0, 1, 2))


def _raises(c):
    raise RuntimeError("kubectl is not installed")


check("a check that throws is reported, not a crash",
      ac.run_check(path, _raises, plugin={"name": "x"}) == ac.CHECK_MISMATCH)
check("a lab file that does not load is a difference, not a crash",
      ac.run_check("/nonexistent.json", lambda c: c.ok("never reached"), plugin={"name": "x"})
      == ac.CHECK_MISMATCH)

seen = {}
with mock.patch.dict(os.environ, {"_vm_name": "node1.lab", "clu_name": "cluster1"}):
    ac.run_check(path, lambda c: seen.update(vm=c.vm_name, clu=c.clu_name), plugin={"name": "x"})
check("the target comes from the same environment the install uses",
      seen == {"vm": "node1.lab", "clu": "cluster1"})
os.unlink(path)

# ── the capability says whether it is worth asking ───────────────────────────
check("an add-on that declares no check advertises check: false",
      apps.attach_capabilities({}, {"name": "x"})["capabilities"]["check"] is False)
check("one that declares it advertises check: true",
      apps.attach_capabilities({}, {"name": "x", "check": True})["capabilities"]["check"] is True)

# ── end to end, through the add-ons themselves ───────────────────────────────
empty = _lab({"common": {}, "nodes": {}, "kclusters": {}})
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_mariadb"), "--check", empty],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("an add-on without a check exits 2 and says so",
      r.returncode == 2 and "no --check implemented" in r.stdout)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_longhorn"), "--check", empty],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("longhorn checks nothing in a lab that does not install it, and exits 0",
      r.returncode == 0 and "nothing to check" in r.stdout)
caps = json.loads(subprocess.run([sys.executable, str(_REPO / "scripts" / "install_longhorn"), "--capabilities"],
                                 stdout=subprocess.PIPE, universal_newlines=True).stdout)
check("longhorn advertises its check", caps.get("check") is True)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_longhorn"), "--check"],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("--check with no lab file is refused, not a crash", r.returncode == 1 and "requires a JSON file" in r.stdout)

# The bash example add-on answers the same contract, in a lab that does not configure it.
motd = _REPO / "examples" / "addons" / "install_example_motd.sh"
r = subprocess.run([str(motd), "--check", empty], stdout=subprocess.PIPE, universal_newlines=True)
check("a bash add-on answers --check too", r.returncode == 0 and "nothing to check" in r.stdout)
r = subprocess.run([str(motd), "--check"], stdout=subprocess.PIPE, universal_newlines=True)
check("...and refuses --check without a lab file", r.returncode == 1)
os.unlink(empty)

# ── the contract checker enforces it ─────────────────────────────────────────
source = (_REPO / "scripts" / "check_addon.py").read_text()
check("check_addon.py checks the exit codes", '"--check", f.name' in source
      and "r.returncode in (0, 1, 2)" in source)
check("...and that the capability matches what the add-on does",
      'caps.get("check")' in source)
contract = (_REPO / "libs" / "apps.py").read_text()
check("the add-on contract documents --check", "--check <json>" in contract)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all add-on --check checks passed")
