#!/usr/bin/env python3
# Unit tests for scripts/install_ansible_playbook — mocked ssh_run, no real SSH.
# Run from 117_ansible_playbook.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402

ap = apps.load_addon_module("install_ansible_playbook", str(_REPO / "scripts"))  # no .py: loaded by path

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
# hostname -> returncode, keyed per-call via a simple responder below
_command_v_rc = 0
_run_rc = 0


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    if cmd.startswith("command -v"):
        return FakeResult(returncode=_command_v_rc)
    if cmd.startswith("ansible-playbook"):
        return FakeResult(returncode=_run_rc)
    return FakeResult(returncode=0)


ap.ssh_run = _fake_ssh_run
died = []
ap.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── _ensure_is_control_node ─────────────────────────────────────────────────
calls.clear()
died.clear()
_command_v_rc = 0
ap._ensure_is_control_node("charon.mydemo.lab")
check("_ensure_is_control_node: ansible-playbook present -> no error", died == [])

died.clear()
_command_v_rc = 1
try:
    ap._ensure_is_control_node("charon.mydemo.lab")
except SystemExit:
    pass
check("_ensure_is_control_node: ansible-playbook missing -> dies, names the fix",
      len(died) == 1 and "ansible_control_node" in died[0])
_command_v_rc = 0


# ── run_playbook: inline content ────────────────────────────────────────────
calls.clear()
ap.run_playbook("charon.mydemo.lab", {"ansible_playbook_content": "- hosts: all\n  tasks: []\n"})
cmds = [c[1] for c in calls]
check("run_playbook(content): stages the playbook under the default dir/name",
      any("/srv/ansible/playbooks/lab_playbook.yml" in c for c in cmds))
check("run_playbook(content): the real content is written via heredoc",
      any("- hosts: all" in c for c in cmds))
check("run_playbook(content): runs ansible-playbook against the staged file",
      any(c.startswith("ansible-playbook") and "/srv/ansible/playbooks/lab_playbook.yml" in c for c in cmds))


# ── run_playbook: local path, custom name/limit/extra-vars/inventory ───────
with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
    f.write("- hosts: webservers\n")
    tmp_path = f.name

calls.clear()
ap.run_playbook(
    "charon.mydemo.lab",
    {"ansible_playbook_path": tmp_path, "ansible_playbook_name": "deploy.yml",
     "ansible_playbook_limit": "webservers", "ansible_playbook_extra_vars": {"version": "1.2.3"}},
    playbook_dir="/custom/dir", inventory_dir="/custom/inv",
)
Path(tmp_path).unlink()
cmds_with_input = [(c, i) for _, c, i in calls]
check("run_playbook(path): reads the REAL local file content",
      any(i == "- hosts: webservers\n" for _, i in cmds_with_input))
check("run_playbook(path): stages under the custom dir/name",
      any("/custom/dir/deploy.yml" in c for c, _ in cmds_with_input))
run_cmd = next(c for c, _ in cmds_with_input if c.startswith("ansible-playbook"))
check("run_playbook: uses the custom inventory dir's dynamic inventory script",
      "-i /custom/inv/uyuni_dynamic_inventory.py" in run_cmd)
check("run_playbook: --limit is passed through", "--limit webservers" in run_cmd)
check("run_playbook: --extra-vars carries the real JSON-encoded dict",
      "--extra-vars" in run_cmd and "version" in run_cmd and "1.2.3" in run_cmd)


# ── run_playbook: url ────────────────────────────────────────────────────────
calls.clear()
ap.run_playbook("charon.mydemo.lab", {"ansible_playbook_url": "https://example.com/p.yml"})
cmds = [c[1] for c in calls]
check("run_playbook(url): fetches the playbook with curl",
      any("curl" in c and "https://example.com/p.yml" in c for c in cmds))


# ── run_playbook: nfs ────────────────────────────────────────────────────────
calls.clear()
nfs_calls = []
ac.fetch_nfs_content = lambda hostname, url: nfs_calls.append((hostname, url)) or "- hosts: nfs-fetched\n"
ap.run_playbook("charon.mydemo.lab", {"ansible_playbook_url": "nfs://nfs1.mydemo.lab/exports/p.yml"})
cmds = [c[1] for c in calls]
check("run_playbook(nfs): fetches via addon_common.fetch_nfs_content, not curl",
      nfs_calls == [("charon.mydemo.lab", "nfs://nfs1.mydemo.lab/exports/p.yml")]
      and not any("curl" in c for c in cmds))
check("run_playbook(nfs): the fetched content is staged via heredoc",
      any("- hosts: nfs-fetched" in c for c in cmds))

# A failing fetch dies.
died.clear()
calls.clear()


def _fake_fetch_fail(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    if cmd.startswith("curl"):
        return FakeResult(returncode=1)
    return FakeResult(returncode=0)


ap.ssh_run = _fake_fetch_fail
try:
    ap.run_playbook("charon.mydemo.lab", {"ansible_playbook_url": "https://example.com/p.yml"})
except SystemExit:
    pass
check("run_playbook(url): a failed curl fetch dies", len(died) == 1)
ap.ssh_run = _fake_ssh_run

# A failing ansible-playbook run dies too.
died.clear()
_run_rc = 1
try:
    ap.run_playbook("charon.mydemo.lab", {"ansible_playbook_content": "- hosts: all\n"})
except SystemExit:
    pass
check("run_playbook: a non-zero ansible-playbook exit dies", len(died) == 1)
_run_rc = 0


# ── _validate ────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []


v = FakeValidator({"ansible_playbook": {}})
ap._validate(v)
check("_validate: none of path/url/content given is an error", any("exactly one" in e for e in v.errors))

v = FakeValidator({"ansible_playbook": {"ansible_playbook_path": "x.yml", "ansible_playbook_url": "https://x"}})
ap._validate(v)
check("_validate: two sources given at once is an error", any("exactly one" in e for e in v.errors))

v = FakeValidator({"ansible_playbook": {"ansible_playbook_content": "- hosts: all\n"}})
ap._validate(v)
check("_validate: exactly one source given has no error", v.errors == [])

v = FakeValidator({"ansible_playbook": {"ansible_playbook_content": "x", "ansible_playbook_extra_vars": "nope"}})
ap._validate(v)
check("_validate: a non-dict extra_vars is an error", any("extra_vars" in e for e in v.errors))

v = FakeValidator({"ansible_playbook": {"ansible_playbook_url": "nfs://nfs1.mydemo.lab/exports/p.yml"}})
ap._validate(v)
check("_validate: an nfs:// ansible_playbook_url is valid, no error", v.errors == [])

v = FakeValidator({"ansible_playbook": {"ansible_playbook_url": "ftp://example.com/p.yml"}})
ap._validate(v)
check("_validate: an unsupported URL scheme is an error",
      any("ansible_playbook_url" in e for e in v.errors))


# ── main(): per-node dispatch, reads ansible_control_node's own dirs ───────
definition = primary.LabDefinition({
    "nodes": {"charon.mydemo.lab": {"addons": ["ansible_control_node", "ansible_playbook"]}},
    "ansible_playbook": {"ansible_playbook_content": "- hosts: all\n"},
    "ansible_control_node": {"ansible_control_node_playbook_dir": "/srv/ansible/playbooks",
                              "ansible_control_node_inventory_dir": "/srv/ansible/inventory"},
}, source_path="/tmp/fake.json", fmt="json")

run_calls = []
ap.run_playbook = lambda hostname, cfg, **kw: run_calls.append((hostname, cfg, kw))
sys.argv = ["install_ansible_playbook", "/tmp/fake.json"]
primary.load_definition = lambda path: definition
ap.main()
check("main(): runs on the node listing ansible_playbook",
      len(run_calls) == 1 and run_calls[0][0] == "charon.mydemo.lab")
check("main(): passes the control node's own playbook/inventory dirs through",
      run_calls[0][2] == {"playbook_dir": "/srv/ansible/playbooks", "inventory_dir": "/srv/ansible/inventory"})


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_ansible_playbook checks passed")
