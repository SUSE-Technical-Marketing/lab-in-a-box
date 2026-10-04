#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy Apertus (Swiss AI Initiative's open LLM) via Ollama
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "apertus" — configurable keys:
#   apertus_tag         : [OPTIONAL] Ollama model tag (default: 8b-instruct-2509-q4_k_m — a ~5.1GB
#                         quantized build, the practical default for lab hardware)
#                         Other options: 8b-instruct-2509-bf16 (~16GB, full precision), 70b-instruct-
#                         2509-q4_k_m (~44GB), 70b-instruct-2509-bf16 (~141GB, needs serious hardware)
#   apertus_ns          : [OPTIONAL] Ollama namespace to pull into (default: ollama)
#   apertus_shorthn     : [OPTIONAL] hostname prefix if Ollama is not yet installed (default: ollama)
#   apertus_rel         : [OPTIONAL] Helm repo alias (default: ollama-helm)
#   apertus_repo_url    : [OPTIONAL] Helm repo URL (default: https://otwld.github.io/ollama-helm/)
#   apertus_version     : [OPTIONAL] Helm chart version (empty = latest)
#
# This script installs Ollama (if not present) and pulls the configured Apertus model — same shape as
# the "deepseek" addon.
#
# Apertus is a fully-open (weights + training data + training code) LLM from the Swiss AI Initiative
# (ETH Zurich, EPFL, and the Swiss National Supercomputing Centre), released 2025-09-02 under Apache
# 2.0. Unlike deepseek/llama/qwen/mistral, it is NOT in Ollama's own officially-curated library — the
# GGUF build pulled here is a COMMUNITY package (MichelRosselli/apertus on ollama.com), maintained by a
# third party, not the Apertus team or Ollama itself. The Ollama client must be 0.12.6 or newer for Apertus'
# architecture to load. If the pull fails with an unrecognized-architecture error, update Ollama first.

__version__ = "3d35d1a"

PLUGIN = {
    "name": "apertus",
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
    v.vns("apertus")
    v.vver("apertus")


def setup_apertus_repo(hostname, apertus_rel=None, apertus_repo_url=None):
    """Add the Ollama Helm repo (mirrors install_deepseek's own repo setup)."""
    helm_repo_add(hostname, apertus_rel or "ollama-helm", apertus_repo_url or "https://otwld.github.io/ollama-helm/")


def setup_apertus(hostname, clu_name, mydomain, apertus_rel=None, apertus_ns=None, apertus_version=None,
                   apertus_shorthn=None, apertus_tag=None):
    """Install Ollama (if needed) and pull the configured Apertus model."""
    rel = apertus_rel or "ollama-helm"
    ns = apertus_ns or "ollama"
    ver_arg = "--version {}".format(shlex.quote(apertus_version)) if apertus_version else ""
    tag = apertus_tag or "8b-instruct-2509-q4_k_m"
    model = "MichelRosselli/apertus:{}".format(tag)
    fqdn = "{}.{}.{}".format(apertus_shorthn or "ollama", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i ollama {}/ollama --namespace {} --create-namespace "
            "--set ollama.models.pull={{{}}} "
            "--set ingress.enabled=true "
            "--set ingress.hosts[0].host={} "
            "--set ingress.hosts[0].paths[0].path=/ "
            "--set ingress.hosts[0].paths[0].pathType=Prefix "
            "{}".format(rel, ns, shlex.quote(model), shlex.quote(fqdn), ver_arg))

    print("Apertus deployed via Ollama. Namespace: {}".format(ns))
    print("API available at: http://{}".format(fqdn))
    print("Model pre-pulled: {}".format(model))
    print("Community GGUF package (not Ollama's own curated library, not the Apertus team) — "
          "verify licensing/provenance before relying on it beyond a lab demo.")


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
    cfg = definition.get("apertus", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_apertus_repo(vm_name, apertus_rel=cfg.get("apertus_rel"), apertus_repo_url=cfg.get("apertus_repo_url"))
    setup_apertus(vm_name, clu_name, clu_cfg.get("mydomain"),
                  apertus_rel=cfg.get("apertus_rel"),
                  apertus_ns=cfg.get("apertus_ns"),
                  apertus_version=cfg.get("apertus_version"),
                  apertus_shorthn=cfg.get("apertus_shorthn"),
                  apertus_tag=cfg.get("apertus_tag"))


if __name__ == "__main__":
    main()
