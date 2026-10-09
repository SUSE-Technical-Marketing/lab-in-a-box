#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Ollama LLM server
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "ollama" — configurable keys:
#   ollama_version      : [OPTIONAL] Helm chart version (empty = latest, e.g. "0.51.0")
#   ollama_ns           : [OPTIONAL] namespace (default: ollama)
#   ollama_shorthn      : [OPTIONAL] hostname prefix (default: ollama)
#   ollama_rel          : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   ollama_repo_url     : [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   ollama_model        : [OPTIONAL] model to pre-pull on startup (default: llama3.2)
#                         Examples: llama3.2, mistral, phi3, gemma2, qwen2.5

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "ollama",
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
    v.vns("ollama")
    v.vver("ollama")
    v.vurl("ollama")


def setup_ollama_repo(hostname, ollama_rel=None, ollama_repo_url=None):
    """Add the Ollama Helm repo. Mirrors setup_ollama_repo (bash)."""
    helm_repo_add(hostname, ollama_rel or "ollama-helm", ollama_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_ollama(hostname, clu_name, mydomain, ollama_rel=None, ollama_ns=None,
                  ollama_version=None, ollama_shorthn=None, ollama_model=None):
    """Install Ollama. Mirrors setup_ollama (bash)."""
    rel = ollama_rel or "ollama-helm"
    ns = ollama_ns or "ollama"
    ver_arg = "--version {}".format(ollama_version) if ollama_version else ""
    model = ollama_model or "llama3.2"
    fqdn = "{}.{}.{}".format(ollama_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, model, fqdn, ver_arg))
    print("Ollama installed. Namespace: {}".format(ns))
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
    cfg = definition.get("ollama", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_ollama_repo(vm_name, ollama_rel=cfg.get("ollama_rel"), ollama_repo_url=cfg.get("ollama_repo_url"))
    setup_ollama(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        ollama_rel=cfg.get("ollama_rel"), ollama_ns=cfg.get("ollama_ns"),
        ollama_version=cfg.get("ollama_version"), ollama_shorthn=cfg.get("ollama_shorthn"),
        ollama_model=cfg.get("ollama_model"),
    )


if __name__ == "__main__":
    main()
