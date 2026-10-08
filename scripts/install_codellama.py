#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy Code Llama models via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "codellama" — configurable keys:
#   codellama_model   : [OPTIONAL] Code Llama model tag (default: codellama — the 7B instruct build)
#                       Examples: codellama:13b, codellama:34b, codellama:70b, codellama:7b-python
#   codellama_ns      : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   codellama_shorthn : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   codellama_rel     : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   codellama_repo_url: [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   codellama_version : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured Code Llama model — same
# shape as the "deepseek"/"apertus"/"qwen"/"mistral" addons. Meta's code-focused Llama 2 derivative,
# official Ollama-library model (ollama.com/library/codellama), broad language coverage (Python,
# C++, Java, PHP, TypeScript, C#, Bash, and more) — the coding-model pick for this project's own
# dev/CI-heavy audience, per the earlier model-catalog discussion.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "codellama",
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
    v.vns("codellama")
    v.vver("codellama")


def setup_codellama_repo(hostname, codellama_rel=None, codellama_repo_url=None):
    """Add the Ollama Helm repo."""
    helm_repo_add(hostname, codellama_rel or "ollama-helm", codellama_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_codellama(hostname, clu_name, mydomain, codellama_rel=None, codellama_ns=None,
                     codellama_version=None, codellama_shorthn=None, codellama_model=None):
    """Install Ollama (if needed) and pull the configured Code Llama model."""
    rel = codellama_rel or "ollama-helm"
    ns = codellama_ns or "ollama"
    ver_arg = "--version {}".format(shlex.quote(codellama_version)) if codellama_version else ""
    model = codellama_model or "codellama"
    fqdn = "{}.{}.{}".format(codellama_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(fqdn), ver_arg))

    print("Code Llama deployed via Ollama. Namespace: {}".format(ns))
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
    cfg = definition.get("codellama", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_codellama_repo(vm_name, codellama_rel=cfg.get("codellama_rel"),
                          codellama_repo_url=cfg.get("codellama_repo_url"))
    setup_codellama(vm_name, clu_name, clu_cfg.get("mydomain"),
                     codellama_rel=cfg.get("codellama_rel"), codellama_ns=cfg.get("codellama_ns"),
                     codellama_version=cfg.get("codellama_version"),
                     codellama_shorthn=cfg.get("codellama_shorthn"),
                     codellama_model=cfg.get("codellama_model"))


if __name__ == "__main__":
    main()
