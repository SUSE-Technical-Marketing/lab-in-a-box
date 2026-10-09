#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy DeepSeek models via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "deepseek" — configurable keys:
#   deepseek_model      : [OPTIONAL] DeepSeek model tag (default: deepseek-r1:7b)
#                         Examples: deepseek-r1:7b, deepseek-r1:14b, deepseek-coder-v2
#   deepseek_ns         : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   deepseek_shorthn    : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   deepseek_rel        : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   deepseek_repo_url   : [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   deepseek_version    : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured DeepSeek model.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "deepseek",
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
    v.vns("deepseek")
    v.vver("deepseek")


def setup_deepseek_repo(hostname, deepseek_rel=None, deepseek_repo_url=None):
    """Add the Ollama Helm repo (DeepSeek runs via Ollama). Mirrors setup_deepseek_repo (bash)."""
    helm_repo_add(hostname, deepseek_rel or "ollama-helm", deepseek_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_deepseek(hostname, clu_name, mydomain, deepseek_rel=None, deepseek_ns=None,
                    deepseek_version=None, deepseek_shorthn=None, deepseek_model=None):
    """Deploy a DeepSeek model via Ollama. Mirrors setup_deepseek (bash)."""
    rel = deepseek_rel or "ollama-helm"
    ns = deepseek_ns or "ollama"
    model = deepseek_model or "deepseek-r1:7b"
    ver_arg = "--version {}".format(deepseek_version) if deepseek_version else ""
    fqdn = "{}.{}.{}".format(deepseek_shorthn or "ollama", clu_name, mydomain)

    print("# Ensuring Ollama is installed in namespace {}".format(ns))
    ssh_run(hostname,
            "helm upgrade -i ollama-deepseek {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, model, fqdn, ver_arg))

    print("DeepSeek model '{}' scheduled for pull via Ollama.".format(model))
    print("API available at: http://{}".format(fqdn))
    print("Note: first-time model pull may take several minutes depending on model size.")


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
    cfg = definition.get("deepseek", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_deepseek_repo(vm_name, deepseek_rel=cfg.get("deepseek_rel"), deepseek_repo_url=cfg.get("deepseek_repo_url"))
    setup_deepseek(
        vm_name, clu_name, clu_cfg.get("mydomain", ""),
        deepseek_rel=cfg.get("deepseek_rel"), deepseek_ns=cfg.get("deepseek_ns"),
        deepseek_version=cfg.get("deepseek_version"), deepseek_shorthn=cfg.get("deepseek_shorthn"),
        deepseek_model=cfg.get("deepseek_model"),
    )


if __name__ == "__main__":
    main()
