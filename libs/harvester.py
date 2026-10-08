"""
Harvester / SUSE Virtualization clusters (kclusters entries with clu_type "harvester").

A Harvester node is an appliance: the OS, RKE2, KubeVirt and Longhorn come from its image, and the cluster forms at first
boot from a Harvester configuration passed as the node's user-data. The first node of the cluster (in the lab
definition's order) creates it; the others join it through the cluster VIP. Today the nodes run on the aws backend from
an AMI the user supplies (built e.g. with github.com/jdreinhardt/harvester-on-aws), with nested virtualization.

kclusters.<name> fields read here (besides clu_type, clu_rel, mydomain):
  harvester_token          cluster token, shared by every node (required)
  harvester_vip            cluster VIP; empty: a secondary private IP AWS picks on the create node
  harvester_mtu            management network MTU (default 1500; every node must use the same)
  harvester_replica_count  Longhorn replica count of the default StorageClass (create node only)
  harvester_device         install device (default /dev/nvme0n1p6, the community AMI's layout)
  harvester_ntp_servers    NTP servers (default 169.254.169.123, the AWS time service)
  harvester_config         mapping merged last into every node's Harvester configuration
  harvester_settings       name: value Harvester Settings applied once the cluster is up
nodes.<name> field: harvester_role (default, management, worker or witness).

Harvester blocks root SSH: the automation node reaches the nodes as "rancher", which has passwordless sudo.
"""
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from lab_creation import die, log, ssh_run, yaml_scalar

CLU_TYPE = "harvester"
DEFAULT_MTU = 1500
DEFAULT_DEVICE = "/dev/nvme0n1p6"
AWS_NTP = "169.254.169.123"
SSH_USER = "rancher"
KUBECONFIG_DIR = Path("/etc/lab_creation")
# Ports the cluster serves beyond SSH: HTTP redirect, UI/API, Kubernetes API, RKE2 supervisor.
OPEN_PORTS = ["80", "443", "6443", "9345"]


def cluster_cfg(definition: dict, clu_name: str) -> dict:
    """kclusters.<clu_name> as written in the lab definition ({} when absent)."""
    return (definition.get("kclusters") or {}).get(clu_name) or {}


def cluster_of(definition: dict, vm_name: str) -> str:
    """The Harvester cluster VM `vm_name` belongs to, or "" when it is not a Harvester node."""
    clu = ((definition.get("nodes") or {}).get(vm_name) or {}).get("kcluster") or ""
    return clu if clu and cluster_cfg(definition, clu).get("clu_type") == CLU_TYPE else ""


def cluster_nodes(definition: dict, clu_name: str) -> list:
    """The names of the nodes in cluster `clu_name`, in the lab definition's order; the first creates the cluster."""
    return [n for n, cfg in (definition.get("nodes") or {}).items() if (cfg or {}).get("kcluster") == clu_name]


def create_node(definition: dict, clu_name: str) -> str:
    """The node that creates cluster `clu_name`."""
    nodes = cluster_nodes(definition, clu_name)
    return nodes[0] if nodes else ""


def _merge(base: dict, extra: dict) -> dict:
    """`base` with mapping `extra` merged in recursively (extra wins)."""
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def node_config(definition: dict, clu_name: str, vm_name: str, vip: str, ssh_keys: list, password_hash: str) -> dict:
    """The Harvester configuration (scheme_version 1) of node `vm_name`: create mode for the first node, join mode
    through `vip` for the others."""
    clu = cluster_cfg(definition, clu_name)
    node = (definition.get("nodes") or {}).get(vm_name) or {}
    token = clu.get("harvester_token") or die("Harvester cluster '{}' has no harvester_token".format(clu_name))
    creating = vm_name == create_node(definition, clu_name)
    install = {
        "automatic": True,
        "mode": "create" if creating else "join",
        "device": clu.get("harvester_device") or DEFAULT_DEVICE,
        "management_interface": {
            "interfaces": [{"name": "eth0"}],
            "method": "dhcp",
            "bond_options": {"mode": "active-backup", "miimon": 100},
            "mtu": int(clu.get("harvester_mtu") or DEFAULT_MTU),
            "default_route": True,
        },
    }
    if node.get("harvester_role"):
        install["role"] = node["harvester_role"]
    cfg = {"scheme_version": 1, "token": token}
    if creating:
        install.update({"vip": vip, "vip_mode": "static"})
        if clu.get("harvester_replica_count") not in (None, ""):
            install["harvester"] = {"storage_class": {"replica_count": int(clu["harvester_replica_count"])}}
    else:
        cfg["server_url"] = "https://{}:443".format(vip)
    cfg["os"] = {
        "hostname": vm_name,
        "ssh_authorized_keys": list(ssh_keys),
        "ntp_servers": list(clu.get("harvester_ntp_servers") or [AWS_NTP]),
    }
    if password_hash:
        cfg["os"]["password"] = password_hash
    cfg["install"] = install
    return _merge(cfg, clu.get("harvester_config") or {})


def user_data(cfg: dict) -> str:
    """Harvester configuration `cfg` as user-data text (JSON, which the installer reads as YAML)."""
    return json.dumps(cfg, indent=2) + "\n"


def problems(definition: dict, backend_name_of) -> list:
    """What prevents the lab's Harvester clusters from being created, as sentences. `backend_name_of(vm_name)` gives a
    node's effective backend name."""
    out = []
    nodes = definition.get("nodes") or {}
    for clu_name, clu in (definition.get("kclusters") or {}).items():
        if (clu or {}).get("clu_type") != CLU_TYPE:
            continue
        members = cluster_nodes(definition, clu_name)
        if not members:
            out.append("Harvester cluster '{}' has no node".format(clu_name))
        if not clu.get("harvester_token"):
            out.append("Harvester cluster '{}' has no harvester_token".format(clu_name))
        if clu.get("addons"):
            out.append("Harvester cluster '{}' lists add-ons; add-ons are not installed on a Harvester cluster "
                       "yet".format(clu_name))
        for vm in members:
            cfg = nodes.get(vm) or {}
            common = definition.get("common") or {}
            if backend_name_of(vm) != "aws":
                out.append("Harvester node '{}' must use the aws backend".format(vm))
            nested = cfg.get("aws_nested_virtualization", common.get("aws_nested_virtualization"))
            if str(nested).lower() != "true":
                out.append("Harvester node '{}' needs aws_nested_virtualization: \"true\"".format(vm))
            image = (cfg.get("SOURCE_IMAGE") or cfg.get("ISO_IMAGE") or common.get("SOURCE_IMAGE")
                     or common.get("ISO_IMAGE") or "")
            if not str(image).startswith("ami-"):
                out.append("Harvester node '{}' needs its Harvester AMI ID as SOURCE_IMAGE".format(vm))
    return out


def wait_https(host: str, timeout: int = 2400, interval: int = 15) -> None:
    """Wait until https://`host`/ answers (any HTTP status; the certificate is self-signed); die after `timeout` s."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    while True:
        try:
            urllib.request.urlopen("https://{}/".format(host), context=ctx, timeout=10)
            return
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, OSError):
            pass
        if time.time() > deadline:
            die("Harvester on {} did not answer on HTTPS within {} s".format(host, timeout))
        time.sleep(interval)


def kubeconfig_path(clu_name: str) -> Path:
    """Where the kubeconfig of Harvester cluster `clu_name` is kept on the automation node."""
    return KUBECONFIG_DIR / "harvester-{}.kubeconfig".format(clu_name)


def fetch_kubeconfig(host: str, dest: Path, server: str, tls_server_name: str = "") -> Path:
    """
    Copy the RKE2 kubeconfig of the Harvester node at `host` (read as "rancher" with sudo) to `dest` (mode 0600), with
    its server set to `server` (e.g. "https://<ip>:6443"). `tls_server_name` names the certificate to expect when
    `server` is an address the certificate does not cover (e.g. a public IP in front of the VIP).
    """
    text = ssh_run(host, "sudo cat /etc/rancher/rke2/rke2.yaml", user=SSH_USER, capture=True).stdout
    text = text.replace("https://127.0.0.1:6443", server)
    if tls_server_name:
        text = text.replace("    server: {}\n".format(server),
                            "    server: {}\n    tls-server-name: {}\n".format(server, tls_server_name))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    dest.chmod(0o600)
    return dest


def wait_nodes_ready(kubeconfig: Path, count: int, timeout: int = 3600, interval: int = 30) -> None:
    """Wait until the cluster reached through `kubeconfig` has `count` Ready nodes; die after `timeout` s."""
    deadline = time.time() + timeout
    while True:
        r = subprocess.run(["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes", "--no-headers"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        ready = sum(1 for line in r.stdout.splitlines() if len(line.split()) > 1 and line.split()[1] == "Ready")
        if r.returncode == 0 and ready >= count:
            return
        if time.time() > deadline:
            die("Harvester cluster has {} of {} nodes Ready after {} s".format(ready, count, timeout))
        time.sleep(interval)


def apply_settings(kubeconfig: Path, settings: dict) -> None:
    """Apply `settings` (name: value) as harvesterhci.io/v1beta1 Setting objects through `kubeconfig`."""
    for name, value in (settings or {}).items():
        manifest = (
            "apiVersion: harvesterhci.io/v1beta1\n"
            "kind: Setting\n"
            "metadata:\n"
            "  name: {}\n"
            "value: {}\n"
        ).format(name, yaml_scalar(value))
        log("- applying Harvester Setting '{}'".format(name))
        r = subprocess.run(["kubectl", "--kubeconfig", str(kubeconfig), "apply", "-f", "-"],
                           input=manifest, universal_newlines=True)
        if r.returncode != 0:
            die("failed to apply Harvester Setting '{}'".format(name))
