#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Rancher
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 2.0
#
# JSON section: "rancher" — SUSE Rancher Prime Kubernetes management platform
#
#   rancher_shorthn        : [OPTIONAL] Short hostname for UI ingress         (default: rancher)
#   rancher_rel            : [OPTIONAL] Helm repo alias                       (default: rancher-prime)
#   rancher_repo_url       : [OPTIONAL] Helm repo URL                         (default: https://charts.rancher.com/server-charts/prime)
#   rancher_helm_rel       : [OPTIONAL] Helm release name                     (default: rancher)
#   rancher_helm_chart     : [OPTIONAL] Helm chart reference                  (default: rancher-prime/rancher)
#   rancher_version        : [OPTIONAL] Helm chart version                    (empty = latest, e.g. 2.13.3)
#   rancher_initial_pwd    : [OPTIONAL] Bootstrap admin password; when unset, Rancher generates one and it is printed
#   rancher_replicas       : [OPTIONAL] Number of Rancher replicas            (default: 2)
#   rancher_cert_repo_name : [OPTIONAL] cert-manager Helm repo alias          (default: jetstack)
#   rancher_cert_repo_url  : [OPTIONAL] cert-manager Helm repo URL            (default: https://charts.jetstack.io)
#   cert_manager_ver       : [OPTIONAL] cert-manager Helm chart version       (empty = latest, e.g. v1.20.2)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "rancher",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
    # Version matrix (libs/versions.py). Kubernetes ranges: SUSE Rancher Prime support matrix
    # (https://www.suse.com/suse-rancher/support-matrix/all-supported-versions/) and cert-manager's supported releases
    # (https://cert-manager.io/docs/releases/).
    "versions": {
        "rancher_version": [
            {"version": "2.15", "kubernetes": {"rke2": {"min": "1.34", "max": "1.36"}, "k3s": {"min": "1.34", "max": "1.36"}}},
            {"version": "2.14", "kubernetes": {"rke2": {"min": "1.33", "max": "1.35"}, "k3s": {"min": "1.33", "max": "1.35"}}},
            {"version": "2.13", "kubernetes": {"rke2": {"min": "1.32", "max": "1.34"}, "k3s": {"min": "1.32", "max": "1.34"}}},
            {"version": "2.12", "kubernetes": {"rke2": {"min": "1.31", "max": "1.33"}, "k3s": {"min": "1.31", "max": "1.33"}}},
        ],
        "cert_manager_ver": [
            {"version": "v1.21", "kubernetes": {"rke2": {"min": "1.33", "max": "1.36"}, "k3s": {"min": "1.33", "max": "1.36"}}},
            {"version": "v1.20", "kubernetes": {"rke2": {"min": "1.32", "max": "1.35"}, "k3s": {"min": "1.32", "max": "1.35"}}},
        ],
    },
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
import versions  # noqa: E402
from lab_creation import (  # noqa: E402
    setup_helm, helm_repo_add, ssh_run, ssh_output, add_service_dns, add_dns_to_named_rr,
    restart_named, die,
)


def _validate(v):
    v.vns("rancher")
    v.vver("rancher")
    v.vurl("rancher")


def setup_rancher_repo(hostname, cfg):
    """Add the Rancher and cert-manager Helm repos (Rancher Prime unless rancher_rel/rancher_repo_url are set)."""
    helm_repo_add(hostname, cfg.get("rancher_rel") or "rancher-prime",
                  cfg.get("rancher_repo_url") or "https://charts.rancher.com/server-charts/prime")
    helm_repo_add(hostname, cfg.get("rancher_cert_repo_name") or "jetstack",
                  cfg.get("rancher_cert_repo_url") or "https://charts.jetstack.io")


def setup_cert_manager(hostname, cfg, ingress_classname=None):
    """Install cert-manager and two staging/prod ClusterIssuers. Mirrors setup_cert-manager (bash)."""
    print("# Setup Cert-manager")
    cert_manager_ver = versions.helm_version_flag(cfg.get("cert_manager_ver"))

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

    rancher_helm_chart defaults to rancher-prime/rancher. An unset rancher_initial_pwd lets Rancher generate the
    bootstrap password, which is read back and printed. rancher_version and cert_manager_ver take a bare chart
    version or "--version X" (versions.helm_version_flag()).
    """
    print("# Setup Rancher {} in cluster \"{}\"".format(cfg.get("rancher_helm_rel") or "rancher", clu_name))

    helm_rel = cfg.get("rancher_helm_rel") or "rancher"
    helm_chart = cfg.get("rancher_helm_chart") or "rancher-prime/rancher"
    shorthn = cfg.get("rancher_shorthn") or "rancher"
    hostname_fqdn = "{}.{}.{}".format(shorthn, clu_name, mydomain)
    rancher_version = versions.helm_version_flag(cfg.get("rancher_version"))
    initial_pwd = cfg.get("rancher_initial_pwd") or ""
    pwd_arg = "--set bootstrapPassword={}".format(shlex.quote(initial_pwd)) if initial_pwd else ""
    replicas = cfg.get("rancher_replicas") or "2"

    result = ssh_run(hostname,
                      "helm upgrade -i {} {} --create-namespace --namespace cattle-system "
                      "--set hostname={} {} {} --set replicas={} ".format(
                          shlex.quote(helm_rel), shlex.quote(helm_chart), shlex.quote(hostname_fqdn),
                          rancher_version, pwd_arg, shlex.quote(replicas)),
                      check=False)
    if result.returncode != 0:
        sys.exit(1)

    print("## Add Rancher DNS")
    dns_entry = "{}.{}".format(shorthn, clu_name)
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
