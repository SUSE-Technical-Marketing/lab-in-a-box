#!/usr/bin/env python3
# Unit tests for scripts/install_run_command — mocked ssh_run, no real SSH.
# Run from 115_run_command.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402

rc = apps.load_addon_module("install_run_command", str(_REPO / "scripts"))  # no .py: loaded by path

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


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    return fake_result


fake_result = FakeResult(returncode=0)
rc.ssh_run = _fake_ssh_run

died = []
rc.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── run_commands(): the shell script built and sent ─────────────────────────
calls.clear()
rc.run_commands("venus.mydemo.lab", ["echo hi", "touch /tmp/x"], shell="bash",
                 env={"FOO": "bar baz"}, stop_on_error=True)
check("run_commands: runs against the given host", calls[0][0] == "venus.mydemo.lab")
script = calls[0][2]
check("run_commands: shebang matches the requested shell", script.startswith("#!/usr/bin/env bash\n"))
check("run_commands: stop_on_error=True sets -e", "set -e" in script)
check("run_commands: env vars are exported and shlex-quoted", "export FOO='bar baz'" in script)
check("run_commands: both commands are present, in order",
      script.index("echo hi") < script.index("touch /tmp/x"))

calls.clear()
rc.run_commands("venus.mydemo.lab", ["echo hi"], stop_on_error=False)
check("run_commands: stop_on_error=False sets +e", "set +e" in calls[0][2])
check("run_commands: default shell is sh", calls[0][2].startswith("#!/usr/bin/env sh\n"))

# A failing remote script dies, not silently swallowed.
died.clear()
fake_result = FakeResult(returncode=1, stdout="boom")
try:
    rc.run_commands("venus.mydemo.lab", ["false"])
except SystemExit:
    pass
check("run_commands: a non-zero exit dies", len(died) == 1)
fake_result = FakeResult(returncode=0)


# ── _classify_source ─────────────────────────────────────────────────────────
check("_classify_source: a multi-line string is inline content",
      rc._classify_source("echo 1\necho 2") == "content")
check("_classify_source: an nfs:// URL is nfs", rc._classify_source("nfs://srv/export/s.sh") == "nfs")
check("_classify_source: an http(s) URL is a url", rc._classify_source("https://example.com/s.sh") == "url")
check("_classify_source: anything else is a local path", rc._classify_source("/some/script.sh") == "path")


# ── run_script: nfs/url/path/content sources ────────────────────────────────
calls.clear()
ac.fetch_nfs_content = lambda hostname, url: "echo from-nfs\n"
rc.run_script("venus.mydemo.lab", "nfs://srv/export/s.sh")
check("run_script(nfs): fetched body lands in the pushed script",
      "echo from-nfs" in calls[0][2])

calls.clear()
fake_result = FakeResult(returncode=0, stdout="echo from-url\n")
rc.run_script("venus.mydemo.lab", "https://example.com/s.sh")
check("run_script(url): curl runs on the target host itself",
      any("curl" in c and "https://example.com/s.sh" in c for h, c, _ in calls))
check("run_script(url): the fetched body lands in the pushed script",
      "echo from-url" in calls[-1][2])
fake_result = FakeResult(returncode=0)

calls.clear()
died.clear()
try:
    rc.run_script("venus.mydemo.lab", "/no/such/script.sh")
except SystemExit:
    pass
check("run_script(path): a nonexistent local file dies with a clear message",
      len(died) == 1 and "/no/such/script.sh" in died[0])

calls.clear()
died.clear()
rc.run_script("venus.mydemo.lab", "echo 1\necho 2\n")
check("run_script(content): inline content is used verbatim as the script body",
      "echo 1" in calls[0][2] and "echo 2" in calls[0][2])


# ── _validate ────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vbool(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and v not in ("true", "false"):
            self.errors.append("[ERROR] bad bool")


v = FakeValidator({"run_command": {}})
rc._validate(v)
check("_validate: missing run_command_commands is an error",
      any("run_command_commands" in e for e in v.errors))

v = FakeValidator({"run_command": {"run_command_commands": ["echo hi"]}})
rc._validate(v)
check("_validate: a valid commands list has no error", v.errors == [])

v = FakeValidator({"run_command": {"run_command_commands": "echo hi"}})
rc._validate(v)
check("_validate: a non-list commands value is an error", v.errors != [])

v = FakeValidator({"run_command": {"run_command_commands": ["echo hi"], "run_command_shell": "zsh"}})
rc._validate(v)
check("_validate: an invalid shell is an error", any("run_command_shell" in e for e in v.errors))

v = FakeValidator({"run_command": {"run_command_commands": ["echo hi"], "run_command_env": "not-a-dict"}})
rc._validate(v)
check("_validate: a non-dict run_command_env is an error", any("run_command_env" in e for e in v.errors))

v = FakeValidator({"run_command": {}})
rc._validate(v)
check("_validate: neither commands nor script given is an error",
      any("exactly one" in e for e in v.errors))

v = FakeValidator({"run_command": {"run_command_commands": ["echo hi"], "run_command_script": "echo 1\n"}})
rc._validate(v)
check("_validate: both commands and script given at once is an error",
      any("exactly one" in e for e in v.errors))

v = FakeValidator({"run_command": {"run_command_script": "echo 1\n"}})
rc._validate(v)
check("_validate: run_command_script alone is valid", v.errors == [])


# ── main(): per-node dispatch via k8s.addon_nodes / addon_node_config ───────
definition = primary.LabDefinition({
    "nodes": {
        "venus.mydemo.lab": {"addons": [{"run_command": {"run_command_commands": ["echo venus"]}}]},
        "mars.mydemo.lab": {"addons": ["run_command"]},
        "earth.mydemo.lab": {"addons": ["rancher"]},
        "mercury.mydemo.lab": {"addons": [{"run_command": {"run_command_script": "echo via-script\n"}}]},
    },
    "run_command": {"run_command_commands": ["echo shared"]},
}, source_path="/tmp/fake.json", fmt="json")

calls.clear()
run_calls = []
script_calls = []
rc.run_commands = lambda hostname, commands, **kw: run_calls.append((hostname, commands))
rc.run_script = lambda hostname, source, **kw: script_calls.append((hostname, source))
sys.argv = ["install_run_command", "/tmp/fake.json"]
primary.load_definition = lambda path: definition
rc.main()
check("main(): runs on every node listing run_command, not earth (which doesn't)",
      {h for h, _ in run_calls} | {h for h, _ in script_calls}
      == {"venus.mydemo.lab", "mars.mydemo.lab", "mercury.mydemo.lab"})
check("main(): a per-node override wins over the shared section",
      dict(run_calls)["venus.mydemo.lab"] == ["echo venus"])
check("main(): a plain-string entry falls back to the shared section",
      dict(run_calls)["mars.mydemo.lab"] == ["echo shared"])
check("main(): a node with run_command_script dispatches to run_script, not run_commands",
      dict(script_calls)["mercury.mydemo.lab"] == "echo via-script\n"
      and "mercury.mydemo.lab" not in dict(run_calls))


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_run_command checks passed")
