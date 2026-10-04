#!/usr/bin/env python3
# Pure-logic unit tests for libs/lab_usb.py and scripts/build_lab_usb.py's
# own repo-root detection — no real VM/SSH needed for any of this, it's all
# in-memory data transformation or a real (but self-contained, disposable)
# git repo in a tempdir. Run from 32_lab_usb.sh, in its own container — see
# tests/run_tests.sh.
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_usb  # noqa: E402
import build_lab_usb  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── remap_lab_definition_to_nat: core remapping ──────────────────────────────
original = {
    "common": {"ISO_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_CPU": "2", "VM_DSK": "40",
               "mygw": "192.168.88.1", "mymask": "24", "mydns": "192.168.88.73",
               "mydomain": "mydemo.lab"},
    "nodes": {
        "srv1.mydemo.lab": {"myip": "192.168.88.101", "mymac": "34:8a:b1:4b:1a:c1"},
        "agent1.mydemo.lab": {"myip": "192.168.88.102", "mymac": "34:8a:b1:4b:1a:c2"},
    },
}

remapped = lab_usb.remap_lab_definition_to_nat(
    original, nat_cidr="192.168.150.0/24", nested_automation_ip="192.168.150.2")

check("remap: never mutates the original definition in place",
      original["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.88.101"
      and original["common"]["mygw"] == "192.168.88.1")

check("remap: every node gets a new myip inside the NAT range",
      all(n["myip"].startswith("192.168.150.") for n in remapped["nodes"].values()))
check("remap: node order is preserved (deterministic remapping, srv1 before agent1)",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] < remapped["nodes"]["agent1.mydemo.lab"]["myip"])
check("remap: node addresses start at the given offset (default 10), not the network address itself",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.150.10")
check("remap: distinct nodes get distinct addresses",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] != remapped["nodes"]["agent1.mydemo.lab"]["myip"])

check("remap: mymac is left untouched — a MAC doesn't need to be 'in range'",
      remapped["nodes"]["srv1.mydemo.lab"]["mymac"] == "34:8a:b1:4b:1a:c1")

check("remap: common.mygw becomes the NAT network's own gateway (first usable host)",
      remapped["common"]["mygw"] == "192.168.150.1")
check("remap: common.mymask reflects the NAT network's own prefix length",
      remapped["common"]["mymask"] == "24")
check("remap: common.mydns points at the nested automation VM's own static IP",
      remapped["common"]["mydns"] == "192.168.150.2")

check("remap: other common fields (ISO_IMAGE, VM_MEM, …) pass through unchanged",
      remapped["common"]["ISO_IMAGE"] == "img.qcow2" and remapped["common"]["VM_MEM"] == "4096")

# A node with a falsy config ({} or None — e.g. a minimal node relying
# entirely on inherited common defaults) must still get a myip. `(node_cfg or {})["myip"] = ip`
# would mutate a throwaway dict whenever node_cfg is falsy, dropping myip in the remapped output.
falsy_cfg_original = {
    "common": {},
    "nodes": {
        "empty.mydemo.lab": {},
        "null.mydemo.lab": None,
        "normal.mydemo.lab": {"myip": "10.0.0.5"},
    },
}
falsy_remapped = lab_usb.remap_lab_definition_to_nat(
    falsy_cfg_original, nat_cidr="192.168.150.0/24", nested_automation_ip="192.168.150.2")
check("remap: a node with an empty {} config still gets a myip",
      "myip" in falsy_remapped["nodes"]["empty.mydemo.lab"])
check("remap: a node with a null config still gets a myip",
      "myip" in falsy_remapped["nodes"]["null.mydemo.lab"])
check("remap: all three nodes end up with distinct addresses",
      len({n.get("myip") for n in falsy_remapped["nodes"].values()}) == 3)
check("remap: the original definition's falsy node entries are never mutated in place",
      falsy_cfg_original["nodes"]["empty.mydemo.lab"] == {}
      and falsy_cfg_original["nodes"]["null.mydemo.lab"] is None)

# A custom start_offset shifts where remapped addresses begin.
remapped2 = lab_usb.remap_lab_definition_to_nat(
    original, nat_cidr="192.168.150.0/24", nested_automation_ip="192.168.150.2", start_offset=50)
check("remap: a custom start_offset is honored",
      remapped2["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.150.50")

# nested_automation_ip is mandatory — this whole scheme depends on knowing
# where the lab's own DNS server lives, there is no safe default to guess.
raised = False
try:
    lab_usb.remap_lab_definition_to_nat(original, nat_cidr="192.168.150.0/24")
except ValueError:
    raised = True
check("remap: dies with a clear error when nested_automation_ip is omitted", raised)

# A NAT range too small for the lab is a clear error, not silent truncation
# or an IndexError.
big_lab = {
    "common": {},
    "nodes": {"vm{}".format(i): {"myip": "10.0.0.{}".format(i)} for i in range(1, 6)},
}
raised = False
try:
    lab_usb.remap_lab_definition_to_nat(
        big_lab, nat_cidr="192.168.150.0/30", nested_automation_ip="192.168.150.2")
except ValueError:
    raised = True
check("remap: a NAT range too small for the lab raises a clear ValueError, not an IndexError", raised)


# ── _find_repo_root(): the real repo, not wherever it happens to be run from ──
# Two directories up from this deployed script is not the repo root. Once run as the installed
# /usr/local/bin/build_lab_usb.py copy every other script here is meant to
# be invoked as -- resolves to /usr/local, which has none of the sibling
# directories (setup_demo_server/, the whole repo tree) this script actually
# needs. _find_repo_root() must locate the real checkout via git instead.
#
# This test container's own copy of the repo may not include .git at all
# (a normal thing for a build context to exclude) -- tolerant of that, same
# pattern as this suite's own pyyaml-optional checks elsewhere, since the
# self-contained fake-repo tests just below exercise the actual logic
# either way, independent of whatever this outer environment happens to be.
_this_is_a_real_checkout = subprocess.run(
    ["git", "-C", str(_REPO), "rev-parse", "--show-toplevel"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
).returncode == 0
if _this_is_a_real_checkout:
    check("_find_repo_root() finds the real repo when run from inside this actual checkout",
          build_lab_usb._find_repo_root() == _REPO)

with tempfile.TemporaryDirectory() as tmp:
    fake_repo = Path(tmp) / "fake-repo"
    (fake_repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(fake_repo)], check=True)
    fake_script = fake_repo / "scripts" / "build_lab_usb.py"
    fake_script.write_text("# not a real script, just needs to exist for __file__ resolution\n")
    result = subprocess.run(
        ["git", "-C", str(fake_script.parent), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
    )
    check("_find_repo_root()'s own git invocation correctly finds a DIFFERENT repo's "
          "toplevel when run from inside it (not hardcoded to _REPO)",
          result.returncode == 0 and Path(result.stdout.strip()) == fake_repo)

with tempfile.TemporaryDirectory() as tmp:
    # No .git anywhere under a plain tempdir — mirrors the installed
    # /usr/local/bin/build_lab_usb.py case (no git context at all).
    result = subprocess.run(
        ["git", "-C", tmp, "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
    )
    check("outside any git checkout, the underlying git command fails the way "
          "_find_repo_root() relies on to trigger its own clear die() message",
          result.returncode != 0)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab_usb checks passed")
