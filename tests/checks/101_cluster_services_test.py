#!/usr/bin/env python3.11
# The ingress controller and storage class of a kcluster — see 101_cluster_services.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import apps  # noqa: E402
import k8s  # noqa: E402
import lab_schema_base  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def lab(clu_type, **kw):
    return {"nodes": {}, "kclusters": {"c1": dict(kw, clu_type=clu_type)}}


# ── what each distribution brings itself ────────────────────────────────────
check("k3s brings both an ingress controller and a storage class",
      set(k8s.K3sDistro.bundled) == {apps.CLUSTER_INGRESS, apps.CLUSTER_STORAGECLASS})
check("rke2 brings an ingress controller and no storage class",
      set(k8s.RKE2Distro.bundled) == {apps.CLUSTER_INGRESS})
check("upstream Kubernetes brings neither", set(k8s.KubeadmDistro.bundled) == set())
check("harvester brings both, and does not let them be replaced",
      set(k8s.HarvesterDistro.bundled) == {apps.CLUSTER_INGRESS, apps.CLUSTER_STORAGECLASS}
      and k8s.HarvesterDistro.can_disable_bundled is False)
for name, cls in k8s.DISTROS.items():
    check("{}: every need it bundles names the value that supplies it".format(name),
          all(need in cls.bundled_choice for need in cls.bundled))

# ── nothing is installed that the lab did not ask for ───────────────────────
check("a cluster whose distribution brings both gets nothing", k8s.cluster_services(lab("k3s"), "c1") == [])
check("a cluster that brings neither gets nothing while nothing needs it",
      k8s.cluster_services(lab("kubeadm"), "c1") == [])
check("an existing rke2 lab is left exactly as it was", k8s.cluster_services(lab("rke2"), "c1") == [])

# ── an add-on's requires_cluster is what pulls one in ──────────────────────
with mock.patch.object(apps, "load_plugin", return_value={"requires_cluster": ["ingress"]}):
    check("an add-on that needs an ingress controller gets one on a cluster with none",
          k8s.cluster_services(lab("kubeadm", addons=["rancher"]), "c1") == ["nginx"])
    check("...and nothing on a cluster that already has one",
          k8s.cluster_services(lab("rke2", addons=["rancher"]), "c1") == [])
with mock.patch.object(apps, "load_plugin", return_value={"requires_cluster": ["ingress", "storageclass"]}):
    check("the ingress controller comes before the storage class that follows it",
          k8s.cluster_services(lab("kubeadm", addons=["harbor"]), "c1") == ["nginx", "longhorn"])
    check("the SUSE-friendly defaults are nginx and Longhorn",
          (k8s.DEFAULT_INGRESS, k8s.DEFAULT_STORAGE) == ("nginx", "longhorn"))
with mock.patch.object(apps, "load_plugin", return_value={"requires_cluster": ["storageclass"],
                                                          "provides_cluster": []}):
    check("an add-on the lab already lists is not installed twice",
          k8s.cluster_services(lab("kubeadm", addons=["longhorn", "harbor"]), "c1") == [])

# ── the lab's own choice ────────────────────────────────────────────────────
check("a named ingress controller is installed even when nothing requires it",
      k8s.cluster_services(lab("kubeadm", clu_ingress="traefik"), "c1") == ["traefik"])
check("a named storage class is installed on its own",
      k8s.cluster_services(lab("kubeadm", clu_storage="local-path"), "c1") == ["local_path"])
check("\"none\" installs nothing", k8s.cluster_services(lab("kubeadm", clu_ingress="none"), "c1") == [])
check("naming what the distribution already brings installs nothing",
      k8s.cluster_services(lab("k3s", clu_ingress="traefik"), "c1") == []
      and k8s.cluster_services(lab("rke2", clu_ingress="nginx"), "c1") == [])
check("a different controller replaces the bundled one",
      k8s.cluster_services(lab("rke2", clu_ingress="traefik"), "c1") == ["traefik"])
check("a different storage class replaces k3s's own",
      k8s.cluster_services(lab("k3s", clu_storage="longhorn"), "c1") == ["longhorn"])
for field, value in (("clu_ingress", "istio"), ("clu_storage", "nfs")):
    try:
        k8s.cluster_services(lab("kubeadm", **{field: value}), "c1")
        check("{} '{}' is refused".format(field, value), False)
    except SystemExit:
        pass
try:
    k8s.cluster_services(lab("harvester", clu_storage="local-path"), "c1")
    check("harvester's own storage cannot be replaced", False)
except SystemExit:
    pass

# ── what the cluster will have, for the preflight to weigh ─────────────────
check("a k3s cluster provides both", k8s.cluster_provides(lab("k3s"), "c1")
      == {apps.CLUSTER_INGRESS, apps.CLUSTER_STORAGECLASS})
check("a bare kubeadm cluster provides nothing", k8s.cluster_provides(lab("kubeadm"), "c1") == set())
check("one with a named ingress controller provides it",
      k8s.cluster_provides(lab("kubeadm", clu_ingress="nginx"), "c1") == {apps.CLUSTER_INGRESS})
with mock.patch.object(apps, "load_plugin", return_value={"provides_cluster": ["storageclass"]}):
    check("an add-on the lab lists itself provides it too",
          k8s.cluster_provides(lab("kubeadm", addons=["longhorn"]), "c1") == {apps.CLUSTER_STORAGECLASS})
check("what the cluster is given is recorded need by need, not guessed from the add-on's name",
      k8s.cluster_plan(lab("kubeadm", clu_ingress="nginx", clu_storage="local-path"), "c1")
      == [(apps.CLUSTER_INGRESS, "nginx"), (apps.CLUSTER_STORAGECLASS, "local_path")])

NEEDS_INGRESS = dict(apps.DEFAULT_PLUGIN, name="x", requires_cluster=["ingress"])
check("an add-on that needs an ingress controller is refused on a cluster with none",
      "ingress" in (apps.requirement_issue(NEEDS_INGRESS, "container", clu_type="kubeadm", provides=set()) or ""))
check("the message names the field that fixes it",
      "clu_ingress" in (apps.requirement_issue(NEEDS_INGRESS, "container", clu_type="kubeadm",
                                               provides=set()) or ""))
check("...and allowed on a cluster that has one",
      apps.requirement_issue(NEEDS_INGRESS, "container", clu_type="rke2",
                             provides={apps.CLUSTER_INGRESS}) is None)
check("a caller that does not know what the cluster brings is not second-guessed",
      apps.requirement_issue(NEEDS_INGRESS, "container", clu_type="rke2") is None)

# ── the bundled component is left out when something else supplies it ──────
check("k3s keeps traefik and local-path when the lab says nothing",
      k8s.K3sDistro()._disable_flags({}) == "")
check("k3s is installed without traefik when another controller is chosen",
      k8s.K3sDistro()._disable_flags({"clu_ingress": "nginx"}) == " --disable traefik")
check("k3s is installed without local-storage when another storage class is chosen",
      "--disable local-storage" in k8s.K3sDistro()._disable_flags({"clu_storage": "longhorn"}))
check("rke2's bundled nginx is disabled in the node config it reads",
      apps.CLUSTER_INGRESS in k8s.cluster_disabled({"clu_ingress": "traefik"}, k8s.RKE2Distro())
      and k8s.cluster_disabled({}, k8s.RKE2Distro()) == set())

written = {}
with mock.patch.object(k8s.Path, "mkdir"), \
     mock.patch.object(k8s.Path, "exists", return_value=False), \
     mock.patch.object(k8s.Path, "write_text", lambda self, text: written.__setitem__(self.name, text)):
    k8s.RKE2Distro().write_node_config("c1", {"mydomain": "lab", "clu_ingress": "traefik"})
check("the disable goes into both rke2 config files",
      written and all("rke2-ingress-nginx" in t for t in written.values()))
written.clear()
with mock.patch.object(k8s.Path, "mkdir"), \
     mock.patch.object(k8s.Path, "exists", return_value=False), \
     mock.patch.object(k8s.Path, "write_text", lambda self, text: written.__setitem__(self.name, text)):
    k8s.RKE2Distro().write_node_config("c1", {"mydomain": "lab"})
check("an rke2 cluster that was not asked to change keeps its own config",
      written and not any("disable" in t for t in written.values()))

# ── the schema says the same as the registry ───────────────────────────────
fields = {f["name"]: f for f in lab_schema_base.base_lab_schema()["sections"]["kclusters"]["fields"]}
check("the kcluster offers both fields", "clu_ingress" in fields and "clu_storage" in fields)
check("clu_ingress offers exactly the controllers k8s.py can install",
      sorted(fields["clu_ingress"]["enum"]) == sorted(k8s.INGRESS_ADDONS))
check("clu_storage offers exactly the storage classes k8s.py can install",
      sorted(fields["clu_storage"]["enum"]) == sorted(k8s.STORAGE_ADDONS))
check("the lab schema records the change", lab_schema_base.LAB_SCHEMA_VERSION != "2.3")

# ── every declaration names a need that exists, and someone provides it ────
import ast  # noqa: E402

declared, providers = {}, set()
for path in sorted((_REPO / "scripts").glob("install_*")):
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        continue
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PLUGIN" for t in node.targets):
            plugin = ast.literal_eval(node.value)
            declared[path.name] = plugin.get("requires_cluster") or []
            if plugin.get("provides_cluster"):
                providers.add(path.name)
                check("{}: what it provides is a known need".format(path.name),
                      all(n in apps.CLUSTER_NEEDS for n in plugin["provides_cluster"]))
for name, needs in declared.items():
    check("{}: what it requires is a known need".format(name), all(n in apps.CLUSTER_NEEDS for n in needs))
check("add-ons do declare what they need of a cluster", sum(1 for n in declared.values() if n) > 15)
check("every need has an add-on that provides it",
      {"install_nginx", "install_traefik", "install_longhorn", "install_local_path"} <= providers)

# ── Longhorn is sized to the cluster it lands on ───────────────────────────
lh = apps.load_addon_module("install_longhorn", str(_REPO / "scripts"))
one = lh.replica_flags(1)
check("a single-node cluster asks for one replica of a volume",
      "defaultSettings.defaultReplicaCount=1" in one)
check("...and one of each CSI sidecar, whose chart defaults to three",
      all("csi.{}ReplicaCount=1".format(part) in one
          for part in ("attacher", "provisioner", "resizer", "snapshotter")))
check("...and lets a replica share the only node there is",
      "replicaSoftAntiAffinity=true" in one)
check("a two-node cluster asks for two", "defaultReplicaCount=2" in lh.replica_flags(2))
check("a larger cluster stops at Longhorn's own default of three",
      "defaultReplicaCount=3" in lh.replica_flags(9) and "defaultReplicaCount=9" not in lh.replica_flags(9))
check("anti-affinity is not relaxed where there is somewhere else to put a replica",
      "replicaSoftAntiAffinity" not in lh.replica_flags(3))
check("the node count comes from the lab",
      k8s.kcluster_node_count({"nodes": {"a": {"kcluster": "c1"}, "b": {"kcluster": "c1"},
                                         "c": {"kcluster": "other"}}}, "c1") == 2)

# ── the local-path add-on ──────────────────────────────────────────────────
lp = apps.load_addon_module("install_local_path", str(_REPO / "scripts"))
check("it says it provides a storage class", lp.PLUGIN["provides_cluster"] == ["storageclass"])
check("it runs on any Kubernetes distribution", lp.PLUGIN["requires_kubernetes"] is None)
patch = json.loads(lp.config_patch("/data/lp"))
check("the directory patch is a ConfigMap merge patch holding valid JSON",
      json.loads(patch["data"]["config.json"])["nodePathMap"][0]["paths"] == ["/data/lp"])

calls = []


def _ssh(host, cmd, **kw):
    calls.append(cmd)
    return mock.Mock(returncode=0, stdout="")


with mock.patch.object(lp, "ssh_run", side_effect=_ssh):
    lp.setup_local_path("n1", {})
text = "\n".join(calls)
check("the release named by the lab is applied, never created",
      "kubectl apply" in text and lp.DEFAULT_VERSION in text and "kubectl create -f" not in text)
check("its storage class is annotated as the cluster default",
      "is-default-class" in text and '"true"' in text)
check("the manifest's own directory is left alone when the lab names none",
      "patch configmap" not in text)

calls = []
with mock.patch.object(lp, "ssh_run", side_effect=_ssh):
    lp.setup_local_path("n1", {"local_path_dir": "/data/lp", "local_path_default": "false"})
text = "\n".join(calls)
check("a lab that names a directory gets it patched in and the provisioner restarted",
      "patch configmap" in text and "rollout restart" in text)
check("local_path_default false leaves another storage class as the default", '"false"' in text)

with mock.patch.object(lp, "ssh_run", side_effect=lambda h, c, **k: mock.Mock(returncode=1, stdout="")):
    try:
        lp.setup_local_path("n1", {})
        check("a manifest that cannot be applied is an error", False)
    except SystemExit:
        pass

r = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_addon.py"), "--quiet",
                    str(_REPO / "scripts" / "install_local_path")], stdout=subprocess.PIPE, universal_newlines=True)
check("it passes the add-on contract checker", r.returncode == 0)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump({"common": {}, "nodes": {}, "local_path": {"local_path_dir": "relative/path",
                                                         "local_path_version": "0.0.31"}}, fh)
out = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_local_path"), "--validate", fh.name],
                     stdout=subprocess.PIPE, universal_newlines=True)
check("a relative directory and a version with no tag are both refused",
      out.returncode != 0 and "must be an absolute path" in out.stdout and "release tag" in out.stdout)
Path(fh.name).unlink()

# ── the orchestrator installs them before the add-ons that need them ──────
source = (_REPO / "scripts" / "setup_lab.py").read_text()
check("setup_lab puts what the cluster needs in front of its own add-ons",
      "k8s.cluster_services(definition, clu_name)" in source and "addons = services + list(addons)" in source)
check("...and tells the requirement check what the cluster provides",
      "provides=k8s.cluster_provides(definition, clu_name)" in source)
check("the preflight does the same",
      "cluster_provides(definition, clu)" in (_REPO / "libs" / "lab_creation.py").read_text())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cluster-service checks passed")
