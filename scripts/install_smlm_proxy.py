#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install the SUSE Multi-Linux Manager (SMLM) proxy on Kubernetes
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://documentation.suse.com/multi-linux-manager/5.2/en/docs/specialized-guides/kubernetes-guide/proxy-kubernetes-deployment.html
#
# ─── JSON section: "smlm_proxy" ─────────────────────────────────────────────────
#
# MANDATORY
#   smlm_proxy_fqdn         : Fully-qualified domain name for the proxy
#                             (e.g. "proxy.cluster2.mydemo.lab")
#   smlm_proxy_server       : FQDN of the parent SMLM server
#                             (default: "smlm".smlm_fqdn from the same JSON)
#   smlm_proxy_scc_user     : SCC username (default: "smlm".smlm_scc_user)
#   smlm_proxy_scc_password : SCC password (default: "smlm".smlm_scc_password)
#
# OPTIONAL – parent server access (proxy config generation via spacecmd)
#   smlm_proxy_server_node  : SSH host of the Kubernetes node running the SMLM
#                             server (default: same as smlm_proxy_server)
#   smlm_proxy_server_ns    : Namespace of the server deployment (default: uyuni-server)
#   smlm_proxy_admin_user   : SMLM web UI admin user (default: "smlm".smlm_admin_user or admin)
#   smlm_proxy_admin_pass   : SMLM web UI admin pass (default: "smlm".smlm_admin_pass or admin123)
#   smlm_proxy_email        : Proxy administrator email (default: root@<smlm_proxy_fqdn>)
#   smlm_proxy_ssh_port     : SSH port the proxy listens on (default: 8022)
#   smlm_proxy_max_cache    : Maximum squid cache size in MB (default: 2048;
#                             ~60% of the squid volume is a good value)
#
# OPTIONAL – Helm / release
#   smlm_proxy_version      : Helm chart version (empty = latest; while 5.2 has
#                             no GA chart yet set "5.2.0-rc" explicitly)
#   smlm_proxy_ns           : Kubernetes namespace  (default: uyuni-proxy)
#   smlm_proxy_rel          : Helm release name     (default: uyuni-proxy)
#   smlm_proxy_registry     : OCI registry          (default: registry.suse.com)
#   smlm_proxy_chart        : OCI chart path        (default: suse/multi-linux-manager/5.2/proxy-helm)
#   smlm_proxy_img_repository : Image repository base (default: derived from
#                             smlm_proxy_registry/smlm_proxy_chart + '/x86_64')
#   smlm_proxy_img_tag      : Image tag for all images (default: chart default, "latest")
#
# OPTIONAL – networking / ingress
#   smlm_proxy_shorthn      : Short hostname for the DNS entry (default: proxy)
#   smlm_proxy_ingress_class: Ingress class name (default: traefik)
#
# OPTIONAL – storage
#   smlm_proxy_storage_class: StorageClass for the squid cache volume
#                             (empty = cluster default)
#   smlm_proxy_lh_overprovision : Longhorn storage-over-provisioning percentage
#                             set when smlm_proxy_storage_class is "longhorn"
#                             (default: 500)
#
# NOTE: RKE2 (default) or K3s, with Traefik. On RKE2, Traefik is enabled
#       through the 'ingress-controller' option and the extra TCP entrypoints
#       ssh (8022) and salt-publish/salt-request (4505/4506) are exposed via a
#       rke2-traefik HelmChartConfig. On K3s (kclusters clu_type "k3s") the
#       bundled Traefik is reused and the same ports are exposed through its
#       ServiceLB. The proxy must run on a different cluster than the SMLM
#       server (their Traefik port sets differ). TLS uses self-signed lab
#       certificates.

__version__ = "39753e7"

PLUGIN = {
    "name": "smlm_proxy",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, ssh_run, add_service_dns, die, log  # noqa: E402


def _validate(v):
    definition = v.definition

    def vfall(proxy_field, smlm_field):
        pv = (definition.get("smlm_proxy", {}) or {}).get(proxy_field, "")
        fv = (definition.get("smlm", {}) or {}).get(smlm_field, "")
        if not pv and not fv:
            v.errors.append(
                "[ERROR] smlm_proxy.{} is required (and no smlm.{} fallback found)".format(
                    proxy_field, smlm_field))

    v.vreq("smlm_proxy", "smlm_proxy_fqdn")
    vfall("smlm_proxy_server", "smlm_fqdn")
    vfall("smlm_proxy_scc_user", "smlm_scc_user")
    vfall("smlm_proxy_scc_password", "smlm_scc_password")
    v.vns("smlm_proxy")
    v.vver("smlm_proxy")
    v.vport("smlm_proxy", "smlm_proxy_ssh_port")
    v.vport("smlm_proxy", "smlm_proxy_max_cache")


# ─── Traefik configuration ───────────────────────────────────────────────────

def setup_smlm_proxy_traefik(hostname, clu_type, cfg):
    """Mirrors setup_smlm_proxy_traefik (bash)."""
    ports = ["ssh:{}".format(cfg.get("smlm_proxy_ssh_port") or "8022"), "salt-publish:4505", "salt-request:4506"]
    if clu_type == "k3s":
        k8s.setup_traefik_k3s(hostname, ports)
    else:
        k8s.setup_traefik_rke2(hostname, ports)


# ─── Namespace, secrets and certificates ─────────────────────────────────────

def setup_smlm_proxy_prereqs(hostname, cfg):
    """Mirrors setup_smlm_proxy_prereqs (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_proxy_ns", "uyuni-proxy")

    if (cfg.get("smlm_proxy_storage_class") or "") == "longhorn":
        k8s.set_longhorn_overprovisioning(hostname, cfg.get("smlm_proxy_lh_overprovision") or "500")

    print("# Creating namespace and secrets in '{}'".format(ns))
    ssh_run(hostname, "kubectl create namespace {} --dry-run=client -o yaml | kubectl apply -f -".format(ns))

    ssh_run(hostname,
            "kubectl create secret docker-registry scc-secret "
            "-n {} --docker-server={} --docker-username='{}' --docker-password='{}' "
            "--dry-run=client -o yaml | kubectl apply -f -".format(
                ns, cfg.get("smlm_proxy_registry") or "registry.suse.com", cfg.get("smlm_proxy_scc_user", ""),
                cfg.get("smlm_proxy_scc_password", "")))

    print("  Generating self-signed TLS certificates")
    ssh_run(hostname, (
        "set -e\n"
        "_fqdn={fqdn}\n"
        "_ns={ns}\n"
        "_tmp=$(mktemp -d)\n"
        "\n"
        "openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \\\n"
        "    -keyout ${{_tmp}}/tls.key \\\n"
        "    -out    ${{_tmp}}/tls.crt \\\n"
        "    -subj \"/CN=${{_fqdn}}\" \\\n"
        "    -addext \"subjectAltName=DNS:${{_fqdn}}\" 2>/dev/null\n"
        "\n"
        "kubectl create secret tls proxy-cert -n ${{_ns}} \\\n"
        "    --cert=${{_tmp}}/tls.crt \\\n"
        "    --key=${{_tmp}}/tls.key \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "kubectl create configmap uyuni-ca -n ${{_ns}} \\\n"
        "    --from-file=ca.crt=${{_tmp}}/tls.crt \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "rm -rf ${{_tmp}}"
    # smlm_proxy_fqdn is free-text with no format validation. It is passed through shlex.quote(),
    # which escapes embedded single quotes. Hand-rolled single quotes would not, and could be injected
    # through the remote command. shlex.quote() also nests correctly inside the surrounding heredoc.
    ).format(fqdn=shlex.quote(cfg.get("smlm_proxy_fqdn", "") or ""), ns=shlex.quote(ns)))


# ─── Proxy configuration generation (on the parent SMLM server) ─────────────

def generate_smlm_proxy_config(hostname, cfg):
    """
    Generate the proxy config on the parent SMLM server via spacecmd, fetch
    it, and extract it on the proxy node. Mirrors generate_smlm_proxy_config
    (bash). Since the server runs on Kubernetes, `mgrctl exec` becomes
    `kubectl exec` on the uyuni deployment.
    """
    srv_node = cfg.get("smlm_proxy_server_node") or cfg.get("smlm_proxy_server", "")
    srv_ns = ac.require_k8s_name(cfg, "smlm_proxy_server_ns", "uyuni-server")
    srv_ssh_base = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-q", "root@{}".format(srv_node)]

    print("# Generating the proxy configuration on the SMLM server ('{}' via '{}')".format(
        cfg.get("smlm_proxy_server", ""), srv_node))

    ssh_port = cfg.get("smlm_proxy_ssh_port") or "8022"
    max_cache = cfg.get("smlm_proxy_max_cache") or "2048"
    email = cfg.get("smlm_proxy_email") or "root@{}".format(cfg.get("smlm_proxy_fqdn", ""))

    # admin_user, admin_pass, fqdn, server and email are free-text values with no format validation.
    # They are passed through shlex.quote(), which escapes embedded single quotes and nests inside other
    # quoted shell words, unlike a bare "'{}'".format(...).
    inner_cmd = "proxy_container_config_nossl -p {port} -o /tmp/smlm-proxy-config.tar.gz {fqdn} {server} {max_cache} {email}".format(
        port=ssh_port, fqdn=cfg.get("smlm_proxy_fqdn", ""), server=cfg.get("smlm_proxy_server", ""),
        max_cache=max_cache, email=email,
    )
    remote_cmd = "kubectl exec -n {ns} deploy/uyuni -c uyuni -- spacecmd -q -u {admin_user} -p {admin_pass} -- {inner}".format(
        ns=srv_ns,
        admin_user=shlex.quote(cfg.get("smlm_proxy_admin_user", "")),
        admin_pass=shlex.quote(cfg.get("smlm_proxy_admin_pass", "")),
        inner=shlex.quote(inner_cmd),
    )
    r = subprocess.run(srv_ssh_base + [remote_cmd], check=False)
    if r.returncode != 0:
        die("spacecmd failed to generate the proxy configuration on {}".format(srv_node))

    # spacecmd can exit 0 on failure — make sure the tarball is really there
    r = subprocess.run(
        srv_ssh_base + ["kubectl exec -n {} deploy/uyuni -c uyuni -- test -s /tmp/smlm-proxy-config.tar.gz".format(
            srv_ns)], check=False)
    if r.returncode != 0:
        die("the proxy configuration tarball was not generated on the server")

    print("  Fetching config.tar.gz and extracting it on '{}'".format(hostname))
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tmp_cfg = tf.name
    with open(tmp_cfg, "wb") as f:
        r = subprocess.run(
            srv_ssh_base + ["kubectl exec -n {} deploy/uyuni -c uyuni -- cat /tmp/smlm-proxy-config.tar.gz".format(
                srv_ns)], stdout=f, check=False)
    if r.returncode != 0:
        die("could not fetch the proxy configuration tarball from the server")
    subprocess.run(srv_ssh_base + ["kubectl exec -n {} deploy/uyuni -c uyuni -- rm -f "
                                    "/tmp/smlm-proxy-config.tar.gz".format(srv_ns)], check=False)

    r = subprocess.run(["rsync", "-a", tmp_cfg, "root@{}:/tmp/smlm-proxy-config.tar.gz".format(hostname)],
                        check=False)
    Path(tmp_cfg).unlink()
    if r.returncode != 0:
        die("could not copy the proxy configuration to {}".format(hostname))

    r = ssh_run(hostname,
                "rm -rf /tmp/smlm-proxy-config && mkdir -p /tmp/smlm-proxy-config && "
                "tar -C /tmp/smlm-proxy-config -xzf /tmp/smlm-proxy-config.tar.gz", check=False)
    if r.returncode != 0:
        die("could not extract the proxy configuration tarball")

    r = ssh_run(hostname,
                "test -s /tmp/smlm-proxy-config/config.yaml -a "
                "-s /tmp/smlm-proxy-config/httpd.yaml -a "
                "-s /tmp/smlm-proxy-config/ssh.yaml", check=False)
    if r.returncode != 0:
        die("config.yaml/httpd.yaml/ssh.yaml missing from the generated tarball")


# ─── Helm install ─────────────────────────────────────────────────────────────

def setup_smlm_proxy(hostname, definition, clu_name, clu_type, mydomain, cfg):
    """Mirrors setup_smlm_proxy (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_proxy_ns", "uyuni-proxy")
    rel = cfg.get("smlm_proxy_rel") or "uyuni-proxy"
    chart = "oci://{}/{}".format(cfg.get("smlm_proxy_registry") or "registry.suse.com",
                                  cfg.get("smlm_proxy_chart") or "suse/multi-linux-manager/5.2/proxy-helm")
    ver_arg = "--version {}".format(cfg["smlm_proxy_version"]) if cfg.get("smlm_proxy_version") else ""

    storage_class_arg = "--set volumes.squid.storageClass={}".format(cfg["smlm_proxy_storage_class"]) \
        if cfg.get("smlm_proxy_storage_class") else ""

    if cfg.get("smlm_proxy_img_repository"):
        img_repo = cfg["smlm_proxy_img_repository"]
    else:
        img_repo = "{}/{}".format(cfg.get("smlm_proxy_registry") or "registry.suse.com",
                                   cfg.get("smlm_proxy_chart") or "suse/multi-linux-manager/5.2/proxy-helm")
        if img_repo.endswith("/proxy-helm"):
            img_repo = img_repo[: -len("/proxy-helm")]
        img_repo = img_repo + "/x86_64"
    img_tag_arg = "--set tag={}".format(cfg["smlm_proxy_img_tag"]) if cfg.get("smlm_proxy_img_tag") else ""

    print("# Installing the SUSE Multi-Linux Manager proxy ({})".format(rel))

    result = ssh_run(hostname,
                      "helm upgrade -i {} {} --namespace {} --create-namespace "
                      "--set registrySecret=scc-secret "
                      "--set repository={} "
                      "--set ingress.class={} "
                      "--set-file global.config=/tmp/smlm-proxy-config/config.yaml "
                      "--set-file global.httpd=/tmp/smlm-proxy-config/httpd.yaml "
                      "--set-file global.ssh=/tmp/smlm-proxy-config/ssh.yaml "
                      "{} {} {}".format(
                          rel, chart, ns, img_repo, cfg.get("smlm_proxy_ingress_class") or "traefik",
                          storage_class_arg, img_tag_arg, ver_arg),
                      check=False)
    if result.returncode != 0:
        die("helm install failed for the SMLM proxy")

    ssh_run(hostname, "rm -rf /tmp/smlm-proxy-config /tmp/smlm-proxy-config.tar.gz", check=False)

    print("# Adding DNS entry for the SMLM proxy")
    dns_entry = "{}.{}".format(cfg.get("smlm_proxy_shorthn") or "proxy", clu_name)
    add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain)

    print("# Waiting for the proxy pods to be ready …")
    r = ssh_run(hostname, "kubectl wait pods -n {} --all --for condition=Ready --timeout=900s 2>/dev/null".format(
        ns), check=False)
    if r.returncode != 0:
        log("Pods not fully ready after 15 min — check: kubectl get pods -n {}".format(ns))

    print("")
    print("SUSE Multi-Linux Manager proxy deployed.")
    print("  Proxy FQDN   : {}".format(cfg.get("smlm_proxy_fqdn", "")))
    print("  Parent server: {}".format(cfg.get("smlm_proxy_server", "")))
    print("  SSH port     : {}".format(cfg.get("smlm_proxy_ssh_port") or "8022"))
    print("")
    print("  Register clients against the proxy: {}:4505 / 4506".format(cfg.get("smlm_proxy_fqdn", "")))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    cfg = dict(definition.get("smlm_proxy", {}) or {})

    # Fall back to the "smlm" section for values shared with the server
    # (same-JSON deployments where server and proxy are defined together).
    # Matches bash's ${x:-y}, which overrides an empty string too, not just a
    # missing key — so these are plain truthiness checks, not dict.setdefault.
    smlm_cfg = definition.get("smlm", {}) or {}
    if not cfg.get("smlm_proxy_server"):
        cfg["smlm_proxy_server"] = smlm_cfg.get("smlm_fqdn", "")
    if not cfg.get("smlm_proxy_scc_user"):
        cfg["smlm_proxy_scc_user"] = smlm_cfg.get("smlm_scc_user", "")
    if not cfg.get("smlm_proxy_scc_password"):
        cfg["smlm_proxy_scc_password"] = smlm_cfg.get("smlm_scc_password", "")
    if not cfg.get("smlm_proxy_admin_user"):
        cfg["smlm_proxy_admin_user"] = smlm_cfg.get("smlm_admin_user") or "admin"
    if not cfg.get("smlm_proxy_admin_pass"):
        cfg["smlm_proxy_admin_pass"] = smlm_cfg.get("smlm_admin_pass") or "admin123"

    if not cfg.get("smlm_proxy_fqdn"):
        print("ERROR: smlm_proxy_fqdn is required in the 'smlm_proxy' JSON section", file=sys.stderr)
        sys.exit(1)
    if not cfg.get("smlm_proxy_server"):
        print("ERROR: smlm_proxy_server is required in the 'smlm_proxy' JSON section (or smlm_fqdn in 'smlm')",
              file=sys.stderr)
        sys.exit(1)
    if not cfg.get("smlm_proxy_scc_user"):
        print("ERROR: smlm_proxy_scc_user is required in the 'smlm_proxy' JSON section (or smlm_scc_user in 'smlm')",
              file=sys.stderr)
        sys.exit(1)
    if not cfg.get("smlm_proxy_scc_password"):
        print("ERROR: smlm_proxy_scc_password is required in the 'smlm_proxy' JSON section "
              "(or smlm_scc_password in 'smlm')", file=sys.stderr)
        sys.exit(1)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    clu_type = clu_cfg.get("clu_type", "")
    mydomain = clu_cfg.get("mydomain", "")
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_smlm_proxy_traefik(vm_name, clu_type, cfg)
    setup_smlm_proxy_prereqs(vm_name, cfg)
    generate_smlm_proxy_config(vm_name, cfg)
    setup_smlm_proxy(vm_name, definition, clu_name, clu_type, mydomain, cfg)


if __name__ == "__main__":
    main()
