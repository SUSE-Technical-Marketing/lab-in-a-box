#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will provision a node as an Ansible control
# node: installs ansible-core, pushes real example playbooks + a dynamic
# inventory script that queries SMLM/Uyuni's own system list, and sets up
# SSH access from this node to the rest of the lab so those playbooks can
# actually reach targets. Distinct from — and complementary to — the
# SMLM/Uyuni-side "Ansible Control Node" add-on entitlement (smlm_
# ansible_control_nodes / uyuni_ansible_control_nodes, see libs/
# spacecmd_common.py's ensure_ansible_control_node()): that enables the
# server to recognise this system as a valid control node and installs
# the ansible package via highstate; THIS script is what actually puts
# real playbook/inventory content on its filesystem and lets it reach
# other lab nodes — the server-side API has no method to do either (see
# spacecmd_common.py's own module docstring).
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "ansible_control_node" — configurable keys:
#   ansible_control_node_playbook_dir  : [OPTIONAL] where example/pushed playbooks land
#                                         (default: /srv/ansible/playbooks)
#   ansible_control_node_inventory_dir : [OPTIONAL] where the dynamic inventory script
#                                         (and any static inventory files) land
#                                         (default: /srv/ansible/inventory)
#   ansible_control_node_examples      : [OPTIONAL] "true"/"false" — push this project's
#                                         own bundled example playbooks (ping.yml,
#                                         ensure_packages.yml, patch_and_reboot.yml) and
#                                         the real Uyuni/SMLM dynamic inventory script
#                                         (default: true)
#   ansible_control_node_ssh_key_path  : [OPTIONAL] path (on the control node itself) for
#                                         the SSH keypair this node uses to reach managed
#                                         targets (default: /root/.ssh/id_ansible_ed25519)
#   ansible_control_node_managed_nodes : [OPTIONAL] list of hostnames to install this
#                                         control node's own public key into (so playbooks
#                                         can actually reach them via SSH — the real docs'
#                                         own "Establishing Communication with Ansible
#                                         Nodes" step, automated here using this
#                                         automation VM's OWN already-existing SSH access
#                                         to every lab node — a one-hop distribution, not
#                                         reusing ensure_lab_ssh_key/distribute_lab_ssh_key,
#                                         which are a two-hop mechanism for a different
#                                         feature, USB-delivered labs). Default: every
#                                         OTHER node in this lab definition.
#
# Target node(s): any node listing "ansible_control_node" in its own addons[] list — same
# dispatch shape as install_client_registration.py. Register the same node with SMLM/Uyuni
# too (client_registration addon) and set smlm_ansible_control_nodes/uyuni_
# ansible_control_nodes (see those install scripts' own schema docs) for the full,
# real "Setup Ansible Control Node" workflow documentation.suse.com describes.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "ansible_control_node",
    "targets": ["vm", "baremetal"],
    "layers": ["os-native"],
    "requires_kubernetes": None,
    "aux_services": [],
}

import os
import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, ensure_lab_ssh_key  # noqa: E402


def _validate(v):
    v.vns("ansible_control_node")


def _distribute_pubkey(pubkey, target_hosts):
    """
    Installs `pubkey` into every target's authorized_keys — idempotent
    (skips a target that already has the exact line). One-hop: runs
    directly from wherever THIS script executes (the automation VM, which
    already has root SSH to every lab node), unlike libs/lab_creation.py's
    own ensure_lab_ssh_key/distribute_lab_ssh_key pair, a two-hop
    mechanism built for a different feature (USB-delivered labs, reached
    only through their own nested lab-host VM) — reusing that pair here
    would conflate two unrelated SSH keys/topologies.
    """
    for target in target_hosts:
        remote_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "grep -qxF {key} ~/.ssh/authorized_keys 2>/dev/null || "
            "echo {key} >> ~/.ssh/authorized_keys"
        ).format(key=shlex.quote(pubkey))
        r = ssh_run(target, remote_cmd, check=False)
        if r.returncode != 0:
            print("  WARNING: could not install the control node's SSH key on '{}' — "
                  "not reachable, or root SSH not yet set up there".format(target))
        else:
            print("  Installed the control node's SSH key on '{}'".format(target))


def _push_examples(hostname, templ_addons_loc, playbook_dir, inventory_dir):
    """Pushes this project's own bundled example playbooks + the real Uyuni/SMLM dynamic
    inventory script — static content, no templating needed (the inventory script reads
    its own UYUNI_HOST/USER/PASS from the environment at run time, never baked in here)."""
    base = Path(str(templ_addons_loc).rstrip("/")) / "ansible_control_node"

    for playbook in ("ping.yml", "ensure_packages.yml", "patch_and_reboot.yml"):
        content = (base / "playbooks" / playbook).read_text()
        ssh_run(hostname, "cat > {}/{} <<'LAB_IN_A_BOX_EOF'\n{}LAB_IN_A_BOX_EOF".format(
            playbook_dir, playbook, content))
    print("  Pushed 3 example playbooks to {}".format(playbook_dir))

    inventory_script = (base / "inventory" / "uyuni_dynamic_inventory.py").read_text()
    inventory_path = "{}/uyuni_dynamic_inventory.py".format(inventory_dir)
    ssh_run(hostname, "cat > {} <<'LAB_IN_A_BOX_EOF'\n{}LAB_IN_A_BOX_EOF".format(
        inventory_path, inventory_script))
    ssh_run(hostname, "chmod 755 {}".format(shlex.quote(inventory_path)))
    print("  Pushed the Uyuni/SMLM dynamic inventory script to {}".format(inventory_path))


def setup_ansible_control_node(hostname, templ_addons_loc, cfg, managed_nodes):
    playbook_dir = cfg.get("ansible_control_node_playbook_dir") or "/srv/ansible/playbooks"
    inventory_dir = cfg.get("ansible_control_node_inventory_dir") or "/srv/ansible/inventory"
    key_path = cfg.get("ansible_control_node_ssh_key_path") or "/root/.ssh/id_ansible_ed25519"
    push_examples = (cfg.get("ansible_control_node_examples") or "true").lower() != "false"

    print("- Installing ansible-core (if not already present via SMLM's own highstate)")
    ssh_run(hostname, "command -v ansible-playbook >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y ansible-core 2>/dev/null || "
                       "apt-get install -y ansible-core 2>/dev/null || "
                       "dnf install -y ansible-core 2>/dev/null)", check=False)

    ssh_run(hostname, "mkdir -p {} {}".format(shlex.quote(playbook_dir), shlex.quote(inventory_dir)))

    if push_examples:
        _push_examples(hostname, templ_addons_loc, playbook_dir, inventory_dir)

    print("- Ensuring this control node has its own SSH keypair for reaching managed nodes")
    pubkey = ensure_lab_ssh_key(hostname, key_path=key_path, key_comment="ansible-control-node")

    ssh_run(hostname, "mkdir -p /etc/ansible")
    ansible_cfg = (
        "[defaults]\n"
        "inventory = {inventory_dir}\n"
        "private_key_file = {key_path}\n"
        "host_key_checking = False\n"
    ).format(inventory_dir=inventory_dir, key_path=key_path)
    ssh_run(hostname, "cat > /etc/ansible/ansible.cfg <<'LAB_IN_A_BOX_EOF'\n{}LAB_IN_A_BOX_EOF".format(ansible_cfg))

    if managed_nodes:
        print("- Distributing this control node's SSH key to {} managed node(s)".format(len(managed_nodes)))
        _distribute_pubkey(pubkey, managed_nodes)
    else:
        print("- No managed nodes to distribute the SSH key to (empty lab, or this is the only node)")

    print("Ansible control node ready on '{}'. Try:".format(hostname))
    print("  ssh root@{} ansible all -m ping -i {}/uyuni_dynamic_inventory.py".format(
        hostname, inventory_dir))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    cfg = definition.get("ansible_control_node", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "ansible_control_node", vm_name=env_vm_name):
        managed_nodes = cfg.get("ansible_control_node_managed_nodes")
        if managed_nodes is None:
            managed_nodes = [n for n in definition.get("nodes", {}) if n != vm_name]
        setup_ansible_control_node(vm_name, templ_addons_loc, cfg, managed_nodes)


if __name__ == "__main__":
    main()
