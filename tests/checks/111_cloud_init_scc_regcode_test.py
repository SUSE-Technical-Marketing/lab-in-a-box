#!/usr/bin/env python3
# cloud-init user-data template: scc_regcode/scc_email (a node's own SCC registration code/email) must NEVER be
# read by this template — SCC registration is deliberately handled post-boot, over SSH
# (lab_creation.register_suse(), called from setup_vm.py's provision_vm() for every backend/provisioning method,
# which already prefers a node's own scc_regcode/scc_email over the lab-wide SUSE_regcode/SUSE_email when set —
# see tests/checks/14_setup_vm_test.py). A registration code is a reusable credential, not a one-time secret:
# cloud-init persists its own user-data verbatim on the instance (and often in its own log) for as long as the
# instance exists, and on some backends/configurations that data is reachable from inside the instance via the
# metadata service — a real, unnecessary exposure window an SSH round-trip (register_suse() pipes the code over
# stdin, never on a command line or into any persisted file) does not have. So this file is a guard, not a
# feature test: a node without scc_regcode must render exactly what the template rendered before scc_regcode
# existed anywhere in this project, checked against the template as committed at the revision before that (git),
# when the checkout carries one; and scc_regcode/scc_email, even when SET, must add nothing to the rendered
# cloud-init document at all. See 111_cloud_init_scc_regcode.sh.
import subprocess
import sys
import tempfile
from pathlib import Path

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
    "a cloud node (no address, no gateway)": {"_vm_name": "pluto.lab", "SOURCE_IMAGE": "sles16-byos"},
    "a node with a gateway": {"_vm_name": "sol.lab", "SOURCE_IMAGE": "sles.qcow2", "mygw": "10.0.0.1"},
    # Both the current SOURCE_IMAGE and the pre-lab_3_0 ISO_IMAGE are set to the same value here, and
    # only here: this is the one case that exercises the Ubuntu-detection branch at all, and the old
    # template (fetched below, from before scc_regcode existed) reads the OLD field name, so the
    # byte-identical comparison needs both to actually compare the same rendered branch on both sides.
    "an Ubuntu node with an address": {"_vm_name": "u.lab", "SOURCE_IMAGE": "ubuntu-24.04.img",
                                       "ISO_IMAGE": "ubuntu-24.04.img", "myip": "10.0.0.5",
                                       "mymac": "52:54:00:00:00:01", "mymask": "24", "mygw": "10.0.0.1"},
}

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
    check("{}: scc_regcode adds nothing either — registration never happens via cloud-init, by design "
          "(see this file's own header comment for why)".format(label),
          lab_creation.process_template(template, dict(values, scc_regcode="ABCD-1234")) == current)
    check("{}: scc_regcode and scc_email together still add nothing".format(label),
          lab_creation.process_template(template, dict(values, scc_regcode="ABCD-1234", scc_email="a@b.lab"))
          == current)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud-init scc_regcode checks passed")
