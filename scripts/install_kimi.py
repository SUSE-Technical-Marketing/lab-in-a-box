#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a Moonshot AI Kimi proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.1
#
# JSON section: "kimi" — configurable keys:
#   kimi_api_key    : [MANDATORY] Moonshot AI API key (from https://platform.moonshot.ai)
#   kimi_model      : [OPTIONAL] default model (default: moonshot/kimi-k3)
#                      Examples: moonshot/kimi-k2.6 (cheaper), moonshot/kimi-k2.7-code (coding-focused),
#                      moonshot/moonshot-v1-8k
#   kimi_api_base   : [OPTIONAL] override endpoint, Moonshot's global endpoint by default; use
#                      https://api.moonshot.cn/v1 for the China region (default: https://api.moonshot.ai/v1)
#   kimi_version    : [OPTIONAL] LiteLLM Helm chart version (empty = latest)
#   kimi_ns         : [OPTIONAL] namespace (default: kimi)
#   kimi_shorthn    : [OPTIONAL] hostname prefix (default: kimi)
#   kimi_rel        : [OPTIONAL] Helm repo alias (default: litellm)
#   kimi_repo_url   : [OPTIONAL] Helm repo URL (default: https://berriai.github.io/litellm)
#
# IMPORTANT — this is NOT a local/self-hosted model, unlike "ollama"/"deepseek"/"apertus": Kimi K3 is a
# 2.8-trillion-parameter model (Moonshot's own "world's largest open-weight" claim) — genuinely too
# large to self-host on this project's lab hardware, and Ollama's own library only carries it as a
# "cloud" pass-through tag (inference on Ollama's own servers, not local). This addon instead proxies
# Moonshot AI's hosted API through LiteLLM, same shape as the "gemini"/"anthropic"/"openai" addons — a
# real API key and network egress to api.moonshot.ai are required, same as those.
#
# LICENSE NOTE: Kimi K3's weights are published under Moonshot's own custom license (not a permissive
# Apache/MIT-style one) which requires a separate commercial agreement with Moonshot AI once an
# offering built on it serves >US$20M/year in revenue to third parties — irrelevant for a lab/demo
# deployment, but worth knowing before basing a real product on it.
#
# The moonshot/ LiteLLM provider prefix is used. The key is passed directly in litellm_params, as with
# every other proxy addon here, so MOONSHOT_API_KEY is not read from the environment.
# Provider docs: docs.litellm.ai/docs/providers/moonshot

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kimi",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vreq("kimi", "kimi_api_key")
    v.vns("kimi")
    v.vver("kimi")
    v.vurl("kimi")


def setup_kimi_repo(hostname, kimi_rel=None, kimi_repo_url=None):
    """Add the LiteLLM Helm repo (used as the Kimi/Moonshot proxy)."""
    helm_repo_add(hostname, kimi_rel or "litellm", kimi_repo_url or "https://berriai.github.io/litellm")


def setup_kimi(hostname, clu_name, mydomain, kimi_api_key=None, kimi_rel=None, kimi_ns=None,
                kimi_version=None, kimi_shorthn=None, kimi_model=None, kimi_api_base=None):
    """Install LiteLLM configured for Moonshot AI's Kimi models."""
    if not kimi_api_key:
        print("ERROR: kimi_api_key is mandatory. Get one at https://platform.moonshot.ai", file=sys.stderr)
        sys.exit(1)

    rel = kimi_rel or "litellm"
    ns = kimi_ns or "kimi"
    ver_arg = "--version {}".format(shlex.quote(kimi_version)) if kimi_version else ""
    fqdn = "{}.{}.{}".format(kimi_shorthn or "kimi", clu_name, mydomain)
    model = kimi_model or "moonshot/kimi-k3"
    api_base_arg = "--set proxy_config.model_list[0].litellm_params.api_base={} ".format(
        shlex.quote(kimi_api_base or "https://api.moonshot.ai/v1"))

    ssh_run(hostname,
            "helm upgrade -i kimi-proxy {}/litellm-helm --namespace {} --create-namespace "
            "--set proxy_config.model_list[0].model_name=kimi "
            "--set proxy_config.model_list[0].litellm_params.model={} "
            "--set proxy_config.model_list[0].litellm_params.api_key={} "
            "{}"
            "--set ingress.enabled=true "
            "--set ingress.host={} "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(kimi_api_key), api_base_arg,
                        shlex.quote(fqdn), ver_arg))

    print("Kimi proxy (LiteLLM) installed. Namespace: {}".format(ns))
    print("OpenAI-compatible endpoint: http://{}/v1".format(fqdn))
    print("Default model: {} (remote — Moonshot's hosted API, not self-hosted)".format(model))
    print("Usage: curl http://{}/v1/chat/completions -H 'Authorization: Bearer sk-anything' ...".format(fqdn))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("kimi", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_kimi_repo(vm_name, kimi_rel=cfg.get("kimi_rel"), kimi_repo_url=cfg.get("kimi_repo_url"))
    setup_kimi(vm_name, clu_name, clu_cfg.get("mydomain"),
               kimi_api_key=cfg.get("kimi_api_key"),
               kimi_rel=cfg.get("kimi_rel"),
               kimi_ns=cfg.get("kimi_ns"),
               kimi_version=cfg.get("kimi_version"),
               kimi_shorthn=cfg.get("kimi_shorthn"),
               kimi_model=cfg.get("kimi_model"),
               kimi_api_base=cfg.get("kimi_api_base"))


if __name__ == "__main__":
    main()
