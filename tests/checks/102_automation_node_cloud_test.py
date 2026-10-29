#!/usr/bin/env python3
# Unit tests for the cloud automation node: scripts/setup_automation_node.py's own derivations and
# refusals, and setup_demo_server/automation-node-lib.sh's "cloud" node kind — its BIND policy (the
# part that keeps a publicly reachable port 53 from being an open resolver), the KVM-host records it
# must omit, and the hypervisor source-image mount it must not write. Nothing here reaches a cloud
# account or a network. Run from 102_automation_node_cloud.sh, in its own container — see
# tests/run_tests.sh.
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

_LIB = _REPO / "setup_demo_server" / "automation-node-lib.sh"

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _module():
    spec = importlib.util.spec_from_file_location(
        "setup_automation_node", _REPO / "scripts" / "setup_automation_node.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


san = _module()

# The host key this would authorize on the node is not present in a test container, and its contents
# are not what is under test here; only that it reaches the settings.
_key = tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False)
_key.write("ssh-ed25519 AAAAtest automation@test\n")
_key.close()
san.ROOT_SSH_PUB_KEY = Path(_key.name)


# ── the settings it hands automation-node-lib.sh ──────────────────────────
# write_node_settings only writes the names in NODE_SETTINGS, so a key we invent is silently dropped
# and a key we forget is silently empty. Both are real failure modes, so the sets must match exactly.
NODE_SETTINGS = set(re.search(r'NODE_SETTINGS="(.*?)"', _LIB.read_text(), re.S).group(1).split())
settings = san.node_settings({}, {}, "automation.mydemoaws.lab", "10.0.1.50", "mydemoaws.lab")
check("the settings are exactly automation-node-lib.sh's own NODE_SETTINGS",
      set(settings) == NODE_SETTINGS)
check("the node kind is 'cloud', which is what the lib branches on",
      settings["_node_kind"] == "cloud" and san.NODE_KIND == "cloud")
check("_myip is the private address, which is what the node's own zone publishes",
      settings["_myip"] == "10.0.1.50")
check("the reverse zone is derived from that address",
      settings["_mynetrev"] == "1.0.10")
check("no KVM host is claimed, since a cloud node has none",
      not settings["_host_ip"] and not settings["_host_name"] and not settings["_host_fqdn"]
      and not settings["_virt_srv"])
check("no static address, mask or gateway is written: the provider assigns them",
      not settings["_mymask_cidr"] and not settings["_mygw"])
check("DNS forwarding goes to the provider's own resolver by default",
      settings["_mydns"] == "169.254.169.253")
# Leap 15 and Leap 16 do not agree on the Python package names, Leap 16 packages no kubernetes client
# and neither needs ipcalc (only the archived bash used it). The node picks its own interpreter, so
# nothing here may pin one.
check("the interpreter is left for the node's own distribution to decide",
      settings["_python_bin"] == "")
check("the pinned kubectl installer is handed to the node, for a distribution with no client package",
      "dl.k8s.io/release/v" in settings["_kubectl_install"]
      and "/usr/local/bin/kubectl" in settings["_kubectl_install"])
check("it is the same script the container image is built with, so the version is pinned once",
      settings["_kubectl_install"] == __import__("kvm_host_profiles").kubectl_binary_script())

lib_pkgs = _LIB.read_text()
check("Leap 16's own Python packages are named",
      "_LEAP16_PACKAGES=\"python313 python313-cryptography python313-PyYAML\"" in lib_pkgs)
check("Leap 15's are still named, so an existing automation VM is unchanged",
      "python311" in lib_pkgs and "kubernetes1.35-client" in lib_pkgs)
check("the package set is chosen by the node's own VERSION_ID",
      "_version=\"$(. /etc/os-release && echo \"${VERSION_ID%%.*}\")\"" in lib_pkgs)
check("no package list installs ipcalc, which nothing outside the archived bash calls",
      not any("ipcalc" in re.search(r'^{}="([^"]*)"'.format(name), lib_pkgs, re.M).group(1)
              for name in ("_VM_PACKAGES", "_LEAP15_PACKAGES", "_LEAP16_PACKAGES")))
check("kubectl is installed from the pinned binary when the distribution packages none",
      'if ! command -v kubectl >/dev/null && [[ -n "${_kubectl_install}" ]]; then' in lib_pkgs)

check("this host's public key is passed through for the node to authorize",
      settings["ROOT_SSH_PUB_KEY"] == "ssh-ed25519 AAAAtest automation@test")
check("no KVM host key is claimed, since there is no KVM host",
      settings["HOST_SSH_PUB_KEY"] == "")
check("the lab domain and the hostname come from the lab definition",
      settings["_mydomain"] == "mydemoaws.lab"
      and settings["AUTOMATION_HOSTNAME"] == "automation.mydemoaws.lab")


# ── automation-node-lib.sh: the cloud node's BIND ─────────────────────────
def _write_dns_files(host_name="", host_ip="", host_fqdn=""):
    """Run write_dns_files from the real library into a temporary root and return that root."""
    root = tempfile.mkdtemp()
    script = (
        '. {lib}\n'
        'AUTOMATION_HOSTNAME=automation.mydemoaws.lab MYREG=registry.mydemoaws.lab\n'
        '_myip=10.0.1.50 _mydomain=mydemoaws.lab _mynetrev=1.0.10 _mydns=169.254.169.253\n'
        '_host_name={hn} _host_ip={hi} _host_fqdn={hf}\n'
        'write_dns_files {root}\n'
    ).format(lib=_LIB, hn=host_name or "''", hi=host_ip or "''", hf=host_fqdn or "''", root=root)
    result = subprocess.run(["bash", "-c", script], stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0, result.stderr
    return Path(root)


root = _write_dns_files()
named_conf = (root / "etc/named.conf").read_text()
zone = (root / "var/lib/named/mydemoaws.lab.lan").read_text()
reverse = (root / "var/lib/named/1.0.10.db").read_text()

# The whole point of the hardening: a cloud automation node's port 53 is reachable from the internet,
# and a resolver that recurses for anyone is usable for amplification attacks. Authoritative answers
# for the lab's own zone stay open, which is all that resolving a lab VM by name needs.
check("BIND answers queries from anywhere, so the lab's names resolve externally",
      "allow-query { any; };" in named_conf)
check("BIND recurses only for private networks, so it is not an open resolver",
      "allow-recursion {" in named_conf
      and all(n in named_conf.split("allow-recursion {")[1].split("}")[0]
              for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")))
check("the cache is not open either",
      "allow-query-cache {" in named_conf and "0.0.0.0/0" not in named_conf)
check("responses are rate-limited", "rate-limit {" in named_conf)
check("no zone transfers and no version banner",
      "allow-transfer { none; };" in named_conf and "version none;" in named_conf)
check("it no longer recurses for the whole internet (the old allow-query)",
      "allow-query { 127.0.0.1; 0.0.0.0/0; };" not in named_conf)

check("the forward zone carries the automation node's own record",
      "automation         IN  A       10.0.1.50" in zone)
check("no empty KVM-host A record is written, which named would refuse the zone over",
      not re.search(r"^\s+IN\s+A\s*$", zone, re.M))
check("no empty KVM-host PTR record either",
      not re.search(r"^\s+IN\s+PTR\s*$", reverse, re.M))

with_host = _write_dns_files("nuc6", "192.168.88.6", "nuc6.mydemo.lab")
check("a KVM host, when there is one, still gets its A record",
      "nuc6         IN  A       192.168.88.6" in
      (with_host / "var/lib/named/mydemoaws.lab.lan").read_text())
check("and its PTR record",
      "6      IN  PTR     nuc6.mydemo.lab." in
      (with_host / "var/lib/named/1.0.10.db").read_text())


# ── the hypervisor source-image mount a cloud node must not get ───────────
def _write_sshfs(node_kind):
    root = tempfile.mkdtemp()
    script = ('. {lib}\n_node_kind={kind} _virt_srv=qemu+ssh://nuc6/system\n'
              'write_sshfs_mount {root}\n').format(lib=_LIB, kind=node_kind, root=root)
    subprocess.run(["bash", "-c", script], check=True)
    return Path(root)


cloud_root = _write_sshfs("cloud")
check("a cloud node gets no sshfs mount of a hypervisor it cannot reach",
      not (cloud_root / "etc/systemd/system/srv-www-htdocs-sources.mount").exists()
      and not (cloud_root / "etc/fstab").exists())
container_root = _write_sshfs("container")
check("a container node still gets its sshfs mount units",
      (container_root / "etc/systemd/system/srv-www-htdocs-sources.mount").exists())

lib_text = _LIB.read_text()
check("the sources automount is not started on a cloud node either",
      re.search(r'if \[\[ "\$\{_node_kind\}" == cloud \]\]; then\s*\n\s*:', lib_text) is not None)
check("a cloud node installs the packages a plain provider image lacks",
      'elif [[ "${_node_kind}" == cloud ]]; then' in lib_text
      and lib_text.count("node_install_packages") >= 2)
check("an empty root password hash is never written as a passwordless account",
      '[[ -z "${ROOT_PWD_HASH}" ]] || echo "root:${ROOT_PWD_HASH}" | chpasswd -e' in lib_text)

# ── it must not build a second DNS server beside the one it is building ───
# provision_vm()'s cloud path creates a lab-dns VM and points the provider's network at it. For an
# automation node that is both an instance nobody needs and the wrong server for the network to use,
# since the node being built serves the zone itself.
src = (_REPO / "scripts" / "setup_automation_node.py").read_text()
check("the automation node is provisioned with the cloud-DNS-VM step turned off",
      "provision_vm(definition, config, defaults, vm_name, ensure_cloud_dns=False)" in src)
check("and the network is pointed at the node itself instead",
      "backend.use_dns_server(private_ip, mydomain)" in src)
check("that happens after the node is configured, not before its BIND exists",
      src.index("configure_node(public_ip, stage)") < src.index("backend.use_dns_server("))

setup_vm_src = (_REPO / "scripts" / "setup_vm.py").read_text()
check("provision_vm takes the switch and defaults it on, so every other caller is unchanged",
      "def provision_vm(definition, config, defaults, vm_name, ensure_cloud_dns=True):" in setup_vm_src)
check("and the switch actually guards the cloud-DNS-VM block",
      "if ensure_cloud_dns and backend_name in backends.CLOUD_BACKEND_NAMES" in setup_vm_src)

# ── a cloud node is waited for at its address, not at its name ────────────
# The name is registered by the same run, and whether it resolves from the machine doing the
# provisioning depends on that machine serving the lab's domain. Polling the name made building a node
# in its own cloud domain impossible: live on AWS the instance was up and reachable by address while
# check_ssh_conn spent 100 retries on a name the local BIND had no zone for.
check("provision_vm polls the address the provider assigned when there is one",
      'ssh_target = env.get("myip") or vm_name' in setup_vm_src)
check("and nothing is waited for by name any more",
      "check_ssh_conn(vm_name)" not in setup_vm_src
      and setup_vm_src.count("check_ssh_conn(ssh_target)") == 2)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud automation-node checks passed")
