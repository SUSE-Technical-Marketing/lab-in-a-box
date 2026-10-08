#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Open WebUI (chat frontend for Ollama/OpenAI-compatible APIs)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "open_webui" — configurable keys:
#   open_webui_version         : [OPTIONAL] Helm chart version (empty = latest)
#   open_webui_ns              : [OPTIONAL] namespace (default: open-webui)
#   open_webui_shorthn         : [OPTIONAL] hostname prefix (default: openwebui)
#   open_webui_rel             : [OPTIONAL] Helm repo alias (default: open-webui)
#   open_webui_repo_url        : [OPTIONAL] Helm repo URL (default: https://helm.openwebui.com/)
#   open_webui_ollama_url      : [OPTIONAL] external Ollama endpoint to attach — default is derived
#                                 automatically as http://ollama.<ollama_ns>.svc.cluster.local:11434
#                                 when this lab's own "ollama" addon section is present (in-cluster
#                                 Service DNS, per the ollama-helm chart's own release/service naming —
#                                 Verify with
#                                 `kubectl get svc -n <ollama_ns>` if this doesn't resolve). Set
#                                 explicitly to point at an Ollama instance outside this lab, or leave
#                                 both this and the "ollama" section unset to use Open WebUI's own
#                                 bundled Ollama subchart (chart default) instead.
#   open_webui_openai_base_urls : [OPTIONAL] space-separated list of extra OpenAI-compatible endpoints
#                                 to attach as additional model providers (e.g. this lab's own
#                                 "gemini"/"kimi"/"anthropic"/"openai" LiteLLM-proxy addons' FQDNs, so
#                                 they all show up as selectable models in one chat UI)
#   open_webui_openai_api_keys : [OPTIONAL] matching space-separated list of API keys, same order/count
#                                 as open_webui_openai_base_urls (each proxy addon in this project
#                                 accepts any bearer token, so "sk-anything" works unless the proxy was
#                                 configured with LiteLLM's own key enforcement)
#
# The chart's PVCs need a StorageClass. A bare RKE2 cluster has none by default, and the pods then
# stay Pending with no error in this addon's output. Install a storage addon (for example "longhorn")
# or another default StorageClass before this addon.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "open_webui",
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
    v.vns("open_webui")
    v.vver("open_webui")


def setup_open_webui_repo(hostname, open_webui_rel=None, open_webui_repo_url=None):
    """Add the Open WebUI Helm repo."""
    helm_repo_add(hostname, open_webui_rel or "open-webui", open_webui_repo_url or "https://helm.openwebui.com/")


def setup_open_webui(hostname, clu_name, mydomain, definition, open_webui_rel=None, open_webui_ns=None,
                      open_webui_version=None, open_webui_shorthn=None, open_webui_ollama_url=None,
                      open_webui_openai_base_urls=None, open_webui_openai_api_keys=None):
    """Install Open WebUI, wired to this lab's own Ollama/LiteLLM-proxy addons where present."""
    rel = open_webui_rel or "open-webui"
    ns = open_webui_ns or "open-webui"
    ver_arg = "--version {}".format(shlex.quote(open_webui_version)) if open_webui_version else ""
    fqdn = "{}.{}.{}".format(open_webui_shorthn or "openwebui", clu_name, mydomain)

    ollama_url = open_webui_ollama_url
    ollama_cfg = definition.get("ollama") if not ollama_url else None
    if not ollama_url and ollama_cfg is not None:
        ollama_ns = (ollama_cfg or {}).get("ollama_ns") or "ollama"
        ollama_url = "http://ollama.{}.svc.cluster.local:11434".format(ollama_ns)

    set_args = [
        "--set ingress.enabled=true",
        "--set ingress.host={}".format(shlex.quote(fqdn)),
    ]
    if ollama_url:
        set_args.append("--set ollamaUrls[0]={}".format(shlex.quote(ollama_url)))

    base_urls = (open_webui_openai_base_urls or "").split()
    api_keys = (open_webui_openai_api_keys or "").split()
    for i, url in enumerate(base_urls):
        set_args.append("--set openaiBaseApiUrls[{}]={}".format(i, shlex.quote(url)))
        key = api_keys[i] if i < len(api_keys) else "sk-anything"
        set_args.append("--set openaiApiKeys[{}]={}".format(i, shlex.quote(key)))
    if base_urls:
        set_args.append("--set enableOpenaiApi=true")

    ssh_run(hostname,
            "helm upgrade -i open-webui {}/open-webui --namespace {} --create-namespace {} "
            "{}".format(rel, ns, " ".join(set_args), ver_arg))

    print("Open WebUI installed. Namespace: {}".format(ns))
    print("Chat UI available at: http://{}".format(fqdn))
    if ollama_url:
        print("Attached Ollama endpoint: {}".format(ollama_url))
    if base_urls:
        print("Attached OpenAI-compatible endpoint(s): {}".format(", ".join(base_urls)))
    if not ollama_url and not base_urls:
        print("No ollama/OpenAI-compatible endpoint configured — using the chart's own bundled Ollama subchart default.")


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
    cfg = definition.get("open_webui", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_open_webui_repo(vm_name, open_webui_rel=cfg.get("open_webui_rel"),
                           open_webui_repo_url=cfg.get("open_webui_repo_url"))
    setup_open_webui(vm_name, clu_name, clu_cfg.get("mydomain"), definition,
                      open_webui_rel=cfg.get("open_webui_rel"),
                      open_webui_ns=cfg.get("open_webui_ns"),
                      open_webui_version=cfg.get("open_webui_version"),
                      open_webui_shorthn=cfg.get("open_webui_shorthn"),
                      open_webui_ollama_url=cfg.get("open_webui_ollama_url"),
                      open_webui_openai_base_urls=cfg.get("open_webui_openai_base_urls"),
                      open_webui_openai_api_keys=cfg.get("open_webui_openai_api_keys"))


if __name__ == "__main__":
    main()
