#!/usr/bin/env python3
# Mocked unit tests for scripts/destroy_lab.py; destroy_vm() and lab_creation.purge_known_host()
# (ssh-keygen known-hosts cleanup — not necessarily installed in the test
# container) are monkeypatched. Verifies per-node dispatch across the whole
# lab and that one node's destroy failure never blocks the rest. Run from
# 16_destroy_lab.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import destroy_lab  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


keygen_calls = []


def _fake_purge_known_host(*names):
    keygen_calls.append(names)


destroy_lab.lab_creation.purge_known_host = _fake_purge_known_host

definition = {
    "common": {"lab_name": "test-lab"},
    "nodes": {"vm1": {}, "vm2": {}, "vm3": {}},
}
config = {}
defaults = {}

# ── Every node is torn down; a failure on one doesn't block the rest ────────
processed = []


def _flaky_destroy(definition, config, defaults, vm_name):
    processed.append(vm_name)
    if vm_name == "vm2":
        raise RuntimeError("simulated destroy failure")
    if vm_name == "vm3":
        raise SystemExit(1)


destroy_lab.destroy_vm = _flaky_destroy
destroy_lab.destroy_lab(definition, config, defaults, "lab.json")

check("destroy_lab: every node is attempted despite mid-loop failures",
      processed == ["vm1", "vm2", "vm3"])
check("destroy_lab: ssh-keygen known-hosts cleanup issued once per node",
      len(keygen_calls) == 3)
check("destroy_lab: known-hosts cleanup targets each vm_name",
      all(any(vm in " ".join(call) for call in keygen_calls) for vm in ("vm1", "vm2", "vm3")))

# ── The happy path: every node destroyed cleanly, nothing raised ───────────
processed.clear()
keygen_calls.clear()
destroy_lab.destroy_vm = lambda definition, config, defaults, vm_name: processed.append(vm_name)
try:
    destroy_lab.destroy_lab(definition, config, defaults, "lab.json")
    ok = True
except Exception:
    ok = False
check("destroy_lab: happy path processes all nodes without raising",
      ok and processed == ["vm1", "vm2", "vm3"])


# ── main(): --version / missing-args ────────────────────────────────────────
import io
from contextlib import redirect_stdout

old_argv = sys.argv
sys.argv = ["destroy_lab.py", "--version"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --version: exits 0 and prints the version", code == 0 and "destroy_lab.py" in buf.getvalue())

sys.argv = ["destroy_lab.py"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        destroy_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main: missing lab.json argument prints usage and exits 1", code == 1 and "Usage" in buf.getvalue())


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all destroy_lab checks passed")
