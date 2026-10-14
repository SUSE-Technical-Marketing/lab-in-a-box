#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install an OpenAI proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 2.0
#
# JSON section: "openai" — configurable keys:
#   openai_api_key   : [MANDATORY] OpenAI API key (from https://platform.openai.com)
#   openai_model     : [OPTIONAL] default model (default: openai/gpt-5)
#                       Examples: openai/gpt-5-mini, openai/gpt-5-nano, openai/o5
#   openai_version   : [OPTIONAL] litellm-helm chart version (empty = latest)
#   openai_chart     : [OPTIONAL] litellm-helm chart (default: oci://ghcr.io/berriai/litellm-helm)
#   openai_ns        : [OPTIONAL] namespace (default: openai)
#   openai_shorthn   : [OPTIONAL] hostname prefix (default: openai)
#
# Same shape as the "gemini"/"anthropic" addons. Useful as a known-good baseline endpoint to compare
# against the lab's own local models (ollama/deepseek/apertus/...) — everything speaks the same
# OpenAI-compatible API either way, so a client (or open_webui) can point at any of them interchangeably.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "openai",
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
    v.vreq("openai", "openai_api_key")
    v.vns("openai")
    v.vver("openai")


def setup_openai(hostname: str, clu_name: str, mydomain: str, cfg: dict) -> str:
    """Install the LiteLLM proxy in front of the configured model; returns the endpoint hostname."""
    ns = cfg.get("openai_ns") or "openai"
    fqdn = "{}.{}.{}".format(cfg.get("openai_shorthn") or "openai", clu_name, mydomain)
    litellm_common.setup_litellm_proxy(hostname, "openai-proxy", ns, fqdn, "gpt", cfg.get("openai_model") or "openai/gpt-5",
                                       cfg["openai_api_key"], chart=cfg.get("openai_chart") or "",
                                       version=cfg.get("openai_version") or "")
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
    cfg = definition.get("openai", {}) or {}
    if not cfg.get("openai_api_key"):
        print("ERROR: openai.openai_api_key is mandatory.", file=sys.stderr)
        sys.exit(1)

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = setup_openai(vm_name, clu_name, clu_cfg.get("mydomain", ""), cfg)
    ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
