#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy StarCoder2 models via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "starcoder2" — configurable keys:
#   starcoder2_model   : [OPTIONAL] StarCoder2 model tag (default: starcoder2 — the 3B build)
#                        Examples: starcoder2:7b, starcoder2:15b
#   starcoder2_ns      : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   starcoder2_shorthn : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   starcoder2_rel     : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   starcoder2_repo_url: [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   starcoder2_version : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured StarCoder2 model — same
# shape as the "deepseek"/"apertus"/"qwen"/"mistral"/"codellama" addons. A second, distinct
# coding-focused model choice (BigCode/ServiceNow/Hugging Face/NVIDIA collaboration, transparently
# trained), official Ollama-library model (ollama.com/library/starcoder2), 3B/7B/15B sizes.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "starcoder2",
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
    v.vns("starcoder2")
    v.vver("starcoder2")


def setup_starcoder2_repo(hostname, starcoder2_rel=None, starcoder2_repo_url=None):
    """Add the Ollama Helm repo."""
    helm_repo_add(hostname, starcoder2_rel or "ollama-helm", starcoder2_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_starcoder2(hostname, clu_name, mydomain, starcoder2_rel=None, starcoder2_ns=None,
                      starcoder2_version=None, starcoder2_shorthn=None, starcoder2_model=None):
    """Install Ollama (if needed) and pull the configured StarCoder2 model."""
    rel = starcoder2_rel or "ollama-helm"
    ns = starcoder2_ns or "ollama"
    ver_arg = "--version {}".format(shlex.quote(starcoder2_version)) if starcoder2_version else ""
    model = starcoder2_model or "starcoder2"
    fqdn = "{}.{}.{}".format(starcoder2_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(fqdn), ver_arg))

    print("StarCoder2 deployed via Ollama. Namespace: {}".format(ns))
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
    cfg = definition.get("starcoder2", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_starcoder2_repo(vm_name, starcoder2_rel=cfg.get("starcoder2_rel"),
                           starcoder2_repo_url=cfg.get("starcoder2_repo_url"))
    setup_starcoder2(vm_name, clu_name, clu_cfg.get("mydomain"),
                      starcoder2_rel=cfg.get("starcoder2_rel"), starcoder2_ns=cfg.get("starcoder2_ns"),
                      starcoder2_version=cfg.get("starcoder2_version"),
                      starcoder2_shorthn=cfg.get("starcoder2_shorthn"),
                      starcoder2_model=cfg.get("starcoder2_model"))


if __name__ == "__main__":
    main()
