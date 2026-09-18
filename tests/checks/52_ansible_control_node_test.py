#!/usr/bin/env python3
# Unit tests for scripts/install_ansible_control_node.py — mocked ssh_run/
# ensure_lab_ssh_key, no real SSH. Verifies the real bundled example
# playbook/dynamic-inventory content actually gets pushed (not just that
# *some* SSH call happens), the SSH-key generation + one-hop distribution
# to managed nodes, and main()'s default-to-every-other-lab-node behavior.
# Run from 52_ansible_control_node.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_ansible_control_node as acn  # noqa: E402

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
    calls.append((hostname, cmd))
    if unreachable_targets and hostname in unreachable_targets:
        return FakeResult(returncode=1, stderr="Connection refused")
    return FakeResult(returncode=0)


unreachable_targets = set()
acn.ssh_run = _fake_ssh_run
acn.ensure_lab_ssh_key = lambda hostname, key_path=None, key_comment=None: (
    calls.append((hostname, "ensure_lab_ssh_key {} {}".format(key_path, key_comment)))
    or "ssh-ed25519 AAAAfake ansible-control-node"
)


# ── setup_ansible_control_node: full real-content push ─────────────────────
calls.clear()
templ_addons_loc = str(_REPO / "templates" / "addons")
cfg = {}
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, cfg, ["venus.mydemo.lab", "mars.mydemo.lab"])
cmds = [c[1] for c in calls]

check("setup_ansible_control_node: installs ansible-core",
      any("ansible-core" in c for c in cmds))
check("setup_ansible_control_node: creates the default playbook/inventory dirs",
      any("mkdir -p /srv/ansible/playbooks /srv/ansible/inventory" in c for c in cmds))
check("setup_ansible_control_node: pushes the REAL ping.yml content, not a stub",
      any("Confirm every inventory host is reachable" in c for c in cmds))
check("setup_ansible_control_node: pushes the REAL ensure_packages.yml content",
      any("ansible.builtin.package:" in c for c in cmds))
check("setup_ansible_control_node: pushes the REAL patch_and_reboot.yml content",
      any("community.general.zypper:" in c for c in cmds))
check("setup_ansible_control_node: pushes the REAL dynamic inventory script, not a stub",
      any("systemgroup.listSystemsMinimal" in c for c in cmds))
check("setup_ansible_control_node: makes the inventory script executable",
      any("chmod 755" in c and "uyuni_dynamic_inventory.py" in c for c in cmds))
check("setup_ansible_control_node: generates an SSH keypair on the control node itself",
      any("ensure_lab_ssh_key /root/.ssh/id_ansible_ed25519 ansible-control-node" in c for c in cmds))
check("setup_ansible_control_node: writes ansible.cfg pointing at the generated key",
      any("private_key_file = /root/.ssh/id_ansible_ed25519" in c for c in cmds))
check("setup_ansible_control_node: distributes the control node's key to every managed node",
      any(h == "venus.mydemo.lab" and "authorized_keys" in c for h, c in calls)
      and any(h == "mars.mydemo.lab" and "authorized_keys" in c for h, c in calls))


# ── ansible_control_node_examples: "false" skips the content push ──────────
calls.clear()
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc,
                                {"ansible_control_node_examples": "false"}, [])
cmds = [c[1] for c in calls]
check("setup_ansible_control_node: examples='false' skips pushing playbook/inventory content",
      not any("Confirm every inventory host is reachable" in c for c in cmds)
      and not any("systemgroup.listSystemsMinimal" in c for c in cmds))
check("setup_ansible_control_node: ansible-core install and key setup still happen either way",
      any("ansible-core" in c for c in cmds)
      and any("ensure_lab_ssh_key" in c for c in cmds))


# ── custom playbook_dir/inventory_dir/ssh_key_path are honoured ────────────
calls.clear()
custom_cfg = {
    "ansible_control_node_playbook_dir": "/opt/ansible/pb",
    "ansible_control_node_inventory_dir": "/opt/ansible/inv",
    "ansible_control_node_ssh_key_path": "/root/.ssh/custom_key",
}
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, custom_cfg, [])
cmds = [c[1] for c in calls]
check("setup_ansible_control_node: honours a custom playbook_dir/inventory_dir",
      any("mkdir -p /opt/ansible/pb /opt/ansible/inv" in c for c in cmds))
check("setup_ansible_control_node: honours a custom ssh_key_path",
      any("ensure_lab_ssh_key /root/.ssh/custom_key" in c for c in cmds)
      and any("private_key_file = /root/.ssh/custom_key" in c for c in cmds))


# ── an unreachable managed node warns, doesn't crash the whole run ─────────
calls.clear()
unreachable_targets = {"phobos.mydemo.lab"}
try:
    acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, {},
                                    ["phobos.mydemo.lab", "venus.mydemo.lab"])
    crashed = False
except SystemExit:
    crashed = True
check("setup_ansible_control_node: an unreachable managed node does not abort the whole run",
      not crashed and any(h == "venus.mydemo.lab" and "authorized_keys" in c for h, c in calls))
unreachable_targets = set()


# ── main(): defaults managed_nodes to every OTHER node in the lab ──────────
calls.clear()
acn.primary.load_definition = lambda json_file: {
    "nodes": {"charon.mydemo.lab": {"addons": ["ansible_control_node"]},
              "venus.mydemo.lab": {}, "mars.mydemo.lab": {}},
}
acn.primary.load_defaults = lambda: {"_templ_addons_loc": templ_addons_loc}
old_argv = sys.argv
sys.argv = ["install_ansible_control_node.py", "lab.json"]
try:
    acn.main()
finally:
    sys.argv = old_argv
targets_reached = {h for h, c in calls if "authorized_keys" in c}
check("main(): defaults managed_nodes to every OTHER node in the lab definition, not itself",
      targets_reached == {"venus.mydemo.lab", "mars.mydemo.lab"})


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ansible_control_node checks passed")
