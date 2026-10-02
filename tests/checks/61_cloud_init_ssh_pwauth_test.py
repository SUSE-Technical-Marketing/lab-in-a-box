#!/usr/bin/env python3
# cloud-init user-data template: the per-node ssh_pwauth switch — see
# 61_cloud_init_ssh_pwauth.sh.
import sys
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
base = {"_vm_name": "ubuntu.lab", "ISO_IMAGE": "sles.qcow2", "mygw": "10.0.0.1"}

on = lab_creation.process_template(template, dict(base, ssh_pwauth="true"))
check("ssh_pwauth=true enables password login", "\nssh_pwauth: true\n" in on)
check("ssh_pwauth=true allows root login", "\ndisable_root: false\n" in on)

off = lab_creation.process_template(template, dict(base, ssh_pwauth="false"))
check("ssh_pwauth=false adds nothing", "ssh_pwauth" not in off and "disable_root" not in off)

unset = lab_creation.process_template(template, base)
check("ssh_pwauth unset adds nothing", "ssh_pwauth" not in unset)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud-init ssh_pwauth checks passed")
