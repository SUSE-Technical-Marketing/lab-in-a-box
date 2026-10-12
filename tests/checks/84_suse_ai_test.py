#!/usr/bin/env python3.11
# install_suse_ai.py — see 84_suse_ai.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

_loader = SourceFileLoader("install_suse_ai", str(_REPO / "scripts" / "install_suse_ai.py"))
_spec = importlib.util.spec_from_loader("install_suse_ai", _loader)
sa = importlib.util.module_from_spec(_spec)
_loader.exec_module(sa)

import appco_common  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


def run(cfg, crd_present=False, login_rc=0):
    """setup_suse_ai() with `cfg`; returns (commands, inputs, exit code or None)."""
    cmds, inputs = [], []

    def fake_ssh(host, cmd, check=True, input_text=None, **kw):
        cmds.append(cmd)
        if input_text:
            inputs.append(input_text)
        if cmd.startswith("kubectl get crd"):
            return Result(0 if crd_present else 1)
        if cmd.startswith("helm registry login"):
            return Result(login_rc)
        return Result(0)

    creds = {"appcollection_user": "appuser", "appcollection_password": "apptoken"}
    code = None
    with mock.patch.object(sa, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(appco_common, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(sa.ac, "resolve_credential", return_value=creds), \
            mock.patch.object(sa.primary, "load_service_credential", return_value={"scc_regcode": "AIREG"}):
        try:
            sa.setup_suse_ai("node1", "c1", "lab", cfg)
        except SystemExit as e:
            code = e.code
    return cmds, inputs, code


def installs(cmds):
    return [c for c in cmds if c.startswith("helm upgrade --install")]


# ── ordering and derived values ──────────────────────────────────────────────────────────────────────────────────────
check("ordered: milvus, ollama, open-webui first, unknown components after",
      sa.ordered(["open-webui", "extra", "milvus", "ollama"]) == ["milvus", "ollama", "open-webui", "extra"])
owui = dict(sa.component_values("open-webui", {}, ["milvus", "ollama", "open-webui"], "ai-ns", "ai.c1.lab"))
check("open-webui: uses the separate Ollama service, not its embedded one",
      owui.get("ollama.enabled") == "false" and owui.get("ollamaUrls[0]") == "http://ollama.ai-ns.svc.cluster.local:11434")
check("open-webui: Milvus is its vector database when installed",
      owui.get("extraEnvVars[0].value") == "milvus"
      and owui.get("extraEnvVars[1].value") == "http://milvus.ai-ns.svc.cluster.local:19530")
check("open-webui: ingress host is the printed address, TLS from the default source",
      owui.get("ingress.host") == "ai.c1.lab" and owui.get("global.tls.source") == "suse-private-ai")
alone = dict(sa.component_values("open-webui", {}, ["open-webui"], "ai-ns", "ai.c1.lab"))
check("open-webui alone: keeps its embedded Ollama and no vector DB setting",
      "ollama.enabled" not in alone and "extraEnvVars[0].name" not in alone)
oll = sa.component_values("ollama", {"suse_ai_ollama_models": "gemma:2b llama3.1", "suse_ai_gpu": "true"}, [], "n", "f")
check("ollama: models pulled at start and the NVIDIA GPU settings",
      ("ollama.models.pull[1]", "llama3.1") in oll and ("ollama.gpu.enabled", "true") in oll
      and ("ollama.gpu.type", "nvidia") in oll)
check("ollama: no GPU settings by default", not any(k.startswith("ollama.gpu") for k, _ in sa.component_values("ollama", {}, [], "n", "f")))
check("milvus: standalone, without Kafka", ("cluster.enabled", "false") in sa.component_values("milvus", {}, [], "n", "f")
      and ("kafka.enabled", "false") in sa.component_values("milvus", {}, [], "n", "f"))
check("pull_secret_pairs: names, or {name:} objects for cert-manager",
      sa.pull_secret_pairs(["a", "b"]) == [("global.imagePullSecrets[0]", "a"), ("global.imagePullSecrets[1]", "b")]
      and sa.pull_secret_pairs(["a"], named=True) == [("global.imagePullSecrets[0].name", "a")])
check("extra_set_pairs: key=value pairs, values may contain '='",
      sa.extra_set_pairs("a=1 bad b=x=y") == [("a", "1"), ("b", "x=y")])

# ── setup_suse_ai(): full flow ───────────────────────────────────────────────────────────────────────────────────────
cmds, inputs, code = run({"suse_ai_components": "open-webui milvus ollama", "suse_ai_extra_set_milvus": "a.b=c"})
inst = installs(cmds)
check("setup: components installed in dependency order after cert-manager",
      [c.split()[3] for c in inst] == ["cert-manager", "milvus", "ollama", "open-webui"])
check("setup: every component chart gets the application-collection pull secret",
      all("--set 'global.imagePullSecrets[0]'=application-collection" in c for c in inst[1:]))
check("setup: cert-manager is installed from the Application Collection with its CRDs and the {name:} pull secret",
      "oci://dp.apps.rancher.io/charts/cert-manager" in inst[0] and "crds.enabled=true" in inst[0]
      and "'global.imagePullSecrets[0].name'=application-collection" in inst[0])
check("setup: the pull secret also exists in cert-manager's namespace",
      any("create secret docker-registry application-collection" in c and "--namespace cert-manager" in c for c in cmds))
check("setup: Helm logs in to <registry>/charts with the token on stdin, never on the command line",
      any(c.startswith("helm registry login dp.apps.rancher.io/charts --username appuser") for c in cmds)
      and inputs == ["apptoken"] and not any("apptoken" in c for c in cmds if c.startswith("helm")))
check("setup: suse_ai_extra_set_<component> goes to that component only",
      "a.b=c" in next(c for c in inst if " milvus " in c) and not any("a.b=c" in c for c in inst if " milvus " not in c))
check("setup: no registry.suse.com login without a SUSE AI registration code",
      not any("registry.suse.com" in c for c in cmds))
cmds, _, _ = run({}, crd_present=True)
check("setup: cert-manager is left alone when its CRDs exist", [c.split()[3] for c in installs(cmds)] == ["ollama", "open-webui"])
cmds, _, _ = run({"suse_ai_tls_source": "secret"})
check("setup: no cert-manager with suse_ai_tls_source=secret",
      not any(c.startswith("kubectl get crd") for c in cmds) and "cert-manager" not in " ".join(installs(cmds)))
cmds, inputs, _ = run({"suse_ai_scc_account": "scc-ai"}, crd_present=True)
check("setup: the SCC account's regcode adds the suse-ai-registry secret, a registry.suse.com login and a second pull secret",
      any("create secret docker-registry suse-ai-registry --docker-server=registry.suse.com --docker-username=regcode" in c
          for c in cmds)
      and any(c.startswith("helm registry login registry.suse.com --username regcode") for c in cmds) and "AIREG" in inputs
      and all("--set 'global.imagePullSecrets[1]'=suse-ai-registry" in c for c in installs(cmds)))
check("suse_regcode: plaintext suse_ai_regcode when no SCC account is named", sa.suse_regcode({"suse_ai_regcode": "X"}) == "X")
_, _, code = run({}, login_rc=1)
check("setup: a failed registry login exits 1", code == 1)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all suse_ai checks passed")
