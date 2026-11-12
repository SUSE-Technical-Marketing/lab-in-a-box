#!/usr/bin/env python3
# Unit tests for scripts/install_lab_in_a_box and libs/lab_in_a_box_common.py —
# mocked ssh_run, no real SSH. Run from 121_lab_in_a_box.sh, in its own
# container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import lab_in_a_box_common as lic  # noqa: E402
import primary  # noqa: E402

rc = apps.load_addon_module("install_lab_in_a_box", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
results = {}


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    for marker, result in results.items():
        if marker in cmd:
            return result
    return FakeResult(returncode=0)


lic.ssh_run = _fake_ssh_run
rc.ssh_run = _fake_ssh_run


# ── lab_in_a_box_common.lab_in_a_box_installed() ─────────────────────────────
calls.clear()
results.clear()
results["test -x /usr/local/bin/setup_lab.py"] = FakeResult(returncode=0)
check("lab_in_a_box_installed: true when the marker file is executable",
      lic.lab_in_a_box_installed("sol.mydemo.lab") is True)

results["test -x /usr/local/bin/setup_lab.py"] = FakeResult(returncode=1)
check("lab_in_a_box_installed: false otherwise",
      lic.lab_in_a_box_installed("sol.mydemo.lab") is False)


# ── lab_in_a_box_common.install_lab_in_a_box() ───────────────────────────────
calls.clear()
lic.install_lab_in_a_box("sol.mydemo.lab")
check("install_lab_in_a_box: fetches and runs install_demo_server_scripts.sh",
      any(h == "sol.mydemo.lab" and "curl" in c and lic.DEFAULT_INSTALL_URL in c for h, c, _ in calls))
check("install_lab_in_a_box: defaults to --fetch-only (non-destructive)",
      any("--fetch-only" in c for _, c, _ in calls))

calls.clear()
lic.install_lab_in_a_box("sol.mydemo.lab", "https://mirror.lab/install_demo_server_scripts.sh", ["-y", "--name", "a b"])
check("install_lab_in_a_box: a custom URL and args are used, each arg quoted individually",
      any(h == "sol.mydemo.lab" and "https://mirror.lab/install_demo_server_scripts.sh" in c
          and "-y --name 'a b'" in c for h, c, _ in calls))


# ── check_lab_in_a_box() ──────────────────────────────────────────────────────
class FakeChecker:
    def __init__(self, vm_name):
        self.vm_name = vm_name
        self.problems = []
        self.notes = []

    def ok(self, what):
        self.notes.append(what)

    def fail(self, what, **kw):
        self.problems.append(what)


results.clear()
results["test -x /usr/local/bin/setup_lab.py"] = FakeResult(returncode=0)
c = FakeChecker("sol.mydemo.lab")
rc.check_lab_in_a_box(c)
check("check_lab_in_a_box: ok when installed", c.problems == [] and len(c.notes) == 1)

results["test -x /usr/local/bin/setup_lab.py"] = FakeResult(returncode=1)
c = FakeChecker("sol.mydemo.lab")
rc.check_lab_in_a_box(c)
check("check_lab_in_a_box: fails when not installed", len(c.problems) == 1)


# ── _validate ─────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vurl_field(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and not str(v).startswith(("http://", "https://")):
            self.errors.append("[ERROR] bad url")


v = FakeValidator({"lab_in_a_box": {}})
rc._validate(v)
check("_validate: nothing set is fine (defaults apply)", v.errors == [])

v = FakeValidator({"lab_in_a_box": {"lab_in_a_box_args": "not-a-list"}})
rc._validate(v)
check("_validate: lab_in_a_box_args must be a list of strings", v.errors != [])

v = FakeValidator({"lab_in_a_box": {"lab_in_a_box_args": ["-y"]}})
rc._validate(v)
check("_validate: a real list of strings is fine", v.errors == [])

v = FakeValidator({"lab_in_a_box": {"lab_in_a_box_install_url": "ftp://example.com/x.sh"}})
rc._validate(v)
check("_validate: a non-http(s) install URL is an error", v.errors != [])


# ── main(): per-node dispatch, idempotent skip, per-node overrides ──────────
definition = primary.LabDefinition({
    "nodes": {
        "sol.mydemo.lab": {"addons": ["lab_in_a_box"]},
        "luna.mydemo.lab": {"addons": [{"lab_in_a_box": {"lab_in_a_box_args": ["-y"]}}]},
        "earth.mydemo.lab": {"addons": ["rancher"]},
    },
    "lab_in_a_box": {},
}, source_path="/tmp/fake.json", fmt="json")

install_calls = []
rc.install_lab_in_a_box = lambda hostname, install_url, args: install_calls.append((hostname, install_url, args))
rc.lab_in_a_box_installed = lambda hostname: False  # nobody has it yet in this run
primary.load_definition = lambda path: definition
sys.argv = ["install_lab_in_a_box", "/tmp/fake.json"]
rc.main()
check("main(): runs on every node listing lab_in_a_box, not earth (which doesn't)",
      {h for h, _, _ in install_calls} == {"sol.mydemo.lab", "luna.mydemo.lab"})
check("main(): the shared section with no override uses the default install URL and args",
      dict((h, (u, a)) for h, u, a in install_calls)["sol.mydemo.lab"] == (lic.DEFAULT_INSTALL_URL, None))
check("main(): a per-node lab_in_a_box_args override reaches install_lab_in_a_box",
      dict((h, (u, a)) for h, u, a in install_calls)["luna.mydemo.lab"] == (lic.DEFAULT_INSTALL_URL, ["-y"]))

install_calls.clear()
rc.lab_in_a_box_installed = lambda hostname: True  # now pretend everyone already has it
rc.main()
check("main(): already-installed nodes are skipped entirely (idempotent)", install_calls == [])


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_lab_in_a_box checks passed")
