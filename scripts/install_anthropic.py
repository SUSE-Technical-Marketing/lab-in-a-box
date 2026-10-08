#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install an Anthropic Claude AI proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "anthropic" — configurable keys:
#   anthropic_api_key   : [MANDATORY] Anthropic API key (from https://console.anthropic.com)
#   anthropic_model     : [OPTIONAL] default Claude model (default: anthropic/claude-sonnet-5)
#                         Examples: anthropic/claude-opus-5, anthropic/claude-haiku-4-5
#   anthropic_version   : [OPTIONAL] LiteLLM Helm chart version (empty = latest)
#   anthropic_ns        : [OPTIONAL] namespace (default: anthropic)
#   anthropic_shorthn   : [OPTIONAL] hostname prefix (default: anthropic)
#   anthropic_rel       : [OPTIONAL] Helm repo alias (default: litellm)
#   anthropic_repo_url  : [OPTIONAL] Helm repo URL (default: https://berriai.github.io/litellm)
#
# Same shape as the "gemini" addon: deploys LiteLLM as an OpenAI-compatible proxy, this time in front
# of Anthropic's own API. Any OpenAI SDK/client can reach Claude models through the resulting endpoint.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "anthropic",
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
    v.vreq("anthropic", "anthropic_api_key")
    v.vns("anthropic")
    v.vver("anthropic")


def setup_anthropic_repo(hostname, anthropic_rel=None, anthropic_repo_url=None):
    """Add the LiteLLM Helm repo (used as the Anthropic proxy)."""
    helm_repo_add(hostname, anthropic_rel or "litellm", anthropic_repo_url or "https://berriai.github.io/litellm")


def setup_anthropic(hostname, clu_name, mydomain, anthropic_api_key=None, anthropic_rel=None,
                     anthropic_ns=None, anthropic_version=None, anthropic_shorthn=None, anthropic_model=None):
    """Install LiteLLM configured for Anthropic Claude."""
    if not anthropic_api_key:
        print("ERROR: anthropic_api_key is mandatory. Get one at https://console.anthropic.com", file=sys.stderr)
        sys.exit(1)

    rel = anthropic_rel or "litellm"
    ns = anthropic_ns or "anthropic"
    ver_arg = "--version {}".format(shlex.quote(anthropic_version)) if anthropic_version else ""
    fqdn = "{}.{}.{}".format(anthropic_shorthn or "anthropic", clu_name, mydomain)
    model = anthropic_model or "anthropic/claude-sonnet-5"

    ssh_run(hostname,
            "helm upgrade -i anthropic-proxy {}/litellm-helm --namespace {} --create-namespace "
            "--set proxy_config.model_list[0].model_name=claude "
            "--set proxy_config.model_list[0].litellm_params.model={} "
            "--set proxy_config.model_list[0].litellm_params.api_key={} "
            "--set ingress.enabled=true "
            "--set ingress.host={} "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(anthropic_api_key), shlex.quote(fqdn), ver_arg))

    print("Anthropic proxy (LiteLLM) installed. Namespace: {}".format(ns))
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
    cfg = definition.get("anthropic", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_anthropic_repo(vm_name, anthropic_rel=cfg.get("anthropic_rel"),
                          anthropic_repo_url=cfg.get("anthropic_repo_url"))
    setup_anthropic(vm_name, clu_name, clu_cfg.get("mydomain"),
                     anthropic_api_key=cfg.get("anthropic_api_key"),
                     anthropic_rel=cfg.get("anthropic_rel"),
                     anthropic_ns=cfg.get("anthropic_ns"),
                     anthropic_version=cfg.get("anthropic_version"),
                     anthropic_shorthn=cfg.get("anthropic_shorthn"),
                     anthropic_model=cfg.get("anthropic_model"))


if __name__ == "__main__":
    main()
