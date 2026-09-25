#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUSE AI (SUSE's own Ollama + Open WebUI + Milvus AI stack)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "suse_ai" — configurable keys:
#   suse_ai_registry_user     : [MANDATORY] SUSE Application Collection registry username (your SCC
#                               login email — same credential family as this project's SUSE_regcode/
#                               SUSE_email registration keys, but a SEPARATE Application Collection
#                               entitlement token, not your SCC registration code itself)
#   suse_ai_registry_password : [MANDATORY] SUSE Application Collection registry password/token
#   suse_ai_registry_account : [OPTIONAL] name of an encrypted credential_kind "appcollection"
#                               file under /etc/lab_creation/credentials/ (see README's
#                               Credentials section) to read suse_ai_registry_user/
#                               suse_ai_registry_password from instead of this section's own
#                               plaintext fields — auto-discovered if exactly one
#                               "appcollection" credential file exists and this is left unset.
#                               The plaintext fields above remain fully valid either way.
#   suse_ai_registry          : [OPTIONAL] OCI registry host (default: dp.apps.rancher.io)
#   suse_ai_ns                : [OPTIONAL] namespace (default: suse-private-ai — SUSE's own documented
#                               default, kept as-is rather than following this project's usual
#                               <name>-system convention, to match SUSE's own docs/support examples)
#   suse_ai_components        : [OPTIONAL] space-separated list of components to install (default:
#                               "ollama open-webui"). Add "milvus" for local RAG vector search — Open
#                               WebUI can use Milvus OR OpenSearch as its RAG backend; OpenSearch isn't
#                               wired up here, add it via a separate addon/values override if preferred.
#   suse_ai_ollama_version     : [OPTIONAL] chart version pin for the ollama component (empty = latest)
#   suse_ai_open_webui_version : [OPTIONAL] chart version pin for the open-webui component
#   suse_ai_milvus_version     : [OPTIONAL] chart version pin for the milvus component
#   suse_ai_tls_source         : [OPTIONAL] "suse-private-ai" (self-signed, default) | "letsEncrypt"
#                               (needs public DNS + cert-manager's HTTP-01) | "secret" (bring your own
#                               cert, no cert-manager needed)
#   suse_ai_shorthn            : [OPTIONAL] hostname prefix for Open WebUI's ingress (default: ai)
#   suse_ai_extra_set_<component> : [OPTIONAL] e.g. suse_ai_extra_set_open-webui — a raw space-
#                               separated "key=value" list appended as extra `--set` flags to that
#                               component's helm install, for anything not covered by the keys above
#                               (see the note below on why this exists instead of guessed values keys)
#
# PREREQUISITES this addon does NOT install for you (document/configure separately): cert-manager
# (unless suse_ai_tls_source="secret"), an ingress controller already present on the cluster, and
# (confirmed live, see below) a StorageClass — a bare RKE2 cluster has none by default.
#
# PARTIALLY LIVE-TESTED 2026-09-05 on a disposable single-node RKE2 cluster on nuc6.mydemo.lab: the
# namespace + `application-collection` docker-registry Secret creation both confirmed working
# end-to-end for real (`kubectl get secret application-collection -n suse-private-ai` — type
# kubernetes.io/dockerconfigjson, present). The OCI `helm registry login` step itself could NOT be
# exercised for real — no genuine SUSE Application Collection entitlement was available, only this
# project's own SUSE_email/SUSE_regcode (SCC product-registration credentials) from
# lab_creation.cfg, tried on the theory they might double as Application Collection access. They do
# NOT: a real `401 Unauthorized` came back from dp.apps.rancher.io, confirming these are genuinely
# separate credential families — exactly what this addon's own header already (correctly) warned
# about before this test. A real bug WAS found and fixed by this: the login failure crashed with an
# uncaught RuntimeError/Python traceback instead of a clear message — `setup_suse_ai_registry()` now
# checks the login's exit code itself and dies with an actionable error instead. Still not verified:
# an actual successful login/chart pull, which needs a real entitlement to test at all.
#
# Additionally verified against documentation.suse.com/suse-ai/1.0 and github.com/SUSE/suse-ai-deployer, 2026-09-05:
# the suse-private-ai namespace default, the "application-collection" registry-secret convention, the
# ollama/open-webui/milvus component split, and the oci://dp.apps.rancher.io/charts/milvus chart
# reference (confirmed via a real community install example: `helm upgrade --install milvus
# oci://dp.apps.rancher.io/charts/milvus -n suseai --version 4.2.2`). NOT independently confirmed:
# the exact OCI chart path for "ollama" (assumed to follow the same oci://<registry>/charts/<name>
# pattern as milvus, since SUSE's own meta chart deploys it as a same-family component — verify with
# `helm show chart oci://dp.apps.rancher.io/charts/ollama` before relying on it), and the SUSE-AI-
# specific Open WebUI chart's own ingress/TLS values keys (this project's community "open_webui"
# addon's keys are confirmed for the UPSTREAM open-webui chart, but SUSE republishes its own OCI build
# which may structure ingress/TLS differently around suse_ai_tls_source above — hence the
# suse_ai_extra_set_<component> escape hatch instead of guessing a specific key that might silently
# produce an unreachable service).
#
# This deploys SUSE's own curated, GPU-aware rebuild of the same Ollama/Open-WebUI/Milvus components
# this project's separate "ollama"/"open_webui"/"milvus" addons already install from their community
# upstream charts — don't run both against the same cluster/namespace at once.

__version__ = "0259515"

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
              "Collection entitlement, NOT your SCC registration code/email (confirmed live "
              "2026-09-05: those two credential families are separate — the SCC login this "
              "project's SUSE_email/SUSE_regcode lab_creation.cfg keys use for product "
              "registration does not also authenticate to dp.apps.rancher.io). Get Application "
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
