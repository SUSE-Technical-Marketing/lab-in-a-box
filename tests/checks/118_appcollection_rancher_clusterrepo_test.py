#!/usr/bin/env python3
# Unit tests for appco_common.register_rancher_cluster_repo/rancher_is_present (the Rancher
# ClusterRepo registration feature, so the SUSE Application Collection shows up in Rancher's own
# Apps & Marketplace UI, not just via the Helm CLI), its default-on wiring into
# install_appcollection (appcollection_standalone opts out), its trigger from install_rancher
# (rancher_install_appcollection), and the appcollection_3_0 migration that preserves pre-3.0
# behavior for existing labs. Mocked ssh_run, no real SSH.
# Run from 118_appcollection_rancher_clusterrepo.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import appco_common as appco  # noqa: E402
import migrations  # noqa: E402

ic = apps.load_addon_module("install_appcollection", str(_REPO / "scripts"))  # no .py: loaded by path
ir = apps.load_addon_module("install_rancher", str(_REPO / "scripts"))  # no .py: loaded by path

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
_missing_ns = set()


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    if cmd.startswith("kubectl get namespace"):
        ns = cmd.split()[-1]
        return FakeResult(returncode=1 if ns in _missing_ns else 0)
    return FakeResult(returncode=0)


appco.ssh_run = _fake_ssh_run
died = []
appco.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── register_rancher_cluster_repo: happy path ───────────────────────────────
calls.clear()
_missing_ns.clear()
appco.register_rancher_cluster_repo("rancher1.mydemo.lab", "appcouser", "appcotoken")
cmds = [c[1] for c in calls]
check("register_rancher_cluster_repo: checks both cattle-system and fleet-default exist",
      any("kubectl get namespace cattle-system" in c for c in cmds)
      and any("kubectl get namespace fleet-default" in c for c in cmds))
check("register_rancher_cluster_repo: creates the basic-auth secret in both namespaces",
      sum(1 for c in cmds if "kubernetes.io/basic-auth" in c and "--namespace cattle-system" in c) == 1
      and sum(1 for c in cmds if "kubernetes.io/basic-auth" in c and "--namespace fleet-default" in c) == 1)
check("register_rancher_cluster_repo: the real user/token are passed through, not placeholders",
      any("appcouser" in c for c in cmds)
      and any("appcotoken" in c for c in cmds if "bash -c" in c))
clusterrepo_input = next((i for _, c, i in calls if c == "kubectl apply -f -"), None)
check("register_rancher_cluster_repo: applies a real ClusterRepo manifest via stdin",
      clusterrepo_input is not None and "kind: ClusterRepo" in clusterrepo_input
      and "oci://dp.apps.rancher.io/charts" in clusterrepo_input
      and "name: application-collection" in clusterrepo_input)

# A custom registry/name are honored.
calls.clear()
appco.register_rancher_cluster_repo("rancher1.mydemo.lab", "u", "p", registry="my.registry.lab", name="my-repo")
clusterrepo_input = next((i for _, c, i in calls if c == "kubectl apply -f -"), None)
check("register_rancher_cluster_repo: honors a custom registry and ClusterRepo name",
      "oci://my.registry.lab/charts" in clusterrepo_input and "name: my-repo" in clusterrepo_input)

# Missing Rancher namespaces die with a clear, actionable message.
died.clear()
_missing_ns = {"cattle-system"}
try:
    appco.register_rancher_cluster_repo("notrancher.mydemo.lab", "u", "p")
except SystemExit:
    pass
check("register_rancher_cluster_repo: dies clearly when cattle-system is missing (no Rancher here)",
      len(died) == 1 and "cattle-system" in died[0] and "Rancher" in died[0])
_missing_ns = set()


# ── rancher_is_present: read-only dependency probe ──────────────────────────
_missing_ns = set()
check("rancher_is_present: True when both namespaces exist", appco.rancher_is_present("rancher1.mydemo.lab") is True)

_missing_ns = {"cattle-system"}
check("rancher_is_present: False when cattle-system is missing", appco.rancher_is_present("notrancher.lab") is False)

_missing_ns = {"fleet-default"}
check("rancher_is_present: False when fleet-default is missing", appco.rancher_is_present("notrancher.lab") is False)
_missing_ns = set()


# ── install_appcollection._validate ─────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vreq_or_credential(self, *a, **kw):
        pass

    def vver(self, *a, **kw):
        pass

    def vbool(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and v not in ("true", "false"):
            self.errors.append("[ERROR] bad bool")


v = FakeValidator({"appcollection": {"appcollection_standalone": "yes"}})
ic._validate(v)
check("_validate: an invalid appcollection_standalone value is an error", v.errors != [])

v = FakeValidator({"appcollection": {"appcollection_standalone": "true"}})
ic._validate(v)
check("_validate: 'true' is a valid appcollection_standalone value", v.errors == [])


# ── setup_appcollection: default requires+uses Rancher; appcollection_standalone opts out ──
register_calls = []
ic.appco_common.register_rancher_cluster_repo = lambda hostname, user, password, registry=None, **kw: (
    register_calls.append((hostname, user, password, registry))
)
ic.appco_common.helm_login = lambda *a, **kw: None
ic.appco_common.rancher_is_present = lambda hostname: hostname not in _no_rancher_hosts
_no_rancher_hosts = set()
died.clear()
ic.ac.die_not_ready = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)

# Default (no appcollection_standalone), Rancher present: registers the ClusterRepo automatically,
# with no opt-in field needed at all.
register_calls.clear()
ic.setup_appcollection("rancher1.mydemo.lab", {}, "u", "p")
check("setup_appcollection: registers the ClusterRepo by DEFAULT when Rancher is present",
      register_calls == [("rancher1.mydemo.lab", "u", "p", appco.DEFAULT_REGISTRY)])

register_calls.clear()
ic.setup_appcollection("rancher1.mydemo.lab", {"appcollection_registry": "my.registry.lab"}, "u", "p")
check("setup_appcollection: a custom appcollection_registry is passed through to the ClusterRepo registration",
      register_calls == [("rancher1.mydemo.lab", "u", "p", "my.registry.lab")])

# appcollection_standalone="true": never checks for Rancher, never registers a ClusterRepo, even
# if Rancher happens to be present.
register_calls.clear()
died.clear()
ic.setup_appcollection("rancher1.mydemo.lab", {"appcollection_standalone": "true"}, "u", "p")
check("setup_appcollection: appcollection_standalone='true' skips the ClusterRepo registration entirely",
      register_calls == [] and died == [])

# Default mode, Rancher NOT present: defers via die_not_ready(), not a hard failure — and does so
# BEFORE helm_login (the mandatory "check first" rule) by never reaching register_calls either.
_no_rancher_hosts = {"norancher.mydemo.lab"}
died.clear()
register_calls.clear()
try:
    ic.setup_appcollection("norancher.mydemo.lab", {}, "u", "p")
except SystemExit:
    pass
check("setup_appcollection: defers via die_not_ready() when Rancher isn't present yet, by default",
      len(died) == 1 and "rancher" in died[0].lower() and "appcollection_standalone" in died[0])
check("setup_appcollection: never gets to register_rancher_cluster_repo when it already deferred",
      register_calls == [])
_no_rancher_hosts = set()

# appcollection_standalone="true" on a cluster with NO Rancher at all: no dependency check, no die.
died.clear()
ic.setup_appcollection("norancher.mydemo.lab", {"appcollection_standalone": "true"}, "u", "p")
check("setup_appcollection: appcollection_standalone='true' never checks for Rancher at all", died == [])


# ── install_rancher: rancher_install_appcollection trigger ──────────────────
ir.appco_common.register_rancher_cluster_repo = lambda hostname, user, password, registry=None, **kw: (
    register_calls.append((hostname, user, password, registry))
)
ir.ac.resolve_credential = lambda cfg, kind, field_map, **kw: {
    "appcollection_user": cfg.get("appcollection_user") or "",
    "appcollection_password": cfg.get("appcollection_password") or "",
}
ir_died = []
ir.die = lambda m: ir_died.append(m) or (_ for _ in ()).throw(SystemExit)


def _rancher_main(rancher_cfg, appco_cfg):
    import os
    ir.os = os
    os.environ["_vm_name"] = "rancher1.mydemo.lab"
    os.environ["clu_name"] = "c1"
    ir.sys.argv = ["install_rancher", "/tmp/fake.json"]
    definition = {
        "nodes": {"rancher1.mydemo.lab": {"kcluster": "c1", "KUBERNETES_NODE_TYPE": "server"}},
        "kclusters": {"c1": {"clu_type": "rke2"}},
        "rancher": rancher_cfg,
        "appcollection": appco_cfg,
    }
    ir.primary.load_definition = lambda path: definition
    ir.primary.load_config = lambda: {}
    ir.k8s.node_role = lambda node_cfg: "server"
    ir.k8s.load_kclu_vars = lambda definition, clu_name: {"mydomain": "mydemo.lab", "clu_type": "rke2"}
    ir.setup_helm = lambda *a, **kw: None
    ir.setup_rancher_repo = lambda *a, **kw: None
    ir.setup_cert_manager = lambda *a, **kw: None
    ir.setup_rancher = lambda *a, **kw: None
    ir.main()


register_calls.clear()
ir_died.clear()
_rancher_main({}, {})
check("install_rancher: does NOT register a ClusterRepo by default", register_calls == [])

register_calls.clear()
ir_died.clear()
_rancher_main({"rancher_install_appcollection": "true"},
              {"appcollection_user": "u", "appcollection_password": "p"})
check("install_rancher: registers the ClusterRepo when rancher_install_appcollection is 'true', with "
      "NO separate appcollection addon entry needed",
      register_calls == [("rancher1.mydemo.lab", "u", "p", appco.DEFAULT_REGISTRY)])

register_calls.clear()
ir_died.clear()
try:
    _rancher_main({"rancher_install_appcollection": "true"}, {})
except SystemExit:
    pass
check("install_rancher: dies clearly when rancher_install_appcollection is 'true' but no appcollection "
      "credentials exist anywhere in this lab",
      len(ir_died) == 1 and "appcollection" in ir_died[0].lower())


# ── migrations.appcollection_3_0 ─────────────────────────────────────────────
# A kcluster using appcollection WITHOUT rancher: gets appcollection_standalone injected, preserving
# the pre-3.0 behavior exactly.
definition_no_rancher = {
    "kclusters": {"c1": {"addons": ["appcollection"]}},
}
changes = migrations.appcollection_3_0(definition_no_rancher)
check("appcollection_3_0: reports a change for a kcluster with appcollection but no rancher",
      len(changes) == 1 and "c1" in changes[0])
check("appcollection_3_0: injects appcollection_standalone='true', turning the plain string into an override",
      definition_no_rancher["kclusters"]["c1"]["addons"]
      == [{"appcollection": {"appcollection_standalone": "true"}}])

# Already a {"appcollection": {...}} override: the existing fields are kept, standalone is added.
definition_existing_override = {
    "kclusters": {"c1": {"addons": [{"appcollection": {"appcollection_chart": "redis"}}]}},
}
migrations.appcollection_3_0(definition_existing_override)
check("appcollection_3_0: an existing override keeps its other fields, standalone is merged in",
      definition_existing_override["kclusters"]["c1"]["addons"][0]
      == {"appcollection": {"appcollection_chart": "redis", "appcollection_standalone": "true"}})

# A kcluster with BOTH appcollection and rancher: left completely alone (the new default is the
# intended behavior there, not something to migrate away from).
definition_with_rancher = {
    "kclusters": {"c1": {"addons": ["rancher", "appcollection"]}},
}
changes = migrations.appcollection_3_0(definition_with_rancher)
check("appcollection_3_0: no change when rancher is ALSO on the same kcluster",
      changes == [] and definition_with_rancher["kclusters"]["c1"]["addons"] == ["rancher", "appcollection"])

# A kcluster with no appcollection at all: untouched, no spurious change.
definition_unrelated = {"kclusters": {"c1": {"addons": ["longhorn"]}}}
changes = migrations.appcollection_3_0(definition_unrelated)
check("appcollection_3_0: no-op for a kcluster that doesn't use appcollection at all",
      changes == [] and definition_unrelated["kclusters"]["c1"]["addons"] == ["longhorn"])

# Old single-cluster format ("cluster" instead of "kclusters"): same logic applies.
definition_old_format = {"cluster": {"clu_name": "c1"}, "addons": ["appcollection"]}
changes = migrations.appcollection_3_0(definition_old_format)
check("appcollection_3_0: the old single-'cluster' format is migrated too",
      len(changes) == 1
      and definition_old_format["addons"] == [{"appcollection": {"appcollection_standalone": "true"}}])

check("appcollection_3_0: registered in migrations.MIGRATIONS", "appcollection_3_0" in migrations.MIGRATIONS)


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All appcollection/Rancher-ClusterRepo checks passed")
