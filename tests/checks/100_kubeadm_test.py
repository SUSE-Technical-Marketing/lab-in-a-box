#!/usr/bin/env python3.11
# clu_type kubeadm: upstream Kubernetes built with kubeadm — see 100_kubeadm.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import k8s  # noqa: E402
import lab_schema_base  # noqa: E402
import schema_validate as sv  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


distro = k8s.get_distro("kubeadm")

# ── the cluster type is reachable from a lab ─────────────────────────────────
check("kubeadm is a cluster type", isinstance(distro, k8s.KubeadmDistro) and distro.name == "kubeadm")
BASE = lab_schema_base.base_lab_schema()
clu_type = [f for f in BASE["sections"]["kclusters"]["fields"] if f["name"] == "clu_type"][0]
check("the schema offers it", "kubeadm" in clu_type["enum"])
# The schema's list and the registry are two halves of the same fact: a lab can only ask for what k8s.py can build.
check("every cluster type the schema offers is one k8s.py can build",
      sorted(clu_type["enum"]) == sorted(k8s.DISTROS))
check("a lab asking for an unknown type is refused by the schema",
      sv.value_finding(clu_type, "openshift") is not None)

# ── what it installs ────────────────────────────────────────────────────────
check("the minor comes from clu_rel, however it is written",
      distro.minor("1.33") == "v1.33" and distro.minor("v1.33") == "v1.33"
      and distro.minor("v1.33.4+build") == "v1.33")
for bad in ("stable", "", None, "latest"):
    try:
        distro.minor(bad)
        check("a clu_rel without a minor is refused: {!r}".format(bad), False)
    except SystemExit:
        pass

check("flannel is the default plugin and brings its own network",
      distro.cni({}) == "flannel" and distro.pod_cidr({}) == "10.244.0.0/16")
check("calico's own network is used when it is chosen",
      distro.pod_cidr({"clu_cni": "calico"}) == "192.168.0.0/16")
check("the lab's own pod network wins",
      distro.pod_cidr({"clu_cni": "calico", "clu_pod_cidr": "10.9.0.0/16"}) == "10.9.0.0/16")
check("none is allowed, for a lab that brings its own plugin", distro.cni({"clu_cni": "none"}) == "none")
try:
    distro.cni({"clu_cni": "weave"})
    check("an unknown plugin is refused", False)
except SystemExit:
    pass

check("kubectl uses the admin kubeconfig kubeadm writes",
      distro.kubeconfig_path() == "/etc/kubernetes/admin.conf"
      and distro.kubeconfig_path() in distro.kubectl_command())
check("there is no join-token file, because kubeadm keeps none", distro.token_path() == "")


class _Node:
    """Records what runs on a node, and answers the commands whose output the install reads."""

    def __init__(self, transactional=False, missing=(), skipped_repo=False, absent_binaries=""):
        self.cmds = []
        self.transactional = transactional
        self.missing = missing
        self.skipped_repo = skipped_repo
        self.absent_binaries = absent_binaries

    def run(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        for name in self.missing:
            if cmd == "command -v {}".format(name):
                return mock.Mock(returncode=1, stdout="")
        if "command -v $b" in cmd:
            return mock.Mock(returncode=0, stdout=self.absent_binaries)
        if self.skipped_repo and cmd.startswith(("zypper", "dnf", "transactional-update")):
            # zypper's informational code for "a repository was skipped", which says nothing about the packages
            return mock.Mock(returncode=106, stdout="")
        return mock.Mock(returncode=0, stdout="")

    def output(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        if "print-join-command" in cmd:
            return "kubeadm join cluster.lab:6443 --token abc.def --discovery-token-ca-cert-hash sha256:1234\n"
        if "upload-certs" in cmd:
            return "deadbeefcafe\n"
        return ""

    def joined(self):
        return "\n".join(self.cmds)


def _install(node, clu_ctx, kind="server", token=None):
    with mock.patch.object(k8s, "ssh_run", side_effect=node.run), \
         mock.patch.object(k8s, "ssh_output", side_effect=node.output), \
         mock.patch.object(k8s, "is_transactional", return_value=node.transactional), \
         mock.patch.object(k8s, "reboot_into_pending_snapshot"):
        if kind == "server":
            return distro.install_server("n1", "clu", clu_ctx, token=token, rancher1_ip=None if token is None else "n0")
        return distro.install_agent("n1", "clu", clu_ctx, token, "n0")


CTX = {"clu_rel": "1.33", "mydomain": "mydemo.lab", "_node_count": 3}

node = _Node()
token, first = _install(node, CTX)
text = node.joined()
check("swap is turned off, since the kubelet refuses to start with it on", "swapoff -a" in text)
check("the modules and sysctls Kubernetes needs are set",
      "br_netfilter" in text and "net.ipv4.ip_forward=1" in text
      and "net.bridge.bridge-nf-call-iptables=1" in text)
check("containerd is installed and put on the systemd cgroup driver",
      "containerd" in text and "SystemdCgroup = true" in text)
check("the packages come from the community repository of this cluster's minor",
      "pkgs.k8s.io/core:/stable:/v1.33" in text and "kubelet kubeadm kubectl" in text)
# Confirmed live on 2026-10-16: without this, zypper answers its own trust prompt with no, skips the repository
# and reports kubeadm as not existing at all.
check("the repository's signing key is imported before the packages are installed",
      "rpm --import" in text
      and text.index("rpm --import") < text.index("kubelet kubeadm kubectl"))
check("the repository is set to refresh itself", "autorefresh=1" in text)
check("the cluster is initialised with the pod network and a control-plane endpoint",
      "kubeadm init --pod-network-cidr 10.244.0.0/16" in text and "--control-plane-endpoint clu.mydemo.lab:6443" in text)
check("certificates are uploaded, so another control-plane node can join", "--upload-certs" in text)
check("the network plugin is applied", "kube-flannel.yml" in text)
check("the admin kubeconfig is put where a plain kubectl finds it", "/root/.kube/config" in text)
check("the join command and the certificate key are handed to the next node",
      token["join"].startswith("kubeadm join") and token["certificate_key"] == "deadbeefcafe" and first == "n1")
check("a three-node cluster keeps its control-plane taint",
      "node-role.kubernetes.io/control-plane-" not in text)

node = _Node()
_install(node, dict(CTX, _node_count=1))
check("a single-node cluster has the control-plane taint removed, since pods have nowhere else to go",
      "node-role.kubernetes.io/control-plane-" in node.joined())

node = _Node()
_install(node, dict(CTX, clu_cni="none"))
check("clu_cni none applies no plugin",
      "kube-flannel.yml" not in node.joined() and "calico.yaml" not in node.joined())
node = _Node()
_install(node, dict(CTX, clu_cni="calico"))
check("calico is applied with its own network",
      "calico.yaml" in node.joined() and "--pod-network-cidr 192.168.0.0/16" in node.joined())


# ── what the live run on 2026-10-16 found ───────────────────────────────────
# The repository and its key come first: a zypper that does not trust the key skips the repository and then exits
# 106 for every later install, so the failure surfaced on whatever was being installed at the time (containerd).
node = _Node()
text = node.joined() if node.cmds else ""
_install(node, CTX)
text = node.joined()
check("the repository and its key are set up before anything is installed",
      text.index("rpm --import") < text.index("containerd")
      and text.index("kubernetes.repo") < text.index("containerd"))

# zypper exits 106 when it skipped a repository, with the packages installed; that is not a failure.
node = _Node(skipped_repo=True)
_install(node, CTX)
check("zypper's skipped-repository exit code does not abort the install",
      "kubeadm init" in node.joined())

# What the packages were for is checked instead, so a skipped repository cannot pass silently.
node = _Node(absent_binaries="kubeadm\nkubelet\n")
try:
    _install(node, CTX)
    check("missing binaries after the install are an error", False)
except SystemExit:
    pass
check("the binaries are checked after the install", "command -v $b" in node.joined())

# ── joining ─────────────────────────────────────────────────────────────────
JOIN = {"join": "kubeadm join clu.lab:6443 --token t --discovery-token-ca-cert-hash sha256:1", "certificate_key": "k"}
node = _Node()
_install(node, CTX, kind="server", token=JOIN)
check("another control-plane node joins with the certificate key",
      "--control-plane --certificate-key k" in node.joined())
check("it is prepared the same way before joining", "swapoff -a" in node.joined())
check("it does not initialise a second cluster", "kubeadm init" not in node.joined())

node = _Node()
_install(node, CTX, kind="agent", token=JOIN)
check("a worker runs the plain join command",
      JOIN["join"] in node.joined() and "--control-plane" not in node.joined())

# ── the package manager is detected, not assumed ────────────────────────────
node = _Node(missing=("zypper", "dnf"))
_install(node, CTX)
check("an apt host gets the deb repository and its keyring",
      "/etc/apt/sources.list.d/kubernetes.list" in node.joined() and "apt-get install -y" in node.joined())
node = _Node(missing=("zypper", "dnf", "apt-get"))
try:
    _install(node, CTX)
    check("a host with no known package manager is refused", False)
except SystemExit:
    pass

node = _Node(transactional=True)
_install(node, CTX)
check("a transactional host installs through transactional-update",
      "transactional-update -n pkg install -y" in node.joined())

# ── the node count comes from the lab, not from a guess ─────────────────────
lab = {"nodes": {"a": {"kcluster": "c1"}, "b": {"kcluster": "c1"}, "c": {"kcluster": "other"}}}
seen = {}
with mock.patch.object(k8s.K8sDistro, "install_cluster", side_effect=lambda d, c, ctx: seen.update(ctx)):
    distro.install_cluster(lab, "c1", {"clu_rel": "1.33"})
check("only this cluster's nodes are counted", seen.get("_node_count") == 2)

# ── an add-on restricts itself to distributions only when it must ───────────
# requires_kubernetes names the distributions an add-on REQUIRES, so null (any Kubernetes) is the normal case and a
# list is a real constraint: the add-on's own code has to be distribution-specific to justify one.
import ast  # noqa: E402
import re  # noqa: E402

import apps  # noqa: E402

DISTRO_SPECIFIC = re.compile(r"clu_type\s*==|/var/lib/rancher/|/etc/rancher/")
restricting, any_k8s = [], []
for path in sorted(Path(_REPO / "scripts").glob("install_*")):
    source = path.read_text(errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        continue
    plugin = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PLUGIN" for t in node.targets):
            plugin = ast.literal_eval(node.value)
    if plugin is None:
        continue
    required = plugin.get("requires_kubernetes")
    if required is None:
        any_k8s.append(path.name)
        continue
    restricting.append(path.name)
    check("{}: every distribution it names is one k8s.py can build".format(path.name),
          isinstance(required, list) and all(d in k8s.DISTROS for d in required))
    check("{}: it is distribution-specific, which is what lets it restrict".format(path.name),
          bool(DISTRO_SPECIFIC.search(source)))

check("most add-ons run on any Kubernetes distribution", len(any_k8s) > 50)
check("an add-on that names no distribution is placed on a kubeadm cluster",
      apps.requirement_issue(dict(apps.DEFAULT_PLUGIN, name="x", requires_kubernetes=None),
                             "container", clu_type="kubeadm") is None)
check("the default for an add-on that prints no capabilities is any distribution",
      apps.DEFAULT_PLUGIN["requires_kubernetes"] is None)
check("one that names distributions is still refused on the others",
      apps.requirement_issue(dict(apps.DEFAULT_PLUGIN, name="x", requires_kubernetes=["rke2", "k3s"]),
                             "container", clu_type="kubeadm") is not None)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all kubeadm checks passed")
