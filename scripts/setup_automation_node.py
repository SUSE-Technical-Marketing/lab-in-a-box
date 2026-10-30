#!/usr/bin/env python3
# Part of lab-in-a-box — builds an automation node on a cloud backend
# Author/s: Raul Mahiques
# License: GPLv3

"""
setup_automation_node.py — build a lab-in-a-box automation node on a cloud provider.

Usage:
    setup_automation_node.py <lab.json> <node_hostname>

The node is an ordinary node of the lab definition, on a cloud backend, so it is created by the same
provisioning path every other node uses: its security group, internet gateway, image resolution,
cloud-init and DNS registration are the ones in libs/backends.py and scripts/setup_vm.py, not a
second implementation of them. What this adds is the step after the VM exists: the node's inputs are
staged and setup_demo_server/automation-node-lib.sh runs on it over SSH, exactly as
setup_lab_automation.sh does for a VM automation node through _node_transport=ssh.

The result is a full automation node (BIND serving the lab domain, Apache, the lab scripts) inside
the provider's own network, so a lab there needs nothing from the home lab to resolve its own names.

Its BIND answers authoritatively for the lab domain from anywhere, and recurses only for the private
ranges (automation-node-lib.sh's _RECURSION_NETWORKS), so a node whose port 53 is reachable from the
internet is not an open resolver. Opening port 53 at all is the lab definition's business: list it in
the node's own aws_open_ports (or the equivalent for another backend).

What this does NOT do: point anything at the new node's DNS. Whoever wants to resolve the lab's names
from elsewhere adds it as a nameserver themselves.
"""

__version__ = "__LABVERSION__"

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import kvm_host_profiles  # noqa: E402
import migrations  # noqa: E402
import primary  # noqa: E402
from lab_creation import die, log, warn, load_vm_vars  # noqa: E402

from setup_vm import provision_vm  # noqa: E402

# setup_demo_server/, beside the repository's scripts/ in a checkout and installed alongside the libs
# otherwise. automation-node-lib.sh and the lab tree it installs both come from here.
_SETUP_DIRS = ("/usr/local/lib/lab_creation/setup_demo_server",
               str(Path(__file__).resolve().parent.parent / "setup_demo_server"))

# The node's own kind, which automation-node-lib.sh branches on: no hypervisor to mount source images
# from and no static network to write, because the provider assigns the address over DHCP.
NODE_KIND = "cloud"
NODE_DIR = "/etc/lab_creation/node"

# This host's public key, authorized on the node so the commands here can reach it afterwards. A
# module constant rather than a literal so a caller, the unit tests included, can point it elsewhere.
ROOT_SSH_PUB_KEY = Path("/root/.ssh/id_rsa.pub")


def setup_dir() -> Path:
    """setup_demo_server/, or die naming where it was looked for."""
    for candidate in _SETUP_DIRS:
        if (Path(candidate) / "automation-node-lib.sh").is_file():
            return Path(candidate)
    die("automation-node-lib.sh not found — looked in: {}".format(", ".join(_SETUP_DIRS)))


def repo_root() -> Path:
    """The repository checkout whose tracked files are staged onto the node."""
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        die("setup_automation_node.py must run from a repository checkout: the node is given the "
            "tracked files of this tree, and '{}' is not one".format(root))
    return root


def node_settings(definition, config, vm_name, private_ip, mydomain) -> dict:
    """
    The values automation-node-lib.sh reads from NODE_DIR/settings (its own NODE_SETTINGS list).

    A cloud node's address, mask and gateway come from the provider, so the ones a KVM automation node
    derives from lab.cfg are not written here. _myip is the private address: it is what the node's own
    zone publishes and what its peers in the same network reach it on. _mydns is the provider's
    resolver, which BIND forwards everything outside the lab domain to.
    """
    key = ROOT_SSH_PUB_KEY
    if not key.is_file():
        die("{} not found — it authorizes this host on the node".format(key))
    return {
        "AUTOMATION_HOSTNAME": vm_name,
        "MYREG": "registry.{}".format(mydomain),
        "_myip": private_ip,
        "_mymask_cidr": "",
        "_mygw": "",
        "_mydns": str(config.get("CLOUD_RESOLVER") or "169.254.169.253"),
        "_mydomain": mydomain,
        "_mynetrev": ".".join(reversed(private_ip.split(".")[:3])),
        "_timezone": str(config.get("_timezone") or "Europe/Zurich"),
        "_virt_srv": "",
        "ROOT_SSH_PUB_KEY": key.read_text().strip(),
        "HOST_SSH_PUB_KEY": "",
        "ROOT_PWD_HASH": str(config.get("ROOT_PWD_HASH") or ""),
        "_host_ip": "",
        "_host_name": "",
        "_host_fqdn": "",
        "myarch": "x86_64",
        "_node_kind": NODE_KIND,
        "_node_netstack": "nm",
        # Left to the node: node_install_packages picks the interpreter its own distribution ships,
        # which is not the same on Leap 15 and Leap 16.
        "_python_bin": str(config.get("_python_bin") or ""),
        "_kubectl_install": kvm_host_profiles.kubectl_binary_script(),
    }


def stage_inputs(stage: Path, settings: dict) -> None:
    """
    Fill `stage` with what the node needs under NODE_DIR: the settings file, automation-node-lib.sh
    and this repository's tracked files.

    The settings file is written by automation-node-lib.sh's own write_node_settings, sourced for the
    purpose, so its format cannot drift from what configure_node reads back.
    """
    lib = setup_dir() / "automation-node-lib.sh"
    exports = "".join("{}={}\n".format(k, shlex.quote(v)) for k, v in settings.items())
    script = ("set -eu\n. {lib}\n{exports}write_node_settings {stage}\n").format(
        lib=shlex.quote(str(lib)), exports=exports, stage=shlex.quote(str(stage)))
    result = subprocess.run(["bash", "-c", script], stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        die("writing the node's settings failed: {}".format((result.stderr or "").strip()))

    node_dir = stage / NODE_DIR.lstrip("/")
    node_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["install", "-m", "0755", str(lib), str(node_dir / "automation-node-lib.sh")],
                   check=True)

    tree = node_dir / "lab-in-a-box"
    tree.mkdir(parents=True, exist_ok=True)
    root = repo_root()
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                           stdout=subprocess.PIPE, check=True).stdout
    tar = subprocess.Popen(["tar", "-C", str(root), "--null", "--ignore-failed-read", "-T", "-",
                            "-cf", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    untar = subprocess.Popen(["tar", "-C", str(tree), "-xf", "-"], stdin=tar.stdout)
    tar.stdin.write(files)
    tar.stdin.close()
    tar.stdout.close()
    if untar.wait() != 0 or tar.wait() != 0:
        die("staging this repository's files for the automation node failed")


def configure_node(host: str, stage: Path) -> None:
    """Copy `stage` onto the node and run automation-node-lib.sh configure there."""
    log("- Copying the configuration into the automation node")
    tar = subprocess.Popen(["tar", "-C", str(stage), "-cf", "-", "."], stdout=subprocess.PIPE)
    push = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "root@" + host, "tar -C / --no-overwrite-dir -xf -"], stdin=tar.stdout)
    tar.stdout.close()
    if push.wait() != 0 or tar.wait() != 0:
        die("copying the configuration into the automation node failed")

    log("- Configuring the automation node (this installs its packages and services)")
    run = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "root@" + host,
         "bash {}/automation-node-lib.sh configure".format(NODE_DIR)], check=False)
    if run.returncode != 0:
        die("configuring the automation node failed (exit {}) — its own output is above".format(
            run.returncode))


def main():
    if primary.wants_help(sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)
    if len(sys.argv) < 3:
        die("Usage:\n{} <configuration file> <node_hostname>".format(sys.argv[0]))

    json_file, vm_name = sys.argv[1], sys.argv[2]
    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)
    migrations.require_current(definition, json_file)

    if vm_name not in (definition.get("nodes") or {}):
        die("'{}' is not a node of '{}' — the automation node is defined as one of its nodes".format(
            vm_name, json_file))

    backend_name = backends.effective_backend_name(definition, config, vm_name)
    if backend_name not in backends.CLOUD_BACKEND_NAMES:
        die("'{}' is on backend '{}' — this builds an automation node on a cloud backend ({}). A "
            "libvirt or Harvester automation node is setup_lab_automation.sh's job.".format(
                vm_name, backend_name, ", ".join(sorted(backends.CLOUD_BACKEND_NAMES))))

    env = load_vm_vars(definition, vm_name)
    mydomain = env.get("mydomain", "")
    if not mydomain:
        die("nodes.{}: no mydomain — the automation node serves a DNS zone and needs its "
            "name".format(vm_name))

    log("Creating the automation node \"{}\" on backend \"{}\"".format(vm_name, backend_name))
    # ensure_cloud_dns=False: this node is the DNS server for its own network. The standard path
    # would create a separate lab-dns VM beside it and point the network at that one instead.
    failures = provision_vm(definition, config, defaults, vm_name, ensure_cloud_dns=False)
    for message in failures or []:
        warn(message)
    if failures:
        die("provisioning '{}' reported failures (above) — not configuring it".format(vm_name))

    backend = backends.get_backend(definition, config, vm_name)
    public_ip = backend.get_ip(vm_name) or ""
    private_ip = backend.get_private_ip(vm_name) or public_ip
    if not public_ip:
        die("backend '{}' reported no address for '{}' after creating it".format(backend_name, vm_name))
    log("- Automation node \"{}\" is up: {} (private {})".format(vm_name, public_ip, private_ip))

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        stage_inputs(stage, node_settings(definition, config, vm_name, private_ip, mydomain))
        configure_node(public_ip, stage)

    # Now that it serves the zone, hand it to the provider's own network, so every instance there
    # resolves this lab through it. Done after configure, not before: pointing the network at a host
    # whose BIND is not running yet would leave the instances already in it without DNS.
    backend.use_dns_server(private_ip, mydomain)

    log("Automation node \"{}\" is ready".format(vm_name))
    log("- It serves DNS for \"{}\" on {} (authoritative from anywhere; it recurses only for "
        "private networks)".format(mydomain, public_ip))
    log("- Add it as a nameserver wherever you want to resolve this lab's names:")
    log("    nameserver {}".format(public_ip))
    log("- Port 53 must be listed in the node's own open ports for that to be reachable.")


if __name__ == "__main__":
    main()
