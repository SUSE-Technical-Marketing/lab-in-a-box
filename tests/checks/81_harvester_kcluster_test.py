#!/usr/bin/env python3
# libs/harvester.py — see 81_harvester_kcluster.sh.
import json
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import harvester  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


LAB = {
    "common": {"backend": "aws", "aws_nested_virtualization": "true", "SOURCE_IMAGE": "ami-0123"},
    "kclusters": {"hv": {"clu_type": "harvester", "harvester_token": "tok", "harvester_mtu": "9000",
                         "harvester_replica_count": "1", "harvester_config": {"os": {"dns_nameservers": ["1.1.1.1"]}}}},
    "nodes": {"h1": {"kcluster": "hv"}, "h2": {"kcluster": "hv", "harvester_role": "witness"}},
}

# ── node_config ────────────────────────────────────────────────────────────────────────────────────────────────────
create = harvester.node_config(LAB, "hv", "h1", "10.0.0.9", ["ssh-ed25519 K"], "$6$hash")
join = harvester.node_config(LAB, "hv", "h2", "10.0.0.9", ["ssh-ed25519 K"], "")
check("node_config: the first node creates the cluster with the VIP", create["install"]["mode"] == "create"
      and create["install"]["vip"] == "10.0.0.9" and create["install"]["vip_mode"] == "static")
check("node_config: the other nodes join through the VIP", join["install"]["mode"] == "join"
      and join["server_url"] == "https://10.0.0.9:443" and "vip" not in join["install"])
check("node_config: token, hostname, keys and the default AWS NTP server",
      create["token"] == "tok" and create["os"]["hostname"] == "h1" and create["os"]["ssh_authorized_keys"] == ["ssh-ed25519 K"]
      and create["os"]["ntp_servers"] == [harvester.AWS_NTP])
check("node_config: the password hash only when given", create["os"]["password"] == "$6$hash" and "password" not in join["os"])
check("node_config: MTU and the replica count (create node only)", create["install"]["management_interface"]["mtu"] == 9000
      and create["install"]["harvester"]["storage_class"]["replica_count"] == 1 and "harvester" not in join["install"])
check("node_config: harvester_role on the node", join["install"]["role"] == "witness" and "role" not in create["install"])
check("node_config: harvester_config merged in last, recursively",
      create["os"]["dns_nameservers"] == ["1.1.1.1"] and create["os"]["hostname"] == "h1")
check("user_data: the configuration as JSON", json.loads(harvester.user_data(create)) == create)
with mock.patch.object(harvester, "die", side_effect=SystemExit("no token")):
    try:
        harvester.node_config({"kclusters": {"hv": {"clu_type": "harvester"}}, "nodes": {"h1": {"kcluster": "hv"}}},
                              "hv", "h1", "v", [], "")
        check("node_config: dies without harvester_token", False)
    except SystemExit:
        pass

# ── problems ───────────────────────────────────────────────────────────────────────────────────────────────────────
check("problems: none for a valid lab", harvester.problems(LAB, lambda vm: "aws") == [])
bad = {"kclusters": {"hv": {"clu_type": "harvester", "addons": ["rancher"]}, "e": {"clu_type": "harvester"}},
       "nodes": {"h1": {"kcluster": "hv", "SOURCE_IMAGE": "harvester.qcow2"}}}
out = harvester.problems(bad, lambda vm: "libvirt")
for expected in ("'e' has no node", "'hv' has no harvester_token", "lists add-ons", "must use the aws backend",
                 "needs aws_nested_virtualization", "needs its Harvester AMI ID"):
    check("problems: reports " + expected, any(expected in p for p in out))

# ── wait_https ─────────────────────────────────────────────────────────────────────────────────────────────────────
answers = [urllib.error.URLError("refused"), OSError("reset"), None]


def urlopen(url, **kw):
    a = answers.pop(0)
    if a:
        raise a


with mock.patch.object(harvester.urllib.request, "urlopen", side_effect=urlopen) as opened, \
        mock.patch.object(harvester.time, "sleep") as slept:
    harvester.wait_https("10.0.0.5")
check("wait_https: retries until the node answers", opened.call_count == 3 and slept.call_count == 2
      and opened.call_args[0][0] == "https://10.0.0.5/")
with mock.patch.object(harvester.urllib.request, "urlopen",
                       side_effect=urllib.error.HTTPError("u", 401, "auth", {}, None)) as opened:
    harvester.wait_https("10.0.0.5")
check("wait_https: any HTTP status counts as an answer", opened.call_count == 1)
clock = iter([0, 0, 10, 10])
with mock.patch.object(harvester.urllib.request, "urlopen", side_effect=OSError("down")), \
        mock.patch.object(harvester.time, "time", side_effect=lambda: next(clock)), \
        mock.patch.object(harvester.time, "sleep"), \
        mock.patch.object(harvester, "die", side_effect=SystemExit("timeout")) as died:
    try:
        harvester.wait_https("10.0.0.5", timeout=5)
    except SystemExit:
        pass
check("wait_https: dies after the timeout", died.called)

# ── kubeconfig_path / fetch_kubeconfig ─────────────────────────────────────────────────────────────────────────────
check("kubeconfig_path: per cluster under /etc/lab_creation",
      harvester.kubeconfig_path("hv") == Path("/etc/lab_creation/harvester-hv.kubeconfig"))
remote = "clusters:\n- cluster:\n    server: https://127.0.0.1:6443\n  name: default\n"
dest = Path(tempfile.mkdtemp()) / "sub" / "hv.kubeconfig"
with mock.patch.object(harvester, "ssh_run", return_value=subprocess.CompletedProcess([], 0, remote, "")) as ssh:
    got = harvester.fetch_kubeconfig("10.0.0.5", dest, "https://10.0.0.5:6443", tls_server_name="localhost")
check("fetch_kubeconfig: reads rke2.yaml as rancher with sudo",
      ssh.call_args[0] == ("10.0.0.5", "sudo cat /etc/rancher/rke2/rke2.yaml") and ssh.call_args[1]["user"] == "rancher")
check("fetch_kubeconfig: sets the server and the TLS server name, mode 0600",
      got == dest and "server: https://10.0.0.5:6443\n    tls-server-name: localhost\n" in dest.read_text()
      and oct(dest.stat().st_mode & 0o777) == "0o600")

# ── wait_nodes_ready ───────────────────────────────────────────────────────────────────────────────────────────────
outputs = ["h1 NotReady control-plane\n", "h1 Ready control-plane\nh2 NotReady <none>\n",
           "h1 Ready control-plane\nh2 Ready <none>\n"]
with mock.patch.object(harvester.subprocess, "run",
                       side_effect=lambda *a, **k: subprocess.CompletedProcess(a, 0, outputs.pop(0), "")) as run, \
        mock.patch.object(harvester.time, "sleep"):
    harvester.wait_nodes_ready(Path("/k"), 2)
check("wait_nodes_ready: polls kubectl until every node is Ready",
      run.call_count == 3 and run.call_args[0][0] == ["kubectl", "--kubeconfig", "/k", "get", "nodes", "--no-headers"])

# ── apply_settings ─────────────────────────────────────────────────────────────────────────────────────────────────
with mock.patch.object(harvester.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
    harvester.apply_settings(Path("/k"), {"ntp-servers": "pool.ntp.org", "auto-disk-provision-paths": ""})
check("apply_settings: one Setting per entry, applied through the kubeconfig", run.call_count == 2
      and run.call_args_list[0][0][0] == ["kubectl", "--kubeconfig", "/k", "apply", "-f", "-"]
      and "kind: Setting\nmetadata:\n  name: ntp-servers\n" in run.call_args_list[0][1]["input"])
with mock.patch.object(harvester.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), \
        mock.patch.object(harvester, "die", side_effect=SystemExit("apply")) as died:
    try:
        harvester.apply_settings(Path("/k"), {"x": "1"})
    except SystemExit:
        pass
check("apply_settings: dies when kubectl apply fails", died.called)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all harvester kcluster checks passed")
