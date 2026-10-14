#!/usr/bin/env python3.11
# install_kagent.py — see 86_kagent.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

_loader = SourceFileLoader("install_kagent", str(_REPO / "scripts" / "install_kagent.py"))
_spec = importlib.util.spec_from_loader("install_kagent", _loader)
ka = importlib.util.module_from_spec(_spec)
_loader.exec_module(ka)

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def run(cfg):
    """setup_kagent() with `cfg`; returns (commands, printed lines, returned hostname)."""
    cmds, printed = [], []
    with mock.patch.object(ka, "ssh_run", side_effect=lambda host, cmd, **kw: cmds.append(cmd)), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        fqdn = ka.setup_kagent("node1", "c1", "mydemo.lab", cfg)
    return cmds, printed, fqdn


cmds, printed, fqdn = run({})
helm = [c for c in cmds if c.startswith("helm ")]
check("defaults: returns the UI hostname kagent.<kcluster>.<domain>", fqdn == "kagent.c1.mydemo.lab")
check("defaults: kagent-crds is installed before kagent, both from the ghcr.io OCI registry",
      len(helm) == 2 and helm[0].startswith("helm upgrade --install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds")
      and helm[1].startswith("helm upgrade --install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent"))
check("defaults: kagent waits for its workloads", "--wait" in helm[1])
check("defaults: namespace kagent-system, created idempotently",
      any("kubectl create namespace kagent-system --dry-run=client" in c for c in cmds)
      and all("--namespace kagent-system" in c for c in helm))
check("defaults: provider openAI, the UI's external URL set",
      "--set providers.default=openAI" in helm[1] and "--set ui.externalUrl=http://kagent.c1.mydemo.lab" in helm[1])
check("defaults: an idempotent ingress routes the UI hostname to kagent-ui:8080",
      any(c.startswith("kubectl create ingress kagent-ui -n kagent-system --rule=kagent.c1.mydemo.lab/*=kagent-ui:8080 "
                       "--dry-run=client") and "kubectl apply -f -" in c for c in cmds))
check("defaults: no API-key secret without kagent_api_key", not any("create secret" in c for c in cmds))
check("defaults: says how to add the missing key", any("kagent-openai" in p and "OPENAI_API_KEY" in p for p in printed))

cmds, printed, _ = run({"kagent_provider": "anthropic", "kagent_api_key": "sk-secret", "kagent_model": "claude-haiku-4-5",
                        "kagent_version": "0.10.3", "kagent_ns": "agents"})
secret = [c for c in cmds if "create secret" in c]
check("anthropic: the key goes to kagent-anthropic/ANTHROPIC_API_KEY through an environment variable",
      len(secret) == 1 and secret[0].startswith("KAGENT_API_KEY=sk-secret bash -c ")
      and "kagent-anthropic" in secret[0] and "ANTHROPIC_API_KEY" in secret[0])
check("anthropic: the key is on no helm command line", not any("sk-secret" in c for c in cmds if c.startswith("helm ")))
check("anthropic: the secret exists before kagent is installed",
      cmds.index(secret[0]) < next(i for i, c in enumerate(cmds) if c.startswith("helm upgrade --install kagent ")))
helm = [c for c in cmds if c.startswith("helm ")]
check("anthropic: provider, model and version reach the charts",
      "--set providers.default=anthropic" in helm[1] and "--set providers.anthropic.model=claude-haiku-4-5" in helm[1]
      and all("--version 0.10.3" in c for c in helm))
check("anthropic: no missing-key hint when the key is set", not any("cannot answer" in p for p in printed))

cmds, printed, _ = run({"kagent_provider": "ollama"})
helm = [c for c in cmds if c.startswith("helm ")]
check("ollama: defaults to the ollama addon's service",
      "--set providers.ollama.config.host=ollama.ollama.svc.cluster.local:11434" in helm[1])
check("ollama: needs no key and prints no hint", not any("create secret" in c for c in cmds)
      and not any("cannot answer" in p for p in printed))
cmds, _, _ = run({"kagent_provider": "ollama", "kagent_ollama_host": "ollama.ai.svc.cluster.local:11434"})
check("ollama: kagent_ollama_host overrides the server",
      any("--set providers.ollama.config.host=ollama.ai.svc.cluster.local:11434" in c for c in cmds))


def errors(cfg):
    v = ac.Validator({"kagent": cfg})
    ka._validate(v)
    return v.errors


check("validate: every listed provider is accepted", all(errors({"kagent_provider": p}) == [] for p in ka._PROVIDER_SECRETS))
check("validate: an unknown provider is rejected", len(errors({"kagent_provider": "openai"})) == 1)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all kagent checks passed")
