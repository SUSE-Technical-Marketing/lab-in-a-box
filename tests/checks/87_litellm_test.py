#!/usr/bin/env python3.11
# libs/litellm_common.py and its add-ons — see 87_litellm.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import litellm_common as lc  # noqa: E402

failures = []


import apps  # noqa: E402


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def load(name):
    """An add-on's module. It has no .py extension, so importlib cannot infer a loader from the path."""
    return apps.load_addon_module(name, str(_REPO / "scripts"))


def run(fn, *args, **kwargs):
    cmds = []
    with mock.patch.object(lc, "ssh_run", side_effect=lambda host, cmd, **kw: cmds.append(cmd)), \
            mock.patch("builtins.print"):
        result = fn(*args, **kwargs)
    return cmds, result


cmds, _ = run(lc.setup_litellm_proxy, "node1", "gemini-proxy", "gemini", "gemini.c1.lab", "gemini",
              "gemini/gemini-2.5-flash", "AIzaSECRET")
helm = [c for c in cmds if c.startswith("helm ")]
check("one helm upgrade --install from the OCI chart, waiting",
      len(helm) == 1 and helm[0].startswith("helm upgrade --install gemini-proxy oci://ghcr.io/berriai/litellm-helm --namespace gemini")
      and "--wait" in helm[0])
check("the API key is on no command line except as an environment variable assignment",
      all("AIzaSECRET" not in c or c.startswith("PROVIDER_API_KEY=AIzaSECRET bash -c ") for c in cmds))
check("the provider secret is applied idempotently",
      any("create secret generic gemini-proxy-provider" in c and "kubectl apply -f -" in c for c in cmds))
master = [c for c in cmds if "gemini-proxy-master-key" in c and "create secret" in c]
check("the master key is created only when missing",
      len(master) == 1 and "kubectl get secret gemini-proxy-master-key" in master[0] and "||" in master[0])
check("the secrets exist before the chart is installed", cmds.index(master[0]) < cmds.index(helm[0]))
for flag in ("db.deployStandalone=false", "migrationJob.enabled=false", "masterkeySecretName=gemini-proxy-master-key",
             "environmentSecrets[0]=gemini-proxy-provider", "proxy_config.model_list[0].model_name=gemini",
             "proxy_config.model_list[0].litellm_params.model=gemini/gemini-2.5-flash",
             "proxy_config.model_list[0].litellm_params.api_key=os.environ/PROVIDER_API_KEY",
             "ingress.enabled=true", "ingress.hosts[0].host=gemini.c1.lab", "ingress.hosts[0].paths[0].pathType=Prefix"):
    check("helm sets " + flag, "--set " + flag in helm[0] or "--set '{}'".format(flag) in helm[0])
check("no api_base unless given", "api_base" not in helm[0])

cmds, _ = run(lc.setup_litellm_proxy, "node1", "kimi-proxy", "kimi", "kimi.c1.lab", "kimi", "moonshot/kimi-k3", "k",
              chart="oci://mirror/litellm-helm", version="1.105.0", api_base="https://api.moonshot.cn/v1")
helm = [c for c in cmds if c.startswith("helm ")][0]
check("chart, version and api_base are passed",
      "oci://mirror/litellm-helm" in helm and "--version 1.105.0" in helm
      and "proxy_config.model_list[0].litellm_params.api_base=https://api.moonshot.cn/v1" in helm)

for name, model_name, model in (("gemini", "gemini", "gemini/gemini-2.5-flash"), ("openai", "gpt", "openai/gpt-5"),
                                ("anthropic", "claude", "anthropic/claude-sonnet-5"), ("kimi", "kimi", "moonshot/kimi-k3")):
    addon = load(name)
    with mock.patch.object(addon.litellm_common, "setup_litellm_proxy") as proxy:
        fqdn = getattr(addon, "setup_" + name)("node1", "c1", "lab", {name + "_api_key": "k"})
    args, kwargs = proxy.call_args
    check("{}: release {}-proxy, default hostname and model".format(name, name),
          args[1:7] == (name + "-proxy", name, "{}.c1.lab".format(name), model_name, model, "k") and fqdn == "{}.c1.lab".format(name))
    if name == "kimi":
        check("kimi: Moonshot's global endpoint by default", kwargs.get("api_base") == "https://api.moonshot.ai/v1")

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all litellm checks passed")
