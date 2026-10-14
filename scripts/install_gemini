#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a Google Gemini AI proxy (LiteLLM)
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 2.0
#
# JSON section: "gemini" — configurable keys:
#   gemini_api_key      : [MANDATORY] Google Gemini API key (from https://aistudio.google.com)
#   gemini_model        : [OPTIONAL] default Gemini model (default: gemini/gemini-2.5-flash)
#                         Examples: gemini/gemini-2.5-pro, gemini/gemini-2.5-flash-lite
#   gemini_version      : [OPTIONAL] litellm-helm chart version (empty = latest)
#   gemini_chart        : [OPTIONAL] litellm-helm chart (default: oci://ghcr.io/berriai/litellm-helm)
#   gemini_ns           : [OPTIONAL] namespace (default: gemini)
#   gemini_shorthn      : [OPTIONAL] hostname prefix (default: gemini)
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
import litellm_common  # noqa: E402
from lab_creation import setup_helm  # noqa: E402


def _validate(v):
    v.vreq("gemini", "gemini_api_key")
    v.vns("gemini")
    v.vver("gemini")


def setup_gemini(hostname: str, clu_name: str, mydomain: str, cfg: dict) -> str:
    """Install the LiteLLM proxy in front of the configured model; returns the endpoint hostname."""
    ns = cfg.get("gemini_ns") or "gemini"
    fqdn = "{}.{}.{}".format(cfg.get("gemini_shorthn") or "gemini", clu_name, mydomain)
    litellm_common.setup_litellm_proxy(hostname, "gemini-proxy", ns, fqdn, "gemini", cfg.get("gemini_model") or "gemini/gemini-2.5-flash",
                                       cfg["gemini_api_key"], chart=cfg.get("gemini_chart") or "",
                                       version=cfg.get("gemini_version") or "")
    return fqdn


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
    if not cfg.get("gemini_api_key"):
        print("ERROR: gemini.gemini_api_key is mandatory.", file=sys.stderr)
        sys.exit(1)

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = setup_gemini(vm_name, clu_name, clu_cfg.get("mydomain", ""), cfg)
    ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
