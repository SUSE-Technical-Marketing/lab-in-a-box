#!/usr/bin/env python3
# LibvirtBackend._graphics() — see 68_libvirt_graphics.sh.
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def caps(*types):
    values = "".join("<value>{}</value>".format(t) for t in types)
    return ("<domainCapabilities><devices><graphics supported='yes'>"
            "<enum name='type'>{}</enum></graphics></devices></domainCapabilities>".format(values))


def graphics(returncode, stdout):
    backend = backends.LibvirtBackend("qemu:///system")
    calls = []

    def fake_virsh(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    backend._virsh = fake_virsh
    first, second = backend._graphics(), backend._graphics()
    check("domcapabilities is queried once per backend", calls == [("domcapabilities",)])
    check("the value is stable across calls", first == second)
    return first


check("SLES 16 (no spice) → vnc", graphics(0, caps("vnc", "egl-headless", "dbus")) == "vnc,listen=0.0.0.0")
check("spice available → spice", graphics(0, caps("vnc", "spice")) == "spice,listen=0.0.0.0")
check("query failed → spice", graphics(1, "") == "spice,listen=0.0.0.0")
check("no graphics element → spice", graphics(0, "<domainCapabilities/>") == "spice,listen=0.0.0.0")

source = (_REPO / "libs" / "backends.py").read_text()
check("no hard-coded spice --graphics left", '"spice,listen=0.0.0.0"' not in source)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all libvirt graphics checks passed")
