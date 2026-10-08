#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy Qwen2.5 models via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "qwen" — configurable keys:
#   qwen_model      : [OPTIONAL] Qwen model tag (default: qwen2.5:7b)
#                     Examples: qwen2.5:0.5b, qwen2.5:14b, qwen2.5:32b, qwen2.5:72b,
#                     qwen2.5-coder:7b (coding-focused variant)
#   qwen_ns         : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   qwen_shorthn    : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   qwen_rel        : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   qwen_repo_url   : [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   qwen_version    : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured Qwen2.5 model — same shape
# as the "deepseek"/"apertus" addons. Qwen2.5 (Alibaba) ships base and instruction-tuned sizes from
# 0.5B to 72B, confirmed on Ollama's own curated library (ollama.com/library/qwen2.5), so — unlike
# apertus — this is an OFFICIAL Ollama-library model, no community-GGUF caveat needed.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "qwen",
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
    v.vns("qwen")
    v.vver("qwen")


def setup_qwen_repo(hostname, qwen_rel=None, qwen_repo_url=None):
    """Add the Ollama Helm repo."""
    helm_repo_add(hostname, qwen_rel or "ollama-helm", qwen_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_qwen(hostname, clu_name, mydomain, qwen_rel=None, qwen_ns=None, qwen_version=None,
               qwen_shorthn=None, qwen_model=None):
    """Install Ollama (if needed) and pull the configured Qwen2.5 model."""
    rel = qwen_rel or "ollama-helm"
    ns = qwen_ns or "ollama"
    ver_arg = "--version {}".format(shlex.quote(qwen_version)) if qwen_version else ""
    model = qwen_model or "qwen2.5:7b"
    fqdn = "{}.{}.{}".format(qwen_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(fqdn), ver_arg))

    print("Qwen2.5 deployed via Ollama. Namespace: {}".format(ns))
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
    cfg = definition.get("qwen", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_qwen_repo(vm_name, qwen_rel=cfg.get("qwen_rel"), qwen_repo_url=cfg.get("qwen_repo_url"))
    setup_qwen(vm_name, clu_name, clu_cfg.get("mydomain"),
               qwen_rel=cfg.get("qwen_rel"), qwen_ns=cfg.get("qwen_ns"),
               qwen_version=cfg.get("qwen_version"), qwen_shorthn=cfg.get("qwen_shorthn"),
               qwen_model=cfg.get("qwen_model"))


if __name__ == "__main__":
    main()
