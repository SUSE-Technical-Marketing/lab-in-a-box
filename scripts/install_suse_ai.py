#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs SUSE AI, SUSE's Ollama, Open WebUI and Milvus stack.
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.2
#
# JSON section: "suse_ai"
#   suse_ai_registry_user      : [MANDATORY] SUSE Application Collection user. This is an Application Collection entitlement, separate
#                                from the SCC registration code.
#   suse_ai_registry_password  : [MANDATORY] Application Collection access token
#   suse_ai_registry_account   : [OPTIONAL] name of an encrypted credential file of kind "appcollection" under
#                                /etc/lab_creation/credentials/ for the registry user and token. It is auto-discovered when exactly
#                                one such file exists and this is unset. The plaintext fields remain valid.
#   suse_ai_regcode            : [OPTIONAL] SCC registration code of a SUSE AI subscription, for registry.suse.com. When set, a second
#                                pull secret (suse-ai-registry) is created and Helm logs in to registry.suse.com as "regcode".
#   suse_ai_scc_account        : [OPTIONAL] name of an encrypted credential file of kind "scc" whose scc_regcode is used as
#                                suse_ai_regcode. Never auto-discovered: an SCC file usually holds a SLES code, not a SUSE AI one.
#   suse_ai_registry           : OCI registry host (default: dp.apps.rancher.io)
#   suse_ai_ns                 : namespace, SUSE's documented default (default: suse-private-ai)
#   suse_ai_components         : space-separated components (default: "ollama open-webui"). Add "milvus" for local RAG vector search.
#                                They are installed in the order milvus, ollama, open-webui. OpenSearch is not wired up here.
#   suse_ai_ollama_version     : chart version pin for ollama (empty = latest)
#   suse_ai_open_webui_version : chart version pin for open-webui
#   suse_ai_milvus_version     : chart version pin for milvus
#   suse_ai_ollama_models      : [OPTIONAL] space-separated models Ollama pulls at start, e.g. "gemma:2b"
#   suse_ai_gpu                : [OPTIONAL] "true" runs Ollama on one NVIDIA GPU; needs the gpu_operator addon (default: false)
#   suse_ai_tls_source         : "suse-private-ai" (self-signed, default), "letsEncrypt" (needs public DNS and cert-manager HTTP-01), or
#                                "secret" (a certificate you provide, without cert-manager)
#   suse_ai_shorthn            : hostname prefix for the Open WebUI ingress (default: ai)
#   suse_ai_extra_set_<component> : space-separated key=value pairs, added as --set flags to that component's helm install, for settings
#                                the keys above do not cover (for example a storage class).
#
# What it does: creates the namespace and the pull secrets, logs Helm in to the registries, installs cert-manager from the Application
# Collection when its CRDs are missing (unless suse_ai_tls_source is "secret"), then installs each component from
# oci://<registry>/charts/<component> with the pull secrets set. Milvus runs standalone (one pod, embedded etcd and MinIO, no Kafka). Open
# WebUI uses the separately installed Ollama when "ollama" is a component, and Milvus as its vector database when "milvus" is.
#
# Prerequisites this addon does not install: an ingress controller and a default StorageClass (K3s ships local-path; on RKE2 add longhorn).
#
# This addon deploys SUSE's rebuild of the Ollama, Open WebUI and Milvus components. The ollama, open_webui and milvus addons install
# the community builds, so do not run both against the same cluster and namespace.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "suse_ai",
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
from appco_common import SECRET_NAME as _APPCO_SECRET, helm_login, pull_secret, pull_secret_pairs, set_flags  # noqa: E402
from lab_creation import setup_helm, ssh_run  # noqa: E402

_DEFAULT_COMPONENTS = ["ollama", "open-webui"]
_ORDER = ["milvus", "ollama", "open-webui"]
_SUSE_SECRET = "suse-ai-registry"
_SUSE_REGISTRY = "registry.suse.com"
_CERT_MANAGER_NS = "cert-manager"


def _validate(v):
    v.vreq_or_credential("suse_ai", "suse_ai_registry_user", "appcollection",
                          account_field="suse_ai_registry_account")
    v.vreq_or_credential("suse_ai", "suse_ai_registry_password", "appcollection",
                          account_field="suse_ai_registry_account")
    v.vns("suse_ai")
    v.vbool("suse_ai", "suse_ai_gpu")


def extra_set_pairs(extra_set: str) -> list:
    """(key, value) pairs from a space-separated "key=value ..." string; entries without "=" are ignored."""
    return [tuple(p.split("=", 1)) for p in (extra_set or "").split() if "=" in p]


def component_values(component: str, cfg: dict, components: list, ns: str, fqdn: str) -> list:
    """The --set pairs this addon derives for `component`, before the pull secrets and suse_ai_extra_set_<component>."""
    if component == "milvus":
        return [("cluster.enabled", "false"), ("etcd.replicaCount", "1"), ("minio.mode", "standalone"),
                ("kafka.enabled", "false")]
    if component == "ollama":
        pairs = [("persistentVolume.enabled", "true")]
        pairs += [("ollama.models.pull[{}]".format(i), m) for i, m in enumerate((cfg.get("suse_ai_ollama_models") or "").split())]
        if str(cfg.get("suse_ai_gpu") or "false") == "true":
            pairs += [("ollama.gpu.enabled", "true"), ("ollama.gpu.type", "nvidia"), ("ollama.gpu.number", "1")]
        return pairs
    if component == "open-webui":
        pairs = [("global.tls.source", cfg.get("suse_ai_tls_source") or "suse-private-ai"),
                 ("ingress.enabled", "true"), ("ingress.host", fqdn), ("ingress.tls", "true")]
        if "ollama" in components:
            pairs += [("ollama.enabled", "false"), ("pipelines.enabled", "false"),
                      ("ollamaUrls[0]", "http://ollama.{}.svc.cluster.local:11434".format(ns))]
        if "milvus" in components:
            pairs += [("extraEnvVars[0].name", "VECTOR_DB"), ("extraEnvVars[0].value", "milvus"),
                      ("extraEnvVars[1].name", "MILVUS_URI"),
                      ("extraEnvVars[1].value", "http://milvus.{}.svc.cluster.local:19530".format(ns))]
        return pairs
    return []


def ordered(components: list) -> list:
    """`components` with the known ones in dependency order (milvus, ollama, open-webui) and any others after them."""
    return [c for c in _ORDER if c in components] + [c for c in components if c not in _ORDER]


def ensure_cert_manager(hostname: str, registry: str, secret_args: tuple) -> None:
    """Install cert-manager from the Application Collection when its CRDs are missing; `secret_args` are pull_secret()'s arguments
    without the namespace."""
    if ssh_run(hostname, "kubectl get crd certificates.cert-manager.io >/dev/null 2>&1", check=False).returncode == 0:
        print("cert-manager already installed; skipping.")
        return
    print("cert-manager not found on the cluster; installing it from the SUSE Application Collection...")
    pull_secret(hostname, *secret_args, _CERT_MANAGER_NS)
    ssh_run(hostname, "helm upgrade --install cert-manager oci://{}/charts/cert-manager --namespace {} --create-namespace --wait{}".format(
        registry, _CERT_MANAGER_NS, set_flags([("crds.enabled", "true")] + pull_secret_pairs([_APPCO_SECRET], named=True))))
    print("cert-manager installed from the SUSE Application Collection.")


def setup_suse_ai_component(hostname: str, registry: str, component: str, ns: str, pairs: list, version: str = "") -> None:
    """helm upgrade --install one SUSE AI component straight from its OCI chart reference."""
    ver_arg = " --version {}".format(shlex.quote(version)) if version else ""
    ssh_run(hostname, "helm upgrade --install {} oci://{}/charts/{} --namespace {} --create-namespace{}{}".format(
        shlex.quote(component), registry, shlex.quote(component), shlex.quote(ns), ver_arg, set_flags(pairs)))
    print("SUSE AI component '{}' installed. Namespace: {}".format(component, ns))


def suse_regcode(cfg: dict) -> str:
    """suse_ai_regcode, or scc_regcode from the "scc" credential file named by suse_ai_scc_account; "" when neither is set."""
    if cfg.get("suse_ai_scc_account"):
        return primary.load_service_credential(cfg["suse_ai_scc_account"]).get("scc_regcode") or ""
    return cfg.get("suse_ai_regcode") or ""


def setup_suse_ai(hostname: str, clu_name: str, mydomain: str, cfg: dict) -> str:
    """Install the configured SUSE AI components; returns the Open WebUI hostname, "" when open-webui is not installed."""
    creds = ac.resolve_credential(cfg, "appcollection", {
        "appcollection_user": "suse_ai_registry_user",
        "appcollection_password": "suse_ai_registry_password",
    }, account_key="suse_ai_registry_account")
    registry_user = creds["appcollection_user"]
    registry_password = creds["appcollection_password"]
    if not registry_user or not registry_password:
        print("ERROR: suse_ai_registry_user and suse_ai_registry_password are mandatory "
              "(SUSE Application Collection entitlement — see https://apps.rancher.io).", file=sys.stderr)
        sys.exit(1)

    registry = cfg.get("suse_ai_registry") or "dp.apps.rancher.io"
    ns = cfg.get("suse_ai_ns") or "suse-private-ai"
    components = ordered((cfg.get("suse_ai_components") or " ".join(_DEFAULT_COMPONENTS)).split())
    tls_source = cfg.get("suse_ai_tls_source") or "suse-private-ai"
    fqdn = "{}.{}.{}".format(cfg.get("suse_ai_shorthn") or "ai", clu_name, mydomain)
    regcode = suse_regcode(cfg)

    appco = (_APPCO_SECRET, registry, registry_user, registry_password)
    pull_secret(hostname, *appco, ns)
    helm_login(hostname, "{}/charts".format(registry), registry_user, registry_password)
    secrets = [_APPCO_SECRET]
    if regcode:
        pull_secret(hostname, _SUSE_SECRET, _SUSE_REGISTRY, "regcode", regcode, ns)
        helm_login(hostname, _SUSE_REGISTRY, "regcode", regcode)
        secrets.append(_SUSE_SECRET)
    if tls_source != "secret" and "open-webui" in components:
        ensure_cert_manager(hostname, registry, appco)

    version_keys = {"ollama": "suse_ai_ollama_version", "open-webui": "suse_ai_open_webui_version",
                    "milvus": "suse_ai_milvus_version"}
    for component in components:
        pairs = (pull_secret_pairs(secrets) + component_values(component, cfg, components, ns, fqdn)
                 + extra_set_pairs(cfg.get("suse_ai_extra_set_{}".format(component))))
        setup_suse_ai_component(hostname, registry, component, ns, pairs,
                                 version=cfg.get(version_keys.get(component, "")) or "")

    print("SUSE AI installed. Namespace: {}. Components: {}".format(ns, ", ".join(components)))
    if "open-webui" not in components:
        return ""
    print("Open WebUI: https://{}".format(fqdn))
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
    cfg = definition.get("suse_ai", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    fqdn = setup_suse_ai(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)
    if fqdn:
        ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
