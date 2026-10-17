#!/usr/bin/env python3.11
# The lab-wide deployment check driver — see 104_check_lab.sh.
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_lab", str(_REPO / "scripts" / "check_lab.py"))
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── which add-on is checked where ──────────────────────────────────────────
# The target has to be the one the install used, or a check looks at the wrong machine: a kcluster's add-ons run on
# its management node, a node's own add-ons on that node.
LAB = {
    "nodes": {
        "n1.lab": {"kcluster": "c1", "myip": "10.0.0.1"},
        "n2.lab": {"kcluster": "c1", "myip": "10.0.0.2", "addons": ["mariadb"]},
        "solo.lab": {"myip": "10.0.0.3", "addons": ["nginx"]},
    },
    "kclusters": {"c1": {"clu_type": "rke2", "addons": ["longhorn", {"rancher": {"rancher_version": "2.13"}}]}},
}
found = driver.placements(LAB)
check("a kcluster's add-ons are checked on its first node, with the cluster named",
      ("longhorn", "n1.lab", "c1") in found and ("rancher", "n1.lab", "c1") in found)
check("an add-on written as an override entry is still found", any(a == "rancher" for a, _v, _c in found))
check("a node's own add-on is checked on that node",
      ("mariadb", "n2.lab", "c1") in found and ("nginx", "solo.lab", "") in found)
check("the cluster's add-ons come before the nodes' own, as the install runs them",
      [a for a, _v, _c in found][:2] == ["longhorn", "rancher"])
check("nothing else is invented", len(found) == 4)

mgm = dict(LAB)
mgm["kclusters"] = {"c1": dict(LAB["kclusters"]["c1"], mgm_node="n2.lab")}
check("mgm_node wins when the lab names one",
      ("longhorn", "n2.lab", "c1") in driver.placements(mgm))
check("a lab with no add-ons has nothing to check", driver.placements({"nodes": {}}) == [])


# ── the target reaches the add-on the way the install passes it ────────────
def _fake_addon(tmp, name, exit_code, body="echo checked"):
    path = Path(tmp) / ("install_" + name)
    path.write_text("#!/bin/sh\n"
                    "if [ \"$1\" = \"--check\" ]; then\n"
                    "  echo \"vm=$_vm_name clu=$clu_name\"\n"
                    "  {}\n"
                    "  exit {}\n"
                    "fi\n"
                    "exit 0\n".format(body, exit_code))
    path.chmod(0o755)
    return path


with tempfile.TemporaryDirectory() as tmp:
    _fake_addon(tmp, "matcher", 0)
    _fake_addon(tmp, "mismatcher", 1, body="echo '[FAIL] the namespace is wrong'")
    _fake_addon(tmp, "nocheck", 2)
    lab = {"nodes": {"n1.lab": {"kcluster": "c1", "myip": "10.0.0.1"}},
           "kclusters": {"c1": {"clu_type": "rke2", "clu_rel": "stable", "mydomain": "x.lab",
                                "addons": ["matcher", "mismatcher", "nocheck"]}}}
    lab_path = Path(tmp) / "lab.json"
    lab_path.write_text(json.dumps(lab))

    env = dict(os.environ, PATH="{}:{}".format(tmp, os.environ["PATH"]))
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_lab.py"), str(lab_path)],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    out = r.stdout
    check("each add-on is told which VM and which cluster to look at", "vm=n1.lab clu=c1" in out)
    check("an add-on whose deployment matches is reported as matching", "[OK]" in out and "matcher" in out)
    check("one that does not is reported, with its own reason",
          "[FAIL]" in out and "the namespace is wrong" in out)
    check("one that implements no check says exactly that, and is not counted as passing",
          "[NONE]" in out and "nocheck" in out and "Nothing was verified for: nocheck" in out)
    check("the summary counts all three", "1 matched, 1 did not, 1 implement no check" in out)
    check("a mismatch makes the exit code non-zero", r.returncode == 1)

    # The whole point of the driver: a lab where nothing implements a check must not look verified.
    lab["kclusters"]["c1"]["addons"] = ["nocheck"]
    lab_path.write_text(json.dumps(lab))
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_lab.py"), str(lab_path)],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("a lab nobody checked exits 0, because nothing is wrong...", r.returncode == 0)
    check("...but says so in as many words, rather than reporting a clean lab",
          "0 matched" in r.stdout and "Nothing was verified for: nocheck" in r.stdout)

    # One add-on named on the command line, and one that is not in the lab at all.
    lab["kclusters"]["c1"]["addons"] = ["matcher", "nocheck"]
    lab_path.write_text(json.dumps(lab))
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_lab.py"), str(lab_path), "matcher"],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("naming an add-on checks only that one", "matcher" in r.stdout and "nocheck" not in r.stdout)
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_lab.py"), str(lab_path), "absent"],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("naming an add-on the lab does not have is an error", r.returncode != 0)

    # An add-on in the lab whose installer is gone is a problem, not a silent pass.
    lab["kclusters"]["c1"]["addons"] = ["matcher", "vanished"]
    lab_path.write_text(json.dumps(lab))
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_lab.py"), str(lab_path)],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("an add-on whose installer is missing is reported and makes it fail",
          r.returncode == 1 and "[GONE]" in r.stdout and "1 missing" in r.stdout)

# ── the add-ons that now have a check of their own ────────────────────────
for name in ("coredns", "local_path", "longhorn", "fleet", "rancher"):
    source = (_REPO / "scripts" / ("install_" + name)).read_text()
    check("{} advertises its check".format(name), '"check": True' in source)
    check("{} implements one".format(name), "def _check(" in source and "check_fn=_check" in source)
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_addon.py"), "--quiet",
                        str(_REPO / "scripts" / ("install_" + name))], stdout=subprocess.PIPE,
                       universal_newlines=True)
    check("{} passes the add-on contract checker".format(name), r.returncode == 0)

check("the installer deploys the driver",
      "check_lab.py" in (_REPO / "install_automation_node_scripts.sh").read_text())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all check_lab checks passed")
