#!/usr/bin/env python3
# cloud-init user-data template: the scc_regcode/scc_email registration of a bring-your-own-subscription SUSE image.
# A node without scc_regcode must render exactly what the template rendered before the option existed, so every
# existing lab is unchanged; that is checked against the template as committed at the revision before this option
# (git), when the checkout carries one. See 111_cloud_init_scc_regcode.sh.
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


template = _REPO / "templates" / "cloud-init.template_user-data"
CASES = {
    "a cloud node (no address, no gateway)": {"_vm_name": "pluto.lab", "ISO_IMAGE": "sles16-byos"},
    "a node with a gateway": {"_vm_name": "sol.lab", "ISO_IMAGE": "sles.qcow2", "mygw": "10.0.0.1"},
    "an Ubuntu node with an address": {"_vm_name": "u.lab", "ISO_IMAGE": "ubuntu-24.04.img", "myip": "10.0.0.5",
                                       "mymac": "52:54:00:00:00:01", "mymask": "24", "mygw": "10.0.0.1"},
}

# ── without scc_regcode: nothing changes ─────────────────────────────────────
previous = None
try:
    previous = subprocess.run(
        ["git", "-c", "safe.directory=" + str(_REPO), "-C", str(_REPO), "show",
         "1e5cc4d:templates/cloud-init.template_user-data"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True).stdout
except (OSError, subprocess.CalledProcessError):
    print("note: no git history in this checkout, so the byte-for-byte comparison is skipped")

for label, values in CASES.items():
    current = lab_creation.process_template(template, values)
    check("{}: no SUSEConnect without scc_regcode".format(label), "SUSEConnect" not in current)
    if previous is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".tmpl", delete=False) as old:
            old.write(previous)
        before = lab_creation.process_template(old.name, values)
        Path(old.name).unlink()
        check("{}: renders exactly what the template rendered before scc_regcode existed".format(label),
              current == before)
    check("{}: scc_email alone adds nothing".format(label),
          lab_creation.process_template(template, dict(values, scc_email="a@b.lab")) == current)

# ── with scc_regcode: one runcmd entry, an argument list no shell parses ─────
node = CASES["a cloud node (no address, no gateway)"]
code = "ABCD-1234-$(touch /tmp/x)"
doc = yaml.safe_load(lab_creation.process_template(template, dict(node, scc_regcode=code)))
runcmd = doc.get("runcmd") or []
check("scc_regcode adds a SUSEConnect registration to runcmd",
      ["SUSEConnect", "-r", code] in runcmd)
check("...after the node's own setup commands, as the last entry", runcmd[-1] == ["SUSEConnect", "-r", code])
check("...passing the code verbatim as an argument, never through a shell",
      isinstance(runcmd[-1], list) and runcmd[-1][2] == code)

doc = yaml.safe_load(lab_creation.process_template(template, dict(node, scc_regcode="ABCD", scc_email="ops@x.lab")))
check("scc_email is registered together with the code",
      (doc.get("runcmd") or [])[-1] == ["SUSEConnect", "-r", "ABCD", "-e", "ops@x.lab"])

doc = yaml.safe_load(lab_creation.process_template(template, dict(CASES["a node with a gateway"], scc_regcode="ABCD")))
runcmd = doc.get("runcmd") or []
check("with a gateway route as well, both are kept and the registration still comes last",
      any("default 10.0.0.1" in str(c) for c in runcmd) and runcmd[-1] == ["SUSEConnect", "-r", "ABCD"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud-init scc_regcode checks passed")
