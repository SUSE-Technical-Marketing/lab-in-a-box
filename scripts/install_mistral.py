#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy Mistral models via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "mistral" — configurable keys:
#   mistral_model   : [OPTIONAL] Mistral model tag (default: mistral — the 7B v0.3 model, Apache-2.0
#                     licensed, permissive unlike Llama's own community license)
#                     Examples: mistral:7b-instruct, mixtral (the larger MoE variant)
#   mistral_ns      : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   mistral_shorthn : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   mistral_rel     : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   mistral_repo_url: [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   mistral_version : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured Mistral model — same shape
# as the "deepseek"/"apertus"/"qwen" addons. Official Ollama-library model (ollama.com/library/mistral),
# no community-GGUF caveat needed.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "mistral",
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
    v.vns("mistral")
    v.vver("mistral")


def setup_mistral_repo(hostname, mistral_rel=None, mistral_repo_url=None):
    """Add the Ollama Helm repo."""
    helm_repo_add(hostname, mistral_rel or "ollama-helm", mistral_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_mistral(hostname, clu_name, mydomain, mistral_rel=None, mistral_ns=None, mistral_version=None,
                   mistral_shorthn=None, mistral_model=None):
    """Install Ollama (if needed) and pull the configured Mistral model."""
    rel = mistral_rel or "ollama-helm"
    ns = mistral_ns or "ollama"
    ver_arg = "--version {}".format(shlex.quote(mistral_version)) if mistral_version else ""
    model = mistral_model or "mistral"
    fqdn = "{}.{}.{}".format(mistral_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(fqdn), ver_arg))

    print("Mistral deployed via Ollama. Namespace: {}".format(ns))
    print("API available at: http://{}".format(fqdn))
    print("Model pre-pulled: {}".format(model))


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
    cfg = definition.get("mistral", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_mistral_repo(vm_name, mistral_rel=cfg.get("mistral_rel"), mistral_repo_url=cfg.get("mistral_repo_url"))
    setup_mistral(vm_name, clu_name, clu_cfg.get("mydomain"),
                  mistral_rel=cfg.get("mistral_rel"), mistral_ns=cfg.get("mistral_ns"),
                  mistral_version=cfg.get("mistral_version"), mistral_shorthn=cfg.get("mistral_shorthn"),
                  mistral_model=cfg.get("mistral_model"))


if __name__ == "__main__":
    main()
