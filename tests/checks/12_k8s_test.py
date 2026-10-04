#!/usr/bin/env python3
# Mocked-SSH unit tests for libs/k8s.py — no live K3s/RKE2
# cluster is available in this project. ssh_run/ssh_output/subprocess.run
# and time.sleep are all mocked, and any test that writes local RKE2 config
# files (write_node_config) runs from a scratch tempdir — tests/checks runs
# against a read-only mount of the repo (see tests/run_tests.sh), so
# anything that writes relative to cwd must not run from the repo root. Run
# from 12_k8s.sh, in its own container.
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import k8s  # noqa: E402

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


class FakeSSH:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []

    def __call__(self, hostname, cmd, **kwargs):
        self.calls.append((hostname, cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeResult()


k8s.prepare_local_as_kubeclient = lambda: None  # never touch the real ~/.kube


# ── get_distro dispatch ───────────────────────────────────────────────────────
check("get_distro('k3s') returns a K3sDistro", isinstance(k8s.get_distro("k3s"), k8s.K3sDistro))
check("get_distro('rke2') returns an RKE2Distro", isinstance(k8s.get_distro("rke2"), k8s.RKE2Distro))
died = False
try:
    k8s.get_distro("bogus")
except SystemExit:
    died = True
check("get_distro: dies naming supported distros for an unknown type", died)


# ── K3sDistro: first node vs. join ───────────────────────────────────────────
fake = FakeSSH(responses=[("node-token", FakeResult(stdout="TOKEN123\n"))])
k8s.ssh_run = fake
k8s.ssh_output = lambda h, c: k8s.ssh_run(h, c, capture=True).stdout.strip()
distro = k8s.K3sDistro()
token, addr = distro.install_server("vm1", "cluster1", {"clu_rel": "stable", "mydomain": "mydemo.lab"})
check("K3sDistro first node: returns the freshly read token", token == "TOKEN123")
check("K3sDistro first node: rancher1_ip is the node itself", addr == "vm1")
install_cmd = next(c for h, c, kw in fake.calls if "get.k3s.io" in c)
check("K3sDistro first node: no K3S_URL/K3S_TOKEN on the server install",
      "K3S_URL" not in install_cmd and "K3S_TOKEN" not in install_cmd)
check("K3sDistro first node: --tls-san includes the cluster FQDN",
      "--tls-san cluster1.mydemo.lab" in install_cmd)
# K3sDistro starts the service explicitly. The k3s install script can leave the service enabled but not started, so both RKE2Distro
# and K3sDistro start it themselves.
check("K3sDistro first node: explicitly starts the k3s service itself, "
      "rather than trusting get.k3s.io's install script to have done it",
      any(c == "systemctl enable --now k3s" for h, c, kw in fake.calls))
enable_call_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if c == "systemctl enable --now k3s")
token_call_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if "node-token" in c)
check("K3sDistro first node: starts the service BEFORE reading the node-token "
      "(the actual bug — reading it first raced against a service that was never started)",
      enable_call_idx < token_call_idx)

fake = FakeSSH()
k8s.ssh_run = fake
token, addr = distro.install_agent("vm2", "cluster1", {"clu_rel": "stable", "mydomain": "mydemo.lab"},
                                    "TOKEN123", "vm1")
check("K3sDistro join: rancher1_ip passed through unchanged", addr == "vm1")
check("K3sDistro join: token passed through unchanged", token == "TOKEN123")
join_cmd = next(c for h, c, kw in fake.calls if "get.k3s.io" in c)
check("K3sDistro join: K3S_URL points at the first server", "K3S_URL=https://vm1:6443" in join_cmd)
check("K3sDistro join: K3S_TOKEN carries the join token", "K3S_TOKEN=TOKEN123" in join_cmd)
check("K3sDistro join: explicitly starts k3s-agent itself, same reasoning as the server case",
      any(c == "systemctl enable --now k3s-agent" for h, c, kw in fake.calls))

# ── K3sDistro: clu_rel, clu_name and mydomain are shell-quoted ───────────────
# These values are free text from the lab JSON. They are quoted, so no value can break or inject into the remote command.
fake = FakeSSH(responses=[("node-token", FakeResult(stdout="TOKEN123\n"))])
k8s.ssh_run = fake
distro.install_server("vm1", "cluster1", {"clu_rel": "stable; rm -rf /", "mydomain": "mydemo.lab"})
install_cmd = next(c for h, c, kw in fake.calls if "get.k3s.io" in c)
check("K3sDistro first node: a shell metacharacter in clu_rel is quoted, not passed through raw",
      "INSTALL_K3S_CHANNEL='stable; rm -rf /'" in install_cmd)


# ── RKE2Distro.write_node_config: run from a scratch tempdir (repo is
#    read-only in the test container) ─────────────────────────────────────────
_orig_cwd = os.getcwd()
_tmp = tempfile.mkdtemp()
os.chdir(_tmp)
try:
    distro = k8s.RKE2Distro()
    distro.write_node_config("cluster1", {"mydomain": "mydemo.lab"})
    server_cfg = Path("cluster1") / "config-server.yaml"
    agent_cfg = Path("cluster1") / "config-agent.yaml"
    check("write_node_config: writes config-server.yaml", server_cfg.is_file())
    check("write_node_config: writes config-agent.yaml", agent_cfg.is_file())
    check("write_node_config: embeds both the domain and cluster.domain in tls-san",
          "mydemo.lab" in server_cfg.read_text() and "cluster1.mydemo.lab" in server_cfg.read_text())

    server_cfg.write_text("MODIFIED-BY-TEST\n")
    distro.write_node_config("cluster1", {"mydomain": "mydemo.lab"})
    check("write_node_config: does not overwrite an existing config file",
          server_cfg.read_text() == "MODIFIED-BY-TEST\n")
finally:
    os.chdir(_orig_cwd)


# ── RKE2Distro._install: command shape, first node vs. join ─────────────────
class FakeSubprocessRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResult(returncode=0)


_tmp2 = tempfile.mkdtemp()
os.chdir(_tmp2)
try:
    fake_ssh = FakeSSH(responses=[("node-token", FakeResult(stdout="RKE2TOKEN\n"))])
    k8s.ssh_run = fake_ssh
    k8s.ssh_output = lambda h, c: k8s.ssh_run(h, c, capture=True).stdout.strip()
    fake_subproc = FakeSubprocessRun()
    k8s.subprocess.run = fake_subproc

    distro = k8s.RKE2Distro()
    token, addr = distro.install_server("vm1", "cluster1", {"clu_rel": "stable", "mydomain": "mydemo.lab"})
    check("RKE2Distro first node: returns the freshly read token", token == "RKE2TOKEN")
    check("RKE2Distro first node: rancher1_ip is the node itself", addr == "vm1")
    install_cmd = next(c for h, c, kw in fake_ssh.calls if "get.rke2.io" in c)
    check("RKE2Distro first node: INSTALL_RKE2_TYPE=server", "INSTALL_RKE2_TYPE=server" in install_cmd)
    check("RKE2Distro first node: enables rke2-server.service",
          any("systemctl enable --now rke2-server.service" in c for h, c, kw in fake_ssh.calls))
    check("RKE2Distro: rsyncs the rendered config to the remote host",
          any(a[0] == "rsync" for a, kw in fake_subproc.calls))

    # ── clu_rel and install_method are shell-quoted ──────────────────────────
    # These are free text from the lab JSON, so they are quoted before they reach the remote install command.
    fake_ssh = FakeSSH(responses=[("node-token", FakeResult(stdout="RKE2TOKEN\n"))])
    k8s.ssh_run = fake_ssh
    distro.install_server("vm1", "cluster1", {
        "clu_rel": "stable; rm -rf /", "mydomain": "mydemo.lab", "install_method": "tar; touch /tmp/pwned"})
    install_cmd = next(c for h, c, kw in fake_ssh.calls if "get.rke2.io" in c)
    check("RKE2Distro first node: a shell metacharacter in clu_rel is quoted, not passed through raw",
          "INSTALL_RKE2_CHANNEL='stable; rm -rf /'" in install_cmd)
    check("RKE2Distro first node: a shell metacharacter in install_method is quoted, not passed through raw",
          "INSTALL_RKE2_METHOD='tar; touch /tmp/pwned'" in install_cmd)

    fake_ssh = FakeSSH()
    k8s.ssh_run = fake_ssh
    token, addr = distro.install_agent("vm2", "cluster1", {"clu_rel": "stable", "mydomain": "mydemo.lab"},
                                        "RKE2TOKEN", "vm1")
    check("RKE2Distro join: rancher1_ip/token passed through unchanged", addr == "vm1" and token == "RKE2TOKEN")
    install_cmd = next(c for h, c, kw in fake_ssh.calls if "get.rke2.io" in c)
    check("RKE2Distro join: INSTALL_RKE2_TYPE=agent", "INSTALL_RKE2_TYPE=agent" in install_cmd)
    check("RKE2Distro join: appends server/token to config.yaml",
          any("server: https://vm1:9345" in c for h, c, kw in fake_ssh.calls)
          and any("token: RKE2TOKEN" in c for h, c, kw in fake_ssh.calls))
finally:
    os.chdir(_orig_cwd)


# ── list_kclusters / get_vm_kcluster / load_kclu_vars ────────────────────────
definition = {
    "kclusters": {"cluster1": {"clu_type": "rke2", "clu_rel": "stable"}},
    "nodes": {"vm1": {"kcluster": "cluster1"}, "vm2": {}},
}
check("list_kclusters: returns all kcluster names", k8s.list_kclusters(definition) == ["cluster1"])
check("get_vm_kcluster: returns the node's kcluster", k8s.get_vm_kcluster(definition, "vm1") == "cluster1")
check("get_vm_kcluster: empty string when unset", k8s.get_vm_kcluster(definition, "vm2") == "")
check("load_kclu_vars: returns the cluster's scalar vars",
      k8s.load_kclu_vars(definition, "cluster1") == {"clu_type": "rke2", "clu_rel": "stable"})
check("load_kclu_vars: empty clu_name returns {}", k8s.load_kclu_vars(definition, "") == {})
died = False
try:
    k8s.load_kclu_vars(definition, "nope")
except SystemExit:
    died = True
check("load_kclu_vars: dies for an undefined kcluster", died)


# ── first_server_node / addon_nodes / iter_cluster_nodes ─────────────────────
definition2 = {
    "nodes": {
        "srv1": {"INSTALL_RKE2_TYPE": "server", "kcluster": "c1", "addons": ["rancher"]},
        "agt1": {"INSTALL_RKE2_TYPE": "agent", "kcluster": "c1"},
        "other": {"kcluster": "c2"},
    }
}
name, ssh_cmd = k8s.first_server_node(definition2)
check("first_server_node: finds the first server-typed node", name == "srv1")
check("first_server_node: builds a root@ ssh command", "root@srv1" in ssh_cmd)

check("first_server_node: returns None when no server node exists",
      k8s.first_server_node({"nodes": {"a": {"INSTALL_RKE2_TYPE": "agent"}}}) is None)

nodes_with_addon = k8s.addon_nodes(definition2, "rancher")
check("addon_nodes: finds the one node with the addon", [n for n, _ in nodes_with_addon] == ["srv1"])
check("addon_nodes: given an explicit vm_name, returns just that one without scanning",
      [n for n, _ in k8s.addon_nodes(definition2, "rancher", vm_name="agt1")] == ["agt1"])
check("addon_nodes: empty list when nothing has the addon",
      k8s.addon_nodes(definition2, "nope") == [])

c1_nodes = [n for n, _, _ in k8s.iter_cluster_nodes(definition2, "c1")]
check("iter_cluster_nodes: yields only nodes in the given cluster, in order",
      c1_nodes == ["srv1", "agt1"])


# ── create_basic_auth_secret / set_longhorn_overprovisioning ─────────────────
fake = FakeSSH()
k8s.ssh_run = fake
k8s.create_basic_auth_secret("vm1", "ns1", "mysecret", "admin", "s3cr3t")
cmd = fake.calls[0][1]
check("create_basic_auth_secret: names the secret and namespace correctly",
      "secret generic mysecret -n ns1" in cmd)
check("create_basic_auth_secret: passes username/password as --from-literal values",
      "--from-literal=username=admin" in cmd and "--from-literal=password=s3cr3t" in cmd)

# A password with shell metacharacters is shlex-quoted, so it cannot break out of the remote command string.
import shlex as _shlex
fake = FakeSSH()
k8s.ssh_run = fake
_pw = "p'w; rm -rf /$(id)"
k8s.create_basic_auth_secret("vm1", "ns1", "mysecret", "admin", _pw)
nasty = fake.calls[0][1]
check("create_basic_auth_secret: a password with quotes/;/$() is shlex-quoted into the command",
      "--from-literal=password={}".format(_shlex.quote(_pw)) in nasty)

fake = FakeSSH(responses=[("storage-over-provisioning-percentage", FakeResult(returncode=1))])
k8s.ssh_run = fake
k8s.set_longhorn_overprovisioning("vm1", 500)  # returncode!=0 just logs, never raises
check("set_longhorn_overprovisioning: issues exactly one patch call", len(fake.calls) == 1)
check("set_longhorn_overprovisioning: patches with the requested percentage",
      '"value":"500"' in fake.calls[0][1])


# ── setup_cnpg_operator ───────────────────────────────────────────────────────
fake = FakeSSH()
k8s.ssh_run = fake
k8s.setup_cnpg_operator("vm1", chart_version="0.22.0")
check("setup_cnpg_operator: passes --version through to helm upgrade --install",
      any("--version 0.22.0" in c for h, c, kw in fake.calls))
check("setup_cnpg_operator: uses helm upgrade --install (idempotent)",
      any("helm upgrade --install cnpg" in c for h, c, kw in fake.calls))

fake = FakeSSH(responses=[("helm upgrade --install cnpg", FakeResult(returncode=1))])
k8s.ssh_run = fake
died = False
try:
    k8s.setup_cnpg_operator("vm1")
except SystemExit:
    died = True
check("setup_cnpg_operator: dies when the helm install fails", died)


# ── setup_traefik_rke2 / setup_traefik_k3s: fast happy paths only (no real
#    30-iteration polling — every relevant probe is scripted to succeed on
#    its first call, and time.sleep is neutered as a belt-and-braces guard
#    against ever actually waiting in a test) ─────────────────────────────────
k8s.time.sleep = lambda s: None

fake = FakeSSH(responses=[
    ("helm status traefik", FakeResult(returncode=1)),        # no old upstream traefik installed
    ("get ds -n kube-system rke2-traefik", FakeResult(returncode=0)),      # already present
    ("rke2-ingress-nginx-controller", FakeResult(returncode=1)),           # nginx not present
    ("rollout status ds/rke2-traefik", FakeResult(returncode=0)),
])
k8s.ssh_run = fake
try:
    k8s.setup_traefik_rke2("vm1", extra_ports=["salt-publish:4505"])
    ok = True
except SystemExit:
    ok = False
check("setup_traefik_rke2: happy path completes without dying", ok)
check("setup_traefik_rke2: writes a HelmChartConfig with the extra port",
      any("salt-publish" in (kw.get("input_text") or "") and "4505" in (kw.get("input_text") or "")
          for h, c, kw in fake.calls))
check("setup_traefik_rke2: switches ingress-controller to traefik in config.yaml",
      any("ingress-controller: traefik" in c for h, c, kw in fake.calls))

fake = FakeSSH(responses=[("rollout status deploy/traefik", FakeResult(returncode=0))])
k8s.ssh_run = fake
try:
    k8s.setup_traefik_k3s("vm1", extra_ports=None)
    ok = True
except SystemExit:
    ok = False
check("setup_traefik_k3s: happy path (no extra ports) completes without dying", ok)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all k8s checks passed")
