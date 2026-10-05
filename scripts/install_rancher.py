#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Rancher
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "rancher" — SUSE Rancher Prime Kubernetes management platform
#
#   rancher_shorthn        : [OPTIONAL] Short hostname for UI ingress         (default: rancher)
#   rancher_rel            : [OPTIONAL] Helm repo alias                       (default: rancher-prime)
#   rancher_repo_url       : [OPTIONAL] Helm repo URL                         (default: https://charts.rancher.com/server-charts/prime)
#   rancher_helm_rel       : [OPTIONAL] Helm release name                     (default: rancher)
#   rancher_helm_chart     : [OPTIONAL] Helm chart reference                  (default: rancher-prime/rancher)
#   rancher_version        : [OPTIONAL] Helm chart version flag               (empty = latest, e.g. --version 2.13.3)
#   rancher_initial_pwd    : [OPTIONAL] Bootstrap admin password              (default: auto-generated)
#   rancher_replicas       : [OPTIONAL] Number of Rancher replicas            (default: 2)
#   rancher_cert_repo_name : [OPTIONAL] cert-manager Helm repo alias          (default: jetstack)
#   rancher_cert_repo_url  : [OPTIONAL] cert-manager Helm repo URL            (default: https://charts.jetstack.io)
#   cert_manager_ver       : [OPTIONAL] cert-manager version flag             (empty = latest, e.g. --version v1.14.4)

__version__ = "526bc48"

PLUGIN = {
    "name": "rancher",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import (  # noqa: E402
    setup_helm, helm_repo_add, ssh_run, ssh_output, add_service_dns, add_dns_to_named_rr,
    restart_named, die,
)


def _validate(v):
    v.vns("rancher")
    v.vver("rancher")
    v.vurl("rancher")


def setup_rancher_repo(hostname, cfg):
    """
    Add the Rancher and cert-manager Helm repos. Mirrors setup_rancher_repo (bash).

    NOTE: bash's own defaults here ("rancher-stable/rancher" repo alias,
    "https://releases.rancher.com/server-charts/stable" URL) do not match
    this section's own doc comment (which claims "rancher-prime" /
    ".../prime") — a real drift between documentation and the executed code.
    Preserved the CODE's actual defaults for identical behavior; the doc
    comment above is what bash ships, kept verbatim rather than "corrected"
    since fixing the comment doesn't change behavior and fixing the CODE to
    match the comment would (a real behavior change out of scope for a
    faithful port).
    """
    helm_repo_add(hostname, cfg.get("rancher_rel") or "rancher-stable/rancher",
                  cfg.get("rancher_repo_url") or "https://releases.rancher.com/server-charts/stable")
    helm_repo_add(hostname, cfg.get("rancher_cert_repo_name") or "jetstack",
                  cfg.get("rancher_cert_repo_url") or "https://charts.jetstack.io")


def setup_cert_manager(hostname, cfg, ingress_classname=None):
    """Install cert-manager and two staging/prod ClusterIssuers. Mirrors setup_cert-manager (bash)."""
    print("# Setup Cert-manager")
    cert_manager_ver = cfg.get("cert_manager_ver") or ""

    result = ssh_run(hostname,
                      "helm upgrade -i cert-manager jetstack/cert-manager {} --namespace cert-manager "
                      "--create-namespace --set installCRDs=true".format(cert_manager_ver), check=False)
    if result.returncode != 0:
        die("cert-manager helm install failed on '{}'".format(hostname))

    ssh_run(hostname,
            "kubectl wait pods -n cert-manager -l app.kubernetes.io/instance=cert-manager "
            "--for condition=Ready --timeout=120s 2>/dev/null", check=False)

    ingress_class = ingress_classname or "traefik"
    manifest = """\
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-ci
spec:
  acme:
    server: https://acme-staging-v02.api.letsencrypt.org/directory
    email: none@someunknowndomain.com
    privateKeySecretRef:
      name: letsencrypt-staging
    solvers:
      - http01:
          ingress:
            class: {ingress_class}
...
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: none@someunknowndomain.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: {ingress_class}
""".format(ingress_class=ingress_class)
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)


def setup_rancher(hostname, definition, clu_name, mydomain, clu_type, cfg, remote_dns_servers=None):
    """
    Install Rancher via Helm, register its DNS entry (both via add_service_dns
    and explicitly for every matching server node), and retrieve the
    bootstrap password. Mirrors setup_rancher (bash).

    NOTE: bash's own doc comment claims rancher_helm_chart defaults to
    "rancher-prime/rancher" and rancher_version/cert_manager_ver are version
    flags — but the executed helm command has NO fallback for
    rancher_helm_chart at all (uses it raw; empty if unset in the JSON) and
    rancher_version is interpolated raw too (expected to already contain the
    literal "--version X" text, per its own doc example — same convention as
    cert_manager_ver, and unlike every other addon's own X_version fields,
    which are bare version strings that get wrapped in "--version " here).
    Preserved exactly, no defaults invented.
    """
    print("# Setup Rancher {} in cluster \"{}\"".format(cfg.get("rancher_helm_rel") or "rancher", clu_name))

    helm_rel = cfg.get("rancher_helm_rel") or "rancher"
    helm_chart = cfg.get("rancher_helm_chart") or ""
    hostname_fqdn = "{}.{}.{}".format(cfg.get("rancher_shorthn", ""), clu_name, mydomain)
    rancher_version = cfg.get("rancher_version") or ""
    initial_pwd = cfg.get("rancher_initial_pwd") or ""
    replicas = cfg.get("rancher_replicas") or "2"

    result = ssh_run(hostname,
                      "helm upgrade -i {} {} --create-namespace --namespace cattle-system "
                      "--set hostname={} {} --set bootstrapPassword={} --set replicas={} ".format(
                          shlex.quote(helm_rel), shlex.quote(helm_chart), shlex.quote(hostname_fqdn),
                          rancher_version, shlex.quote(initial_pwd), shlex.quote(replicas)),
                      check=False)
    if result.returncode != 0:
        sys.exit(1)

    print("## Add Rancher DNS")
    dns_entry = "{}.{}".format(cfg.get("rancher_shorthn") or "ERROR_ranchershort", clu_name)
    add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain, remote_dns_servers=remote_dns_servers)

    install_key = "INSTALL_{}_TYPE".format(clu_type.upper())
    matching_nodes = [
        name for name, node_cfg in definition.get("nodes", {}).items()
        if node_cfg.get(install_key) == "server" and node_cfg.get("kcluster") == clu_name
    ]
    for node_name in matching_nodes:
        add_dns_to_named_rr(definition, dns_entry, node_name, mydomain, remote_dns_servers=remote_dns_servers)

    restart_named(remote_dns_servers)

    print("Wait for rancher to be ready")
    ssh_run(hostname, "kubectl wait pods -n cattle-system -l app=rancher --for condition=Ready --timeout=300s",
            check=False)
    time.sleep(60)

    initial_rancher_pwd = ssh_output(
        hostname,
        "kubectl get secret --namespace cattle-system bootstrap-secret "
        "-o jsonpath='{.data.bootstrapPassword}' | base64 -d")
    if not initial_rancher_pwd:
        die("Failed to retrieve bootstrap secret")

    print("# Initial password: {}".format(initial_rancher_pwd))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()

    # NOTE: same pattern as install_harvester — bash never scans for a node
    # here, it relies entirely on _vm_name/clu_name inherited from the
    # environment (set by setup_lab.py's cluster-addon invocation).
    vm_name = os.environ.get("_vm_name", "")
    clu_name = os.environ.get("clu_name", "")
    node_cfg = definition.get("nodes", {}).get(vm_name, {})

    # Support both KUBERNETES_NODE_TYPE (new) and INSTALL_RKE2_TYPE (deprecated)
    node_type = node_cfg.get("KUBERNETES_NODE_TYPE") or node_cfg.get("INSTALL_RKE2_TYPE", "")
    if node_type in ("server", ""):
        print("Using node: \"{}\"".format(vm_name))
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        cfg = definition.get("rancher", {}) or {}
        online = definition.get("common", {}).get("online") == "1"
        remote_dns_servers = config.get("REMOTE_DNS_SERVERS", "").split() or None

        setup_helm(vm_name, clu_name, online=online)
        print("Setup_rancher_repo")
        setup_rancher_repo(vm_name, cfg)
        setup_cert_manager(vm_name, cfg, ingress_classname=definition.get("common", {}).get("ingressClassname"))
        setup_rancher(vm_name, definition, clu_name, clu_cfg.get("mydomain", ""), clu_cfg.get("clu_type", ""),
                      cfg, remote_dns_servers=remote_dns_servers)


if __name__ == "__main__":
    main()
