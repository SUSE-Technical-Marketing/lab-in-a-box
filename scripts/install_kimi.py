#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a Moonshot AI Kimi proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 2.0
#
# JSON section: "kimi" — configurable keys:
#   kimi_api_key    : [MANDATORY] Moonshot AI API key (from https://platform.moonshot.ai)
#   kimi_model      : [OPTIONAL] default model (default: moonshot/kimi-k3)
#                      Examples: moonshot/kimi-k2.6 (cheaper), moonshot/kimi-k2.7-code (coding-focused),
#                      moonshot/moonshot-v1-8k
#   kimi_api_base   : [OPTIONAL] override endpoint, Moonshot's global endpoint by default; use
#                      https://api.moonshot.cn/v1 for the China region (default: https://api.moonshot.ai/v1)
#   kimi_version    : [OPTIONAL] litellm-helm chart version (empty = latest)
#   kimi_chart      : [OPTIONAL] litellm-helm chart (default: oci://ghcr.io/berriai/litellm-helm)
#   kimi_ns         : [OPTIONAL] namespace (default: kimi)
#   kimi_shorthn    : [OPTIONAL] hostname prefix (default: kimi)
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
# The moonshot/ LiteLLM provider prefix is used, with the key from kimi_api_key.
# Provider docs: docs.litellm.ai/docs/providers/moonshot

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "kimi",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
import litellm_common  # noqa: E402
from lab_creation import setup_helm  # noqa: E402


def _validate(v):
    v.vreq("kimi", "kimi_api_key")
    v.vns("kimi")
    v.vver("kimi")


def setup_kimi(hostname: str, clu_name: str, mydomain: str, cfg: dict) -> str:
    """Install the LiteLLM proxy in front of the configured model; returns the endpoint hostname."""
    ns = cfg.get("kimi_ns") or "kimi"
    fqdn = "{}.{}.{}".format(cfg.get("kimi_shorthn") or "kimi", clu_name, mydomain)
    litellm_common.setup_litellm_proxy(hostname, "kimi-proxy", ns, fqdn, "kimi", cfg.get("kimi_model") or "moonshot/kimi-k3",
                                       cfg["kimi_api_key"], chart=cfg.get("kimi_chart") or "",
                                       version=cfg.get("kimi_version") or "",
                                       api_base=cfg.get("kimi_api_base") or "https://api.moonshot.ai/v1")
    return fqdn


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
    if not cfg.get("kimi_api_key"):
        print("ERROR: kimi.kimi_api_key is mandatory.", file=sys.stderr)
        sys.exit(1)

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = setup_kimi(vm_name, clu_name, clu_cfg.get("mydomain", ""), cfg)
    ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
