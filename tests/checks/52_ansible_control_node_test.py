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
    calls.append((hostname, cmd, input_text))
    if unreachable_targets and hostname in unreachable_targets:
        return FakeResult(returncode=1, stderr="Connection refused")
    return FakeResult(returncode=0)


unreachable_targets = set()
acn.ssh_run = _fake_ssh_run
acn.ensure_lab_ssh_key = lambda hostname, key_path=None, key_comment=None: (
    calls.append((hostname, "ensure_lab_ssh_key {} {}".format(key_path, key_comment), None))
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
      any(h == "venus.mydemo.lab" and "authorized_keys" in c for h, c, _ in calls)
      and any(h == "mars.mydemo.lab" and "authorized_keys" in c for h, c, _ in calls))


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
      not crashed and any(h == "venus.mydemo.lab" and "authorized_keys" in c for h, c, _ in calls))
unreachable_targets = set()


# ── _find_uyuni_server / _write_inventory_env: the credential-env-file fix ──
# Added 2026-09-18 after a real, live-reported failure: SMLM's own
# "Ansible > Schedule Playbook" feature invokes the dynamic inventory
# script via a Salt state running under salt-minion's own process
# environment, which never inherits an operator's `export`ed shell vars —
# the script died every time SMLM itself triggered it. Fix: write the
# credentials to a file on the control node the script reads as a fallback.
calls.clear()
smlm_definition = {
    "smlm": {"smlm_admin_user": "admin", "smlm_admin_pass": "1234"},
    "nodes": {"sol.mydemo.lab": {"addons": ["smlm"]}, "charon.mydemo.lab": {}},
}
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, {}, [], definition=smlm_definition)
cmds = [c[1] for c in calls]
env_writes = [c[2] for c in calls if c[1].startswith("mkdir -p /etc/ansible &&") and c[2]]
check("setup_ansible_control_node: with an 'smlm' node present, writes the real inventory "
      "env file with the real UYUNI_HOST/USER/PASS",
      any("UYUNI_HOST=sol.mydemo.lab" in c and "UYUNI_USER=admin" in c and "UYUNI_PASS=1234" in c
          for c in env_writes))
check("setup_ansible_control_node: the inventory env file is always UYUNI_VERIFY_SSL=false "
      "(this lab's own self-signed cert)",
      any("UYUNI_VERIFY_SSL=false" in c for c in env_writes))
check("setup_ansible_control_node: the env file is written chmod 600 (real credentials)",
      any("chmod 600" in c for c in cmds if "uyuni_inventory.env" in c))

calls.clear()
no_smlm_definition = {"nodes": {"charon.mydemo.lab": {}}}
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, {}, [], definition=no_smlm_definition)
check("setup_ansible_control_node: with no smlm/uyuni node in the lab, writes NO env file at all",
      not any(c.startswith("mkdir -p /etc/ansible &&") for _, c, _i in calls))

calls.clear()
acn.setup_ansible_control_node("charon.mydemo.lab", templ_addons_loc, {}, [])  # definition omitted entirely
check("setup_ansible_control_node: definition omitted entirely (back-compat) -> no env file, no crash",
      not any(c.startswith("mkdir -p /etc/ansible &&") for _, c, _i in calls))


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
targets_reached = {h for h, c, _ in calls if "authorized_keys" in c}
check("main(): defaults managed_nodes to every OTHER node in the lab definition, not itself",
      targets_reached == {"venus.mydemo.lab", "mars.mydemo.lab"})


# ── uyuni_dynamic_inventory.py's own logic: group-name sanitization ────────
# Real bug found live 2026-09-19: this lab's own real SMLM system groups
# include hyphens (e.g. "galilean-moons", "gas-giants") — Ansible group
# names may only contain letters/digits/underscore, so using them verbatim
# triggered "[WARNING]: Invalid characters were found in group names but
# not replaced" on stderr, which SMLM's own "Schedule Playbook" Salt-state
# wrapper treats as a hard failure even though the inventory itself parsed
# fine. Imported directly (a plain, dependency-free script) rather than
# only checked via its pushed content above.
_inv_dir = _REPO / "templates" / "addons" / "ansible_control_node" / "inventory"
sys.path.insert(0, str(_inv_dir))
import uyuni_dynamic_inventory as udi  # noqa: E402

check("_sanitize_group_name: strips hyphens to underscores",
      udi._sanitize_group_name("galilean-moons") == "galilean_moons")
check("_sanitize_group_name: a clean name passes through unchanged",
      udi._sanitize_group_name("terrestrial_planets_already_clean") == "terrestrial_planets_already_clean")
check("_sanitize_group_name: a name colliding with a reserved Ansible group gets suffixed",
      udi._sanitize_group_name("all") == "all_group" and udi._sanitize_group_name("ungrouped") == "ungrouped_group")


class _FakeProxy:
    """Mocks the xmlrpc.client.ServerProxy surface build_inventory() calls."""
    def __init__(self, systems, groups_and_members):
        self._systems = systems
        self._groups_and_members = groups_and_members  # {group_name: [member_names]}

        class _Auth:
            def login(self_, user, password):
                return "sesskey"

            def logout(self_, session):
                pass
        self.auth = _Auth()

        class _System:
            def listSystems(self_, session):
                return systems
        self.system = _System()

        outer = self

        class _SystemGroup:
            def listAllGroups(self_, session):
                return [{"name": g} for g in outer._groups_and_members]

            def listSystemsMinimal(self_, session, group_name):
                return [{"name": n} for n in outer._groups_and_members.get(group_name, [])]
        self.systemgroup = _SystemGroup()


udi.os.environ["UYUNI_HOST"] = "sol.mydemo.lab"
udi.os.environ["UYUNI_USER"] = "admin"
udi.os.environ["UYUNI_PASS"] = "1234"
udi.xmlrpc.client.ServerProxy = lambda *a, **kw: _FakeProxy(
    systems=[{"name": "europa.mydemo.lab", "id": 1}, {"name": "io.mydemo.lab", "id": 2}],
    groups_and_members={
        "galilean-moons": ["europa.mydemo.lab", "io.mydemo.lab"],
        "gas-giants": [],
    })
inv = udi.build_inventory()
check("build_inventory(): hyphenated SMLM group names come out sanitized as real inventory keys",
      "galilean_moons" in inv and "gas_giants" in inv)
check("build_inventory(): the ORIGINAL hyphenated names never appear as inventory keys",
      "galilean-moons" not in inv and "gas-giants" not in inv)
check("build_inventory(): sanitized group correctly lists its real members",
      set(inv["galilean_moons"]["hosts"]) == {"europa.mydemo.lab", "io.mydemo.lab"})
check("build_inventory(): 'all'.children lists the SANITIZED names, not the originals",
      "galilean_moons" in inv["all"]["children"] and "galilean-moons" not in inv["all"]["children"])

# Two different SMLM group names that sanitize to the SAME Ansible group name -> merged, not dropped.
udi.xmlrpc.client.ServerProxy = lambda *a, **kw: _FakeProxy(
    systems=[{"name": "a.lab", "id": 1}, {"name": "b.lab", "id": 2}],
    groups_and_members={"gas-giants": ["a.lab"], "gas_giants": ["b.lab"]})
inv2 = udi.build_inventory()
check("build_inventory(): two SMLM groups colliding after sanitization are MERGED, not one dropped",
      set(inv2["gas_giants"]["hosts"]) == {"a.lab", "b.lab"})
check("build_inventory(): a merged collision only appears ONCE in 'all'.children",
      inv2["all"]["children"].count("gas_giants") == 1)


# ── the control node's own entry gets ansible_connection=local ─────────────
# Real bug found live 2026-09-19: SMLM's own "Schedule Playbook" run
# against the full inventory tried to SSH to charon.mydemo.lab (the
# control node itself) as just another target and failed outright
# ("Permission denied") — install_ansible_control_node.py only ever
# installs the control node's own SSH key on every OTHER lab node, never
# on itself. Standard Ansible fix: mark whichever host matches this
# script's own local FQDN as ansible_connection=local.
udi.socket.getfqdn = lambda: "charon.mydemo.lab"
udi.xmlrpc.client.ServerProxy = lambda *a, **kw: _FakeProxy(
    systems=[{"name": "charon.mydemo.lab", "id": 7}, {"name": "venus.mydemo.lab", "id": 8}],
    groups_and_members={})
inv3 = udi.build_inventory()
check("build_inventory(): the control node's own hostvars get ansible_connection=local",
      inv3["_meta"]["hostvars"]["charon.mydemo.lab"].get("ansible_connection") == "local")
check("build_inventory(): every OTHER host is untouched — no ansible_connection set at all",
      "ansible_connection" not in inv3["_meta"]["hostvars"]["venus.mydemo.lab"])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ansible_control_node checks passed")
