#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs SUSE AI, SUSE's Ollama, Open WebUI and Milvus stack.
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "suse_ai"
#   suse_ai_registry_user      : [MANDATORY] SUSE Application Collection registry user (the SCC login e-mail). This is an Application
#                                Collection entitlement token, separate from the SCC registration code.
#   suse_ai_registry_password  : [MANDATORY] Application Collection registry password or token
#   suse_ai_registry_account   : [OPTIONAL] name of an encrypted credential file of kind "appcollection" under
#                                /etc/lab_creation/credentials/ for the registry user and password. It is auto-discovered when exactly
#                                one such file exists and this is unset. The plaintext fields remain valid.
#   suse_ai_registry           : OCI registry host (default dp.apps.rancher.io)
#   suse_ai_ns                 : namespace (default suse-private-ai, SUSE's documented default)
#   suse_ai_components         : space-separated components (default "ollama open-webui"). Add "milvus" for local RAG vector search.
#                                OpenSearch is not wired up here.
#   suse_ai_ollama_version     : chart version pin for ollama (empty = latest)
#   suse_ai_open_webui_version : chart version pin for open-webui
#   suse_ai_milvus_version     : chart version pin for milvus
#   suse_ai_tls_source         : "suse-private-ai" (self-signed, default), "letsEncrypt" (needs public DNS and cert-manager HTTP-01), or
#                                "secret" (a certificate you provide, without cert-manager)
#   suse_ai_shorthn            : hostname prefix for the Open WebUI ingress (default ai)
#   suse_ai_extra_set_<component> : space-separated key=value pairs, added as --set flags to that component's helm install, for settings
#                                the keys above do not cover.
#
# Prerequisites this addon does not install: cert-manager (unless suse_ai_tls_source is "secret"), an ingress controller, and a StorageClass.
#
# The addon creates the namespace and the application-collection registry Secret. The chart references are
# oci://dp.apps.rancher.io/charts/<component>. The Milvus reference follows SUSE's published example. The ollama reference follows the same
# pattern but is not confirmed, so check it with helm show chart before relying on it.
#
# SUSE's Open WebUI build can use different ingress and TLS keys from the upstream chart that the open_webui addon uses. Use
# suse_ai_extra_set_<component> for those. A failed registry login is reported with an actionable error.
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
from lab_creation import setup_helm, ssh_run  # noqa: E402

_DEFAULT_COMPONENTS = ["ollama", "open-webui"]


def _validate(v):
    v.vreq_or_credential("suse_ai", "suse_ai_registry_user", "appcollection",
                          account_field="suse_ai_registry_account")
    v.vreq_or_credential("suse_ai", "suse_ai_registry_password", "appcollection",
                          account_field="suse_ai_registry_account")
    v.vns("suse_ai")


def setup_suse_ai_registry(hostname, registry, user, password, ns):
    """Authenticate to SUSE's Application Collection OCI registry, for both image pulls (a k8s
    docker-registry Secret in the target namespace) and Helm's own OCI chart pulls."""
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(shlex.quote(ns)), check=False)

    secret_cmd = (
        "kubectl create secret docker-registry application-collection "
        "--docker-server={} --docker-username={} --docker-password=$SUSE_AI_REGISTRY_PASSWORD "
        "--namespace {} --dry-run=client -o yaml | kubectl apply -f -"
    ).format(shlex.quote(registry), shlex.quote(user), shlex.quote(ns))
    ssh_run(hostname, "SUSE_AI_REGISTRY_PASSWORD={} bash -c {}".format(
        shlex.quote(password), shlex.quote(secret_cmd)))

    login = ssh_run(hostname, "helm registry login {} --username {} --password-stdin".format(
        shlex.quote(registry), shlex.quote(user)), input_text=password, check=False)
    if login.returncode != 0:
        print("ERROR: helm registry login to '{}' failed (see helm's own error above) — "
              "suse_ai_registry_user/suse_ai_registry_password must be a real SUSE Application "
              "Collection entitlement, not your SCC registration code or email. The SCC login in "
              "SUSE_email/SUSE_regcode does not authenticate to dp.apps.rancher.io. Get Application "
              "Collection access at https://apps.rancher.io.".format(registry), file=sys.stderr)
        sys.exit(1)


def setup_suse_ai_component(hostname, registry, component, ns, version=None, extra_set=None, extra_args=""):
    """helm upgrade -i one SUSE AI component straight from its OCI chart reference."""
    ver_arg = "--version {}".format(shlex.quote(version)) if version else ""
    set_args = ""
    for pair in (extra_set or "").split():
        if "=" in pair:
            key, _, val = pair.partition("=")
            set_args += " --set {}={}".format(shlex.quote(key), shlex.quote(val))

    ssh_run(hostname,
            "helm upgrade -i {} oci://{}/charts/{} --namespace {} --create-namespace {}{} {}".format(
                shlex.quote(component), registry, shlex.quote(component), shlex.quote(ns),
                ver_arg, set_args, extra_args))
    print("SUSE AI component '{}' installed. Namespace: {}".format(component, ns))


def setup_suse_ai(hostname, clu_name, mydomain, cfg):
    """Install the configured SUSE AI components."""
    # suse_ai_registry_user/suse_ai_registry_password may alternatively come from an
    # encrypted "appcollection"-kind credential file (see README's Credentials
    # section) — resolved here; falls straight through to the plaintext fields
    # below, unchanged, whenever no matching credential file is used.
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
    components = (cfg.get("suse_ai_components") or " ".join(_DEFAULT_COMPONENTS)).split()
    tls_source = cfg.get("suse_ai_tls_source") or "suse-private-ai"
    shorthn = cfg.get("suse_ai_shorthn") or "ai"
    fqdn = "{}.{}.{}".format(shorthn, clu_name, mydomain)

    setup_suse_ai_registry(hostname, registry, registry_user, registry_password, ns)

    version_keys = {
        "ollama": "suse_ai_ollama_version",
        "open-webui": "suse_ai_open_webui_version",
        "milvus": "suse_ai_milvus_version",
    }
    for component in components:
        extra_set = cfg.get("suse_ai_extra_set_{}".format(component))
        extra_args = ""
        if component == "open-webui":
            extra_args = "--set global.tls.source={}".format(shlex.quote(tls_source))
        setup_suse_ai_component(hostname, registry, component, ns,
                                 version=cfg.get(version_keys.get(component, "")),
                                 extra_set=extra_set, extra_args=extra_args)

    print("SUSE AI installed. Namespace: {}. Components: {}".format(ns, ", ".join(components)))
    if "open-webui" in components:
        print("Open WebUI ingress host (verify against the real chart's own ingress key — see this "
              "script's header comment): {}".format(fqdn))
    print("Registry credentials/entitlement: https://apps.rancher.io (SUSE Application Collection)")


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
    setup_suse_ai(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
