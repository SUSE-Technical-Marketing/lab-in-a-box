#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a Google Gemini AI proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "gemini" — configurable keys:
#   gemini_api_key      : [MANDATORY] Google Gemini API key (from https://aistudio.google.com)
#   gemini_model        : [OPTIONAL] default Gemini model (default: gemini/gemini-1.5-flash)
#                         Examples: gemini/gemini-1.5-pro, gemini/gemini-2.0-flash
#   gemini_version      : [OPTIONAL] LiteLLM Helm chart version (empty = latest)
#   gemini_ns           : [OPTIONAL] namespace (default: gemini)
#   gemini_shorthn      : [OPTIONAL] hostname prefix (default: gemini)
#   gemini_rel          : [OPTIONAL] Helm repo alias (default: litellm)
#   gemini_repo_url     : [OPTIONAL] Helm repo URL (default: https://berriai.github.io/litellm)
#
# This script deploys LiteLLM as an OpenAI-compatible proxy for Google Gemini.
# The proxy exposes the same API as OpenAI so any OpenAI SDK can reach Gemini models.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "gemini",
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
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vreq("gemini", "gemini_api_key")
    v.vns("gemini")
    v.vver("gemini")


def setup_gemini_repo(hostname, gemini_rel=None, gemini_repo_url=None):
    """Add the LiteLLM Helm repo (used as the Gemini proxy). Mirrors setup_gemini_repo (bash)."""
    helm_repo_add(hostname, gemini_rel or "litellm", gemini_repo_url or "https://berriai.github.io/litellm")


def setup_gemini(hostname, clu_name, mydomain, gemini_api_key=None, gemini_rel=None, gemini_ns=None,
                  gemini_version=None, gemini_shorthn=None, gemini_model=None):
    """Install LiteLLM configured for Google Gemini. Mirrors setup_gemini (bash)."""
    if not gemini_api_key:
        print("ERROR: gemini_api_key is mandatory. Get one at https://aistudio.google.com", file=sys.stderr)
        sys.exit(1)

    rel = gemini_rel or "litellm"
    ns = gemini_ns or "gemini"
    ver_arg = "--version {}".format(gemini_version) if gemini_version else ""
    fqdn = "{}.{}.{}".format(gemini_shorthn or "gemini", clu_name, mydomain)
    model = gemini_model or "gemini/gemini-1.5-flash"

    ssh_run(hostname,
            "helm upgrade -i gemini-proxy {}/litellm-helm --namespace {} --create-namespace "
            "--set proxy_config.model_list[0].model_name=gemini "
            "--set proxy_config.model_list[0].litellm_params.model={} "
            "--set proxy_config.model_list[0].litellm_params.api_key={} "
            "--set ingress.enabled=true "
            "--set ingress.host={} "
            "{}".format(rel, ns, model, gemini_api_key, fqdn, ver_arg))

    print("Gemini proxy (LiteLLM) installed. Namespace: {}".format(ns))
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
    cfg = definition.get("gemini", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_gemini_repo(vm_name, gemini_rel=cfg.get("gemini_rel"), gemini_repo_url=cfg.get("gemini_repo_url"))
    setup_gemini(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        gemini_api_key=cfg.get("gemini_api_key"), gemini_rel=cfg.get("gemini_rel"), gemini_ns=cfg.get("gemini_ns"),
        gemini_version=cfg.get("gemini_version"), gemini_shorthn=cfg.get("gemini_shorthn"),
        gemini_model=cfg.get("gemini_model"),
    )


if __name__ == "__main__":
    main()
