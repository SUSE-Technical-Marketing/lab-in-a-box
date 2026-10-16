#!/usr/bin/env python3.11
# The fleet add-on: the resources it applies to Rancher's Fleet — see 99_fleet.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import apps  # noqa: E402

fleet = apps.load_addon_module("install_fleet", str(_REPO / "scripts"))

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── the manifests ────────────────────────────────────────────────────────────
group = json.loads(fleet.cluster_group_manifest("production", {"env": "prod"}, "fleet-default"))
check("a cluster group is a Fleet ClusterGroup in the workspace",
      group["apiVersion"] == "fleet.cattle.io/v1alpha1" and group["kind"] == "ClusterGroup"
      and group["metadata"] == {"name": "production", "namespace": "fleet-default"})
check("it selects clusters by label", group["spec"]["selector"]["matchLabels"] == {"env": "prod"})
check("label values are strings, whatever the lab wrote",
      json.loads(fleet.cluster_group_manifest("g", {"n": 3}, "w"))["spec"]["selector"]["matchLabels"] == {"n": "3"})

repo = json.loads(fleet.gitrepo_manifest(
    {"name": "apps", "repo": "https://github.com/rancher/fleet-examples", "branch": "master",
     "paths": ["simple"], "targets": [{"cluster_group": "production"}]}, "fleet-default"))
check("a GitRepo is a Fleet GitRepo in the workspace",
      repo["kind"] == "GitRepo" and repo["metadata"] == {"name": "apps", "namespace": "fleet-default"})
check("the repository, branch and paths come through",
      repo["spec"]["repo"].endswith("fleet-examples") and repo["spec"]["branch"] == "master"
      and repo["spec"]["paths"] == ["simple"])
check("a cluster group target is a clusterGroup",
      repo["spec"]["targets"] == [{"name": "target-0", "clusterGroup": "production"}])

selector = json.loads(fleet.gitrepo_manifest(
    {"name": "apps", "repo": "https://example.com/x.git",
     "targets": [{"name": "dev", "cluster_selector": {"env": "dev"}}]}, "w"))
check("a label target is a clusterSelector",
      selector["spec"]["targets"] == [{"name": "dev", "clusterSelector": {"matchLabels": {"env": "dev"}}}])
check("what the lab did not set is not sent",
      "branch" not in selector["spec"] and "paths" not in selector["spec"]
      and "paused" not in selector["spec"] and "serviceAccount" not in selector["spec"])

bare = json.loads(fleet.gitrepo_manifest({"name": "a", "repo": "https://x/y.git"}, "w"))
check("a GitRepo with no targets has none, so the workspace's default applies",
      "targets" not in bare["spec"])
paused = json.loads(fleet.gitrepo_manifest(
    {"name": "a", "repo": "https://x/y.git", "paused": "true", "revision": "v1.2.3",
     "service_account": "deployer"}, "w"))
check("paused, revision and the service account come through",
      paused["spec"]["paused"] is True and paused["spec"]["revision"] == "v1.2.3"
      and paused["spec"]["serviceAccount"] == "deployer")

example = fleet.examples_gitrepo("fleet-default")
check("the documented example points at rancher/fleet-examples' multi-cluster path",
      example["repo"] == fleet.EXAMPLES_REPO and example["paths"] == [fleet.EXAMPLES_PATH])
check("the example needs no targets of its own, since its fleet.yaml does the per-environment work",
      "targets" not in example)

# ── what it does on the cluster ──────────────────────────────────────────────
CFG = {"fleet_workspace": "fleet-default",
       "fleet_cluster_labels": [{"cluster": "c-m-abc", "labels": {"env": "prod"}}],
       "fleet_cluster_groups": [{"name": "production", "selector": {"env": "prod"}}],
       "fleet_gitrepos": [{"name": "apps", "repo": "https://x/y.git"}]}


def _run(cfg, crds=True):
    calls = []

    def fake(host, cmd, **kw):
        calls.append((cmd, kw.get("input_text")))
        return mock.Mock(returncode=0 if crds else 1, stdout="")

    with mock.patch.object(fleet, "ssh_run", side_effect=fake):
        result = fleet.setup_fleet("n1", cfg)
    return result, calls


result, calls = _run(CFG)
cmds = [c for c, _ in calls]
check("the workspace is created before anything goes in it",
      any("create namespace fleet-default" in c for c in cmds)
      and cmds.index(next(c for c in cmds if "create namespace" in c))
      < cmds.index(next(c for c in cmds if "label cluster" in c)))
check("the cluster is labelled with --overwrite, so a re-run is not an error",
      any("label cluster.fleet.cattle.io c-m-abc env=prod --overwrite" in c for c in cmds))
check("a cluster is labelled before the group that selects it is created",
      cmds.index(next(c for c in cmds if "label cluster" in c))
      < [i for i, (c, body) in enumerate(calls) if body and "ClusterGroup" in body][0])
check("every resource goes through kubectl apply, never create",
      all("kubectl apply -f -" in c for c, body in calls if body))
check("what it applied is reported back", result == {"labelled": 1, "groups": 1, "gitrepos": 1})

applied = [json.loads(body) for _, body in calls if body]
check("a ClusterGroup and a GitRepo were applied",
      sorted(a["kind"] for a in applied) == ["ClusterGroup", "GitRepo"])

result, calls = _run(dict(CFG, fleet_examples="true"))
bodies = [json.loads(b) for _, b in calls if b]
check("fleet_examples adds the documented GitRepo alongside the lab's own",
      result["gitrepos"] == 2
      and sorted(a["metadata"]["name"] for a in bodies if a["kind"] == "GitRepo")
      == ["apps", "fleet-examples-multi-cluster"])
result, _ = _run(dict(CFG, fleet_examples="false"))
check("fleet_examples false adds nothing", result["gitrepos"] == 1)

# ── no Fleet, no silent no-op ───────────────────────────────────────────────
try:
    _run(CFG, crds=False)
    check("a cluster without Fleet is refused", False)
except SystemExit:
    pass

# ── --check reports what is missing ─────────────────────────────────────────
import addon_common as ac  # noqa: E402

lab = {"nodes": {"n1": {"kcluster": "c1", "KUBERNETES_NODE_TYPE": "server"}},
       "kclusters": {"c1": {"clu_type": "rke2"}},
       "fleet": {"fleet_gitrepos": [{"name": "apps", "repo": "https://x/y.git"}]}}
chk = ac.Checker(lab, vm_name="n1")
with mock.patch.object(fleet, "ssh_run", side_effect=lambda h, c, **k: mock.Mock(returncode=1, stdout="")):
    fleet._check(chk)
check("--check says so when Fleet is not on the cluster",
      any("no Fleet CRDs" in p for p in chk.problems))

chk = ac.Checker(lab, vm_name="n1")
with mock.patch.object(fleet, "ssh_run",
                       side_effect=lambda h, c, **k: mock.Mock(returncode=0, stdout="https://x/y.git")):
    fleet._check(chk)
check("--check passes when the GitRepo points where the lab says", not chk.problems)

chk = ac.Checker(lab, vm_name="n1")
with mock.patch.object(fleet, "ssh_run",
                       side_effect=lambda h, c, **k: mock.Mock(returncode=0, stdout="https://elsewhere/z.git")):
    fleet._check(chk)
check("--check reports a GitRepo pointing somewhere else",
      any("expected 'https://x/y.git'" in p for p in chk.problems))

chk = ac.Checker({"nodes": {}}, vm_name="n1")
fleet._check(chk)
check("a lab with no fleet section has nothing to check", not chk.problems and chk.notes)

# ── the contract ────────────────────────────────────────────────────────────
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_addon.py"), "--quiet",
                    str(_REPO / "scripts" / "install_fleet")], stdout=subprocess.PIPE, universal_newlines=True)
check("it passes the add-on contract checker", r.returncode == 0)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"common": {}, "nodes": {}, "fleet": {"fleet_gitrepos": [{"name": "a", "repo": "nope"}]}}, fh)
r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_fleet"), "--validate", fh.name],
                   stdout=subprocess.PIPE, universal_newlines=True)
check("a GitRepo without a git URL is refused", r.returncode != 0 and "must be a git URL" in r.stdout)
Path(fh.name).unlink()

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all fleet checks passed")
