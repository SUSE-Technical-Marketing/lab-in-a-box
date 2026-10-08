#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install an OpenAI proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "openai" — configurable keys:
#   openai_api_key   : [MANDATORY] OpenAI API key (from https://platform.openai.com)
#   openai_model     : [OPTIONAL] default model (default: openai/gpt-5)
#                       Examples: openai/gpt-5-mini, openai/gpt-5-nano, openai/o5
#   openai_version   : [OPTIONAL] LiteLLM Helm chart version (empty = latest)
#   openai_ns        : [OPTIONAL] namespace (default: openai)
#   openai_shorthn   : [OPTIONAL] hostname prefix (default: openai)
#   openai_rel       : [OPTIONAL] Helm repo alias (default: litellm)
#   openai_repo_url  : [OPTIONAL] Helm repo URL (default: https://berriai.github.io/litellm)
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
    v.vreq("openai", "openai_api_key")
    v.vns("openai")
    v.vver("openai")


def setup_openai_repo(hostname, openai_rel=None, openai_repo_url=None):
    """Add the LiteLLM Helm repo (used as the OpenAI proxy)."""
    helm_repo_add(hostname, openai_rel or "litellm", openai_repo_url or "https://berriai.github.io/litellm")


def setup_openai(hostname, clu_name, mydomain, openai_api_key=None, openai_rel=None, openai_ns=None,
                  openai_version=None, openai_shorthn=None, openai_model=None):
    """Install LiteLLM configured for OpenAI."""
    if not openai_api_key:
        print("ERROR: openai_api_key is mandatory. Get one at https://platform.openai.com", file=sys.stderr)
        sys.exit(1)

    rel = openai_rel or "litellm"
    ns = openai_ns or "openai"
    ver_arg = "--version {}".format(shlex.quote(openai_version)) if openai_version else ""
    fqdn = "{}.{}.{}".format(openai_shorthn or "openai", clu_name, mydomain)
    model = openai_model or "openai/gpt-5"

    ssh_run(hostname,
            "helm upgrade -i openai-proxy {}/litellm-helm --namespace {} --create-namespace "
            "--set proxy_config.model_list[0].model_name=gpt "
            "--set proxy_config.model_list[0].litellm_params.model={} "
            "--set proxy_config.model_list[0].litellm_params.api_key={} "
            "--set ingress.enabled=true "
            "--set ingress.host={} "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(openai_api_key), shlex.quote(fqdn), ver_arg))

    print("OpenAI proxy (LiteLLM) installed. Namespace: {}".format(ns))
    print("OpenAI-compatible endpoint: http://{}/v1".format(fqdn))
    print("Default model: {}".format(model))
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
    cfg = definition.get("openai", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_openai_repo(vm_name, openai_rel=cfg.get("openai_rel"), openai_repo_url=cfg.get("openai_repo_url"))
    setup_openai(vm_name, clu_name, clu_cfg.get("mydomain"),
                 openai_api_key=cfg.get("openai_api_key"),
                 openai_rel=cfg.get("openai_rel"),
                 openai_ns=cfg.get("openai_ns"),
                 openai_version=cfg.get("openai_version"),
                 openai_shorthn=cfg.get("openai_shorthn"),
                 openai_model=cfg.get("openai_model"))


if __name__ == "__main__":
    main()
