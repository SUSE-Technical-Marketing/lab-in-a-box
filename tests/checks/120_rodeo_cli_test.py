#!/usr/bin/env python3
# Unit tests for scripts/install_rodeo_cli — mocked ssh_run, no real SSH.
# Run from 120_rodeo_cli.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402

rc = apps.load_addon_module("install_rodeo_cli", str(_REPO / "scripts"))  # no .py: loaded by path

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


rc.ssh_run = _fake_ssh_run

died = []
rc.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── setup_rodeo_cli(): the happy path ────────────────────────────────────────
calls.clear()
died.clear()
results.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=0)
results["rodeo --version"] = FakeResult(returncode=0, stdout="rodeo-cli 0.7.0\n")
rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
check("setup_rodeo_cli: fetches and runs the install script on the target host",
      any(h == "sol.mydemo.lab" and "curl" in c and "https://example.com/install.sh" in c and "| bash" in c
          for h, c, _ in calls))
check("setup_rodeo_cli: checks for /dev/kvm", any("test -e /dev/kvm" in c for _, c, _ in calls))
check("setup_rodeo_cli: confirms the install with rodeo --version",
      any("rodeo --version" in c for _, c, _ in calls))
check("setup_rodeo_cli: no death on a clean install", died == [])

# Missing /dev/kvm: a warning, not a failure — install still completes.
calls.clear()
died.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=1)
rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
check("setup_rodeo_cli: missing /dev/kvm does not die (install still proceeds)", died == [])

# The install script ran, but rodeo isn't actually usable afterwards: dies.
calls.clear()
died.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=0)
results["rodeo --version"] = FakeResult(returncode=1, stderr="rodeo: command not found")
try:
    rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
except SystemExit:
    pass
check("setup_rodeo_cli: dies if 'rodeo --version' fails after the install",
      len(died) == 1 and "sol.mydemo.lab" in died[0])


# ── check_rodeo_cli() ─────────────────────────────────────────────────────────
class FakeChecker:
    def __init__(self, vm_name):
        self.vm_name = vm_name
        self.problems = []
        self.notes = []

    def ok(self, what):
        self.notes.append(what)

    def fail(self, what, **kw):
        self.problems.append(what)


calls.clear()
results.clear()
results["rodeo --version"] = FakeResult(returncode=0, stdout="rodeo-cli 0.7.0\n")
c = FakeChecker("sol.mydemo.lab")
rc.check_rodeo_cli(c)
check("check_rodeo_cli: ok when rodeo --version succeeds", c.problems == [] and len(c.notes) == 1)

results["rodeo --version"] = FakeResult(returncode=1)
c = FakeChecker("sol.mydemo.lab")
rc.check_rodeo_cli(c)
check("check_rodeo_cli: fails when rodeo --version fails", len(c.problems) == 1)


# ── _validate ─────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vurl_field(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and not str(v).startswith(("http://", "https://")):
            self.errors.append("[ERROR] bad url")


v = FakeValidator({"rodeo_cli": {}})
rc._validate(v)
check("_validate: no install URL set is fine (default applies)", v.errors == [])

v = FakeValidator({"rodeo_cli": {"rodeo_cli_install_url": "ftp://example.com/install.sh"}})
rc._validate(v)
check("_validate: a non-http(s) install URL is an error", v.errors != [])

v = FakeValidator({"rodeo_cli": {"rodeo_cli_install_url": "https://example.com/install.sh"}})
rc._validate(v)
check("_validate: a valid https install URL has no error", v.errors == [])


# ── main(): per-node dispatch, and the default install URL ──────────────────
definition = primary.LabDefinition({
    "nodes": {
        "sol.mydemo.lab": {"addons": ["rodeo_cli"]},
        "luna.mydemo.lab": {"addons": [{"rodeo_cli": {"rodeo_cli_install_url": "https://mirror.lab/install.sh"}}]},
        "earth.mydemo.lab": {"addons": ["rancher"]},
    },
    "rodeo_cli": {},
}, source_path="/tmp/fake.json", fmt="json")

setup_calls = []
rc.setup_rodeo_cli = lambda hostname, install_url: setup_calls.append((hostname, install_url))
sys.argv = ["install_rodeo_cli", "/tmp/fake.json"]
primary.load_definition = lambda path: definition
rc.main()
check("main(): runs on every node listing rodeo_cli, not earth (which doesn't)",
      {h for h, _ in setup_calls} == {"sol.mydemo.lab", "luna.mydemo.lab"})
check("main(): the shared section with no override uses the default install URL",
      dict(setup_calls)["sol.mydemo.lab"] == rc.DEFAULT_INSTALL_URL)
check("main(): a per-node override wins over the default install URL",
      dict(setup_calls)["luna.mydemo.lab"] == "https://mirror.lab/install.sh")


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_rodeo_cli checks passed")
