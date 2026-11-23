#!/usr/bin/env python3.11
# install_openshell — see 128_openshell.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

_loader = SourceFileLoader("install_openshell", str(_REPO / "scripts" / "install_openshell"))
_spec = importlib.util.spec_from_loader("install_openshell", _loader)
osh = importlib.util.module_from_spec(_spec)
_loader.exec_module(osh)

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def run(cfg):
    """setup_openshell() with `cfg`; returns (commands, printed lines, returned hostname)."""
    cmds, printed = [], []
    with mock.patch.object(osh, "ssh_run", side_effect=lambda host, cmd, **kw: cmds.append(cmd)), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        fqdn = osh.setup_openshell("node1", "c1", "mydemo.lab", cfg)
    return cmds, printed, fqdn


def run_appcollection(cfg):
    """setup_openshell() with the Application Collection's chart reference and pull-secret flags, as main() hands
    them over; returns the commands."""
    import appco_common as appco
    cmds = []

    def fake_ssh(host, cmd, **kw):
        cmds.append(cmd)
        return mock.Mock(returncode=0)

    with mock.patch.object(osh, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(appco, "ssh_run", side_effect=fake_ssh), \
            mock.patch("builtins.print"):
        chart, extra_set = appco.prepare_appcollection_chart(
            "node1", cfg.get("openshell_appcollection_chart") or osh._DEFAULT_APPCOLLECTION_CHART,
            cfg.get("openshell_ns") or "openshell", "lab-user", "lab-token")
        osh.setup_openshell("node1", "c1", "mydemo.lab", cfg, chart=chart, extra_set=extra_set)
    return cmds


def run_host(cfg):
    """setup_openshell_host() with `cfg`; returns (commands, printed lines)."""
    cmds, printed = [], []
    with mock.patch.object(osh, "ssh_run", side_effect=lambda host, cmd, **kw: cmds.append(cmd)), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        osh.setup_openshell_host("node1", cfg)
    return cmds, printed


# ── kubernetes mode, defaults ─────────────────────────────────────────────────
cmds, printed, fqdn = run({})
helm = [c for c in cmds if c.startswith("helm ")]
check("defaults: returns the gateway hostname openshell.<kcluster>.<domain>", fqdn == "openshell.c1.mydemo.lab")
check("defaults: one release, from the nvidia OCI chart, waiting for its workloads",
      len(helm) == 1 and helm[0].startswith("helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart")
      and "--wait" in helm[0] and "--version" not in helm[0])
check("defaults: namespace openshell, created idempotently and labelled for restricted Pod Security",
      any("kubectl create namespace openshell --dry-run=client" in c for c in cmds)
      and any(c.startswith("kubectl label namespace openshell pod-security.kubernetes.io/enforce=restricted")
              and "--overwrite" in c for c in cmds)
      and "--namespace openshell" in helm[0])
sandbox = [c for c in cmds if c.startswith("kubectl apply --server-side -f ")]
check("defaults: the Agent Sandbox manifest is applied from the pinned v1.0.11 release",
      len(sandbox) == 1 and sandbox[0].endswith(
          "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.11/sandbox.yaml"))
check("defaults: the Agent Sandbox CRDs exist before the chart is installed",
      cmds.index(sandbox[0]) < cmds.index(helm[0]))
check("defaults: unauthenticated clients allowed, TLS on, telemetry off",
      "--set server.auth.allowUnauthenticatedUsers=true" in helm[0]
      and "--set server.disableTls=false" in helm[0]
      and "--set server.telemetryEnabled=false" in helm[0])
check("defaults: no data-volume size, so the chart's own default applies",
      "server.dataStorageSize" not in helm[0])
check("defaults: the gateway certificate covers the hostname clients use",
      "--set pkiInitJob.serverDnsNames[0]=openshell.c1.mydemo.lab" in helm[0])
check("defaults: an idempotent ingress routes the hostname to openshell:8080",
      any(c.startswith("kubectl create ingress openshell -n openshell --rule=openshell.c1.mydemo.lab/*=openshell:8080 "
                       "--dry-run=client") and "kubectl apply -f -" in c for c in cmds))
check("defaults: says the gateway is open to unauthenticated clients",
      any("openshell_allow_unauthenticated" in p and "unauthenticated clients" in p for p in printed))
check("defaults: says sandbox isolation depends on the CNI enforcing egress NetworkPolicy",
      any("NetworkPolicy" in p for p in printed))
check("defaults: no missing-CRD hint when they were applied", not any("cannot create a sandbox" in p for p in printed))

# ── kubernetes mode, every field set ──────────────────────────────────────────
cmds, printed, fqdn = run({
    "openshell_ns": "agents", "openshell_shorthn": "gw", "openshell_version": "0.0.42",
    "openshell_chart": "oci://registry.mydemo.lab/openshell/helm-chart/",
    "openshell_allow_unauthenticated": "false", "openshell_disable_tls": "true",
    "openshell_telemetry": "true", "openshell_storage_size": "10Gi",
})
helm = [c for c in cmds if c.startswith("helm ")]
check("overrides: the hostname follows openshell_shorthn", fqdn == "gw.c1.mydemo.lab")
check("overrides: chart location, version and namespace reach helm",
      helm[0].startswith("helm upgrade --install openshell oci://registry.mydemo.lab/openshell/helm-chart ")
      and "--version 0.0.42" in helm[0] and "--namespace agents" in helm[0])
check("overrides: auth, TLS, telemetry and the data volume reach the chart",
      "--set server.auth.allowUnauthenticatedUsers=false" in helm[0]
      and "--set server.disableTls=true" in helm[0]
      and "--set server.telemetryEnabled=true" in helm[0]
      and "--set server.dataStorageSize=10Gi" in helm[0])
check("overrides: the ingress and the namespace label follow openshell_ns",
      any("kubectl create ingress openshell -n agents --rule=gw.c1.mydemo.lab/*=openshell:8080" in c for c in cmds)
      and any("kubectl label namespace agents pod-security" in c for c in cmds))
check("overrides: no open-gateway warning once unauthenticated clients are refused",
      not any("unauthenticated clients" in p for p in printed))

# ── the Agent Sandbox prerequisite ────────────────────────────────────────────
cmds, printed, _ = run({"openshell_agent_sandbox": "false"})
check("agent sandbox: 'false' applies no manifest and says what is missing",
      not any("kubectl apply --server-side" in c for c in cmds)
      and any("cannot create a sandbox" in p and "sandbox.yaml" in p for p in printed))
cmds, _, _ = run({"openshell_agent_sandbox_url": "https://mirror.mydemo.lab/sandbox.yaml"})
check("agent sandbox: openshell_agent_sandbox_url overrides the manifest",
      any(c == "kubectl apply --server-side -f https://mirror.mydemo.lab/sandbox.yaml" for c in cmds))

# ── the SUSE Application Collection path ──────────────────────────────────────
cmds = run_appcollection({})
helm = [c for c in cmds if c.startswith("helm upgrade")]
check("appcollection: the chart comes from the Collection's registry under its openshell slug",
      helm[0].startswith("helm upgrade --install openshell oci://dp.apps.rancher.io/charts/openshell "))
check("appcollection: the pods get the Collection's image pull secret",
      "--set 'global.imagePullSecrets[0]'=application-collection" in helm[0])
check("appcollection: an image pull secret is created and helm logs in to the registry",
      any("kubectl create secret docker-registry application-collection" in c
          and "--docker-server=dp.apps.rancher.io" in c for c in cmds)
      and any(c.startswith("helm registry login dp.apps.rancher.io --username lab-user --password-stdin")
              for c in cmds))
check("appcollection: the access token reaches no helm command line",
      not any("lab-token" in c for c in cmds if c.startswith("helm ")))
cmds = run_appcollection({"openshell_appcollection_chart": "nvidia-openshell", "openshell_ns": "agents"})
helm = [c for c in cmds if c.startswith("helm upgrade")]
check("appcollection: openshell_appcollection_chart overrides the slug, in the add-on's own namespace",
      helm[0].startswith("helm upgrade --install openshell oci://dp.apps.rancher.io/charts/nvidia-openshell ")
      and "--namespace agents" in helm[0]
      and any("kubectl create secret docker-registry application-collection" in c and "--namespace agents" in c
              for c in cmds))
cmds, _, _ = run({})
check("upstream: no pull secret and no Collection registry when the source is upstream",
      not any("application-collection" in c for c in cmds) and not any("dp.apps.rancher.io" in c for c in cmds))

# ── host mode ─────────────────────────────────────────────────────────────────
cmds, printed = run_host({})
check("host: the upstream installer runs on the node",
      cmds == ["curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"])
check("host: says how to create the first sandbox", any("openshell sandbox create --name demo" in p for p in printed))
cmds, _ = run_host({"openshell_installer_url": "https://mirror.mydemo.lab/install.sh"})
check("host: openshell_installer_url overrides the installer",
      cmds == ["curl -LsSf https://mirror.mydemo.lab/install.sh | sh"])

# ── validation ────────────────────────────────────────────────────────────────


def errors(cfg):
    v = ac.Validator({"openshell": cfg})
    osh._validate(v)
    return v.errors


check("validate: both modes are accepted, and an unset mode", errors({}) == []
      and all(errors({"openshell_mode": m}) == [] for m in osh._MODES))
check("validate: an unknown mode is rejected", len(errors({"openshell_mode": "podman"})) == 1)
check("validate: a non-version chart version is rejected", len(errors({"openshell_version": "latest"})) == 1)
check("validate: an invalid namespace is rejected", len(errors({"openshell_ns": "Agents_1"})) == 1)
check("validate: both URLs must be http(s)",
      len(errors({"openshell_agent_sandbox_url": "sandbox.yaml"})) == 1
      and len(errors({"openshell_installer_url": "file:///tmp/install.sh"})) == 1)
check("validate: openshell_source upstream needs no account, an unknown source is rejected",
      errors({"openshell_source": "upstream"}) == []
      and any("openshell_source" in e for e in errors({"openshell_source": "collection"})))
check("validate: openshell_source appcollection needs a user and a token",
      len([e for e in errors({"openshell_source": "appcollection"})
           if "openshell_appcollection_user" in e or "openshell_appcollection_password" in e]) == 2)
check("validate: a credential file satisfies both",
      errors({"openshell_source": "appcollection", "openshell_appcollection_account": "appco-lab"}) == [])
check("validate: appcollection and host mode are refused together",
      any("openshell_mode" in e for e in errors({"openshell_source": "appcollection", "openshell_mode": "host",
                                                 "openshell_appcollection_account": "appco-lab"})))
check("validate: the booleans only take true/false",
      len(errors({"openshell_disable_tls": "yes"})) == 1
      and len(errors({"openshell_agent_sandbox": "0"})) == 1
      and len(errors({"openshell_allow_unauthenticated": "True"})) == 1
      and len(errors({"openshell_telemetry": "off"})) == 1)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all openshell checks passed")
