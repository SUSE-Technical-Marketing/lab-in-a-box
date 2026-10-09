#!/usr/bin/env python3
# Add-on version matrix — see 77_version_matrix.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation  # noqa: E402
import versions  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


MATRIX = {
    "x_version": [
        {"version": "2.13", "kubernetes": {"rke2": {"min": "1.32", "max": "1.34"}, "k3s": {"min": "1.32"}},
         "os": ["sle15sp7", "sles16.0"]},
        {"version": "2.12"},
    ],
}

# -- normalize / matches / helm flag
check("normalize strips --version and v", versions.normalize("--version v1.20.2") == "1.20.2")
check("normalize keeps a non-version v word", versions.normalize("vX") == "vX")
check("a prefix matches its releases", versions.matches("2.13", "2.13.3") and versions.matches("2.13", "v2.13"))
check("a prefix does not match a longer minor", not versions.matches("2.1", "2.13.0"))
check("helm flag from a bare version", versions.helm_version_flag("2.13.3") == "--version 2.13.3")
check("helm flag from --version X", versions.helm_version_flag("--version v1.20.2") == "--version v1.20.2")
check("no helm flag when empty", versions.helm_version_flag("") == "" and versions.helm_version_flag(None) == "")
check("helm flag is shell-quoted", versions.helm_version_flag("1.0; rm -rf /") == "--version '1.0; rm -rf /'")
check("clu_rel channel has no Kubernetes version", versions.kubernetes_minor("stable") == ())
check("clu_rel version gives major.minor", versions.kubernetes_minor("v1.33.4+rke2r1") == (1, 33))
check("suggestions newest first", versions.suggestions(MATRIX, "x_version") == ["2.13", "2.12"])

# -- issues
check("empty version is not checked", versions.issues(MATRIX, {"x_version": ""}) == [])
check("no matrix, no issues", versions.issues({}, {"x_version": "9"}) == [])
out = versions.issues(MATRIX, {"x_version": "2.11.0"})
check("unknown version warned with the declared list", len(out) == 1 and "2.13, 2.12" in out[0])
check("declared version in range is clean",
      versions.issues(MATRIX, {"x_version": "2.13.1"}, "rke2", "v1.33.2+rke2r1", ["sle15sp7"]) == [])
out = versions.issues(MATRIX, {"x_version": "2.13.1"}, "rke2", "v1.35.0+rke2r1")
check("Kubernetes above max warned", len(out) == 1 and "1.32–1.34" in out[0])
out = versions.issues(MATRIX, {"x_version": "2.13.1"}, "rke2", "v1.31.0+rke2r1")
check("Kubernetes below min warned", len(out) == 1)
check("open max is not checked", versions.issues(MATRIX, {"x_version": "2.13"}, "k3s", "v1.40.0+k3s1") == [])
check("channel clu_rel is not range-checked", versions.issues(MATRIX, {"x_version": "2.13"}, "rke2", "stable") == [])
out = versions.issues(MATRIX, {"x_version": "2.13"}, "harvester", "stable")
check("undeclared clu_type warned", len(out) == 1 and "harvester" in out[0])
out = versions.issues(MATRIX, {"x_version": "2.13"}, os_variants=["slem5.5", "sle15sp7"])
check("undeclared OS warned once", len(out) == 1 and "slem5.5" in out[0])
check("entry without kubernetes/os accepts any placement",
      versions.issues(MATRIX, {"x_version": "2.12.4"}, "harvester", "v1.10", ["x"]) == [])

# -- preflight helper: shared section, per-node override, kcluster OS
definition = {
    "common": {"VM_OSVARIANT": "sle15sp7"},
    "nodes": {"n1": {"kcluster": "k"}, "n2": {"kcluster": "k", "VM_OSVARIANT": "slem5.5"}},
    "kclusters": {"k": {"clu_type": "rke2", "clu_rel": "v1.33.1+rke2r1"}},
    "x": {"x_version": "2.13"},
}
with mock.patch("apps.load_plugin", return_value={"versions": MATRIX}):
    out = lab_creation._addon_version_issues(definition, "x", ["n1", "n2"], "rke2", "v1.33.1+rke2r1")
    check("preflight: node OS falls back to common, own OS warned", len(out) == 1 and "slem5.5" in out[0])
    out = lab_creation._addon_version_issues(definition, {"x": {"x_version": "1.0"}}, ["n1"])
    check("preflight: per-node override is checked", len(out) == 1 and "'1.0'" in out[0])
with mock.patch("apps.load_plugin", return_value={}):
    check("preflight: add-on without matrix gives nothing",
          lab_creation._addon_version_issues(definition, "x", ["n1"], "rke2", "v1.99") == [])

# -- the declared matrices are published and well-formed
for addon, fields in (("rancher", ["cert_manager_ver", "rancher_version"]), ("longhorn", ["lh_version"]),
                      ("neuvector", ["nv_version"])):
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_{}.py".format(addon)), "--schema", "json"],
                       stdout=subprocess.PIPE, universal_newlines=True)
    caps = json.loads(r.stdout).get("capabilities", {}) if r.returncode == 0 else {}
    vers = caps.get("versions") or {}
    check("{}: --schema publishes versions for {}".format(addon, fields), sorted(vers) == fields)
    check("{}: every entry has a version".format(addon),
          all(e.get("version") for entries in vers.values() for e in entries))

# -- --validate prints warnings without failing
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"rancher": {"rancher_version": "2.9.0"}}, f)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_rancher.py"), "--validate", f.name],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("--validate: out-of-matrix version is a warning, exit 0",
      r.returncode == 0 and "[WARNING] rancher: rancher_version '2.9.0'" in r.stdout)
with open(f.name, "w") as fh:
    json.dump({"rancher": {"rancher_version": "--version 2.13.3"}}, fh)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_rancher.py"), "--validate", f.name],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("--validate: --version X form still valid and in the matrix", r.returncode == 0 and r.stdout.strip() == "")

# -- helm commands
sys.path.insert(0, str(_REPO / "scripts"))
import importlib.util  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, str(_REPO / "scripts" / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lh = load("install_longhorn")
calls = []
with mock.patch.object(lh, "ssh_run", side_effect=lambda h, cmd, **k: calls.append(cmd)):
    lh.setup_lh("n1", "k", "lab", lh_version="1.12.1")
check("longhorn: lh_version reaches helm", any("longhorn/longhorn --version 1.12.1 " in c for c in calls))
calls.clear()
with mock.patch.object(lh, "ssh_run", side_effect=lambda h, cmd, **k: calls.append(cmd)):
    lh.setup_lh("n1", "k", "lab")
check("longhorn: no --version when lh_version is empty", not any("--version" in c for c in calls))

rc = load("install_rancher")
calls.clear()
done = mock.Mock(returncode=0)
with mock.patch.object(rc, "ssh_run", side_effect=lambda h, cmd, **k: calls.append(cmd) or done):
    rc.setup_cert_manager("n1", {"cert_manager_ver": "v1.20.2"})
check("rancher: bare cert_manager_ver becomes --version", "jetstack/cert-manager --version v1.20.2 " in calls[0])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all version matrix checks passed")
