"""
k8s.py — Kubernetes cluster setup and addon execution helpers.

Python equivalent of k8s_functions.bash.

Typical usage:
    from k8s import (
        list_kclusters, get_vm_kcluster, load_kclu_vars,
        setup_k3s, setup_rke2,
        add_kclu_dns,
        first_server_node, addon_nodes, iter_cluster_nodes,
        setup_traefik_rke2, setup_traefik_k3s,
        create_basic_auth_secret, set_longhorn_overprovisioning,
        setup_cnpg_operator,
    )
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from lab_creation import (
    die, log, warn,
    ssh_run, ssh_output,
    prepare_local_as_kubeclient,
)
from services import DNSService
import apps


# ── Cluster metadata ──────────────────────────────────────────────────────────

def list_kclusters(definition):
    """
    Return a list of all kcluster names defined in the lab definition
    (mirrors list_kclusters).
    """
    return list(definition.get("kclusters", {}).keys())


def get_vm_kcluster(definition, vm_name):
    """
    Return the kcluster name that vm_name belongs to, or '' if not set
    (mirrors get_vm_kcluster).
    """
    return definition.get("nodes", {}).get(vm_name, {}).get("kcluster", "")


def load_kclu_vars(definition, clu_name):
    """
    Return all scalar key-value pairs for a kcluster (mirrors load_kclu_vars).

    Args:
        definition : Loaded lab definition dict.
        clu_name   : The kcluster name to load.

    Returns a dict. Raises SystemExit if clu_name is not found.
    """
    if not clu_name:
        return {}
    kclusters = definition.get("kclusters", {})
    if clu_name not in kclusters:
        die("kcluster '{}' not found in definition".format(clu_name))
    return {
        k: v
        for k, v in kclusters[clu_name].items()
        if isinstance(v, (str, int, float, bool))
    }


# ── DNS ───────────────────────────────────────────────────────────────────────

def add_kclu_dns(definition, clu_name, clu_type, mydomain, remote_dns_servers=None):
    """
    Register the cluster API/service DNS entry (mirrors add_kclu_dns).

    Prefers agent nodes for round-robin; falls back to all nodes in the cluster.
    """
    DNSService().add_service_dns(
        definition=definition,
        clu_name=clu_name,
        clu_type=clu_type,
        dns_entry=clu_name,
        mydomain=mydomain,
        remote_dns_servers=remote_dns_servers,
    )


# ── Kubernetes distribution interface ───────────────────────────────────────
#
# K8sDistro is the interface for each Kubernetes distribution. get_distro() returns the implementation by name, so a new
# distribution needs no change in the orchestrator. clu_ctx is the dict that load_kclu_vars() returns.
#
# setup_k3s() and setup_rke2() remain as thin wrappers with their original signatures. The RKE2 node config is written by
# RKE2Distro.write_node_config().

class K8sDistro(object):
    """Interface a Kubernetes distribution implements."""

    name = None

    def install_server(self, hostname, clu_name, clu_ctx, token=None, rancher1_ip=None):
        raise NotImplementedError

    def install_agent(self, hostname, clu_name, clu_ctx, token, rancher1_ip):
        raise NotImplementedError

    def kubeconfig_path(self):
        raise NotImplementedError

    def token_path(self):
        raise NotImplementedError


class K3sDistro(K8sDistro):
    name = "k3s"

    def kubeconfig_path(self):
        return "/etc/rancher/k3s/k3s.yaml"

    def token_path(self):
        return "/var/lib/rancher/k3s/server/node-token"

    def install_server(self, hostname, clu_name, clu_ctx, token=None, rancher1_ip=None):
        return self._install(hostname, clu_name, clu_ctx, token=token, rancher1_ip=rancher1_ip)

    def install_agent(self, hostname, clu_name, clu_ctx, token, rancher1_ip):
        return self._install(hostname, clu_name, clu_ctx, token=token, rancher1_ip=rancher1_ip)

    def _install(self, hostname, clu_name, clu_ctx, token=None, rancher1_ip=None):
        """
        Install K3s on a node. Call with token=None for the first server
        node. Pass the returned token to each subsequent node to join the
        cluster. K3s has no separate "agent install" step — providing
        K3S_URL/K3S_TOKEN is what makes a node an agent — so install_server
        and install_agent both funnel into this one path, exactly as the
        original setup_k3s() did (dispatching on token is None vs not, with
        no node_type parameter at all).

        Returns (token, rancher1_ip) — pass these to subsequent node calls.
        """
        clu_rel = clu_ctx.get("clu_rel") or "stable"
        mydomain = clu_ctx.get("mydomain", "")

        prepare_local_as_kubeclient()
        ssh_run(hostname, "mkdir -p /etc/rancher/k3s")

        if token is None:
            log("  Installing K3s server on '{}' (first node of '{}')".format(hostname, clu_name))
            # clu_rel, clu_name and mydomain are free text from the lab JSON, with no validation. They are shell-quoted before they
            # reach the remote command.
            ssh_run(
                hostname,
                "curl -sfL https://get.k3s.io | "
                "INSTALL_K3S_CHANNEL={} "
                "sh -s - server --tls-san {}.{}".format(
                    shlex.quote(clu_rel), shlex.quote(clu_name), shlex.quote(mydomain))
            )
            # The k3s install script can leave the service enabled but not started, on an immutable-root image with a pending reboot
            # flag. The service starts without a reboot. The distribution therefore runs an explicit systemctl enable --now afterwards,
            # as RKE2Distro does, and does not rely on the vendor script to start it.
            ssh_run(hostname, "systemctl enable --now k3s")
            token = ssh_output(hostname, "cat /var/lib/rancher/k3s/server/node-token")
            return token, hostname
        else:
            log("  Joining K3s cluster '{}' on '{}'".format(clu_name, hostname))
            ssh_run(
                hostname,
                "curl -sfL https://get.k3s.io | "
                "INSTALL_K3S_CHANNEL={} "
                "K3S_URL=https://{}:6443 "
                "K3S_TOKEN={} "
                "sh -".format(shlex.quote(clu_rel), shlex.quote(rancher1_ip), shlex.quote(token))
            )
            ssh_run(hostname, "systemctl enable --now k3s-agent")
            return token, rancher1_ip


# These base64 blobs encode small config snippets (NetworkManager, sysctl, PATH)
# copied directly from the bash library to avoid any modification risk.
_RKE2_NM_CONF = "W2tleWZpbGVdCnVubWFuYWdlZC1kZXZpY2VzPWludGVyZmFjZS1uYW1lOmNhbGkqO2ludGVyZmFjZS1uYW1lOmZsYW5uZWwq"
_RKE2_SYSCTL  = "bmV0LmlwdjQuY29uZi5hbGwuZm9yd2FyZGluZz0xCm5ldC5pcHY2LmNvbmYuYWxsLmZvcndhcmRpbmc9MQ=="
_RKE2_PATH    = "ZXhwb3J0IFBBVEg9JFBBVEg6L29wdC9ya2UyL2JpbjovdmFyL2xpYi9yYW5jaGVyL3JrZTIvYmluLwpleHBvcnQgS1VCRUNPTkZJRz0vZXRjL3JhbmNoZXIvcmtlMi9ya2UyLnlhbWwKCg=="


class RKE2Distro(K8sDistro):
    name = "rke2"

    def kubeconfig_path(self):
        return "/etc/rancher/rke2/rke2.yaml"

    def token_path(self):
        return "/var/lib/rancher/rke2/server/node-token"

    def install_server(self, hostname, clu_name, clu_ctx, token=None, rancher1_ip=None):
        return self._install(hostname, clu_name, clu_ctx, "server", token=token, rancher1_ip=rancher1_ip)

    def install_agent(self, hostname, clu_name, clu_ctx, token, rancher1_ip):
        return self._install(hostname, clu_name, clu_ctx, "agent", token=token, rancher1_ip=rancher1_ip)

    def write_node_config(self, clu_name, clu_ctx):
        """
        Write local RKE2 config files (server + agent) if they do not
        already exist. Writes both regardless of role — the original
        _write_rke2_config() took a node_type argument but its body ignored
        it and always wrote both files; preserved here unchanged.
        """
        mydomain = clu_ctx.get("mydomain", "")
        clu_dir = Path(clu_name)
        clu_dir.mkdir(exist_ok=True)
        content = textwrap.dedent("""\
            write-kubeconfig-mode: "0600"
            tls-san:
              - "{domain}"
              - "{clu}.{domain}"
        """.format(domain=mydomain, clu=clu_name))
        for kind in ("server", "agent"):
            cfg = clu_dir / "config-{}.yaml".format(kind)
            if not cfg.exists():
                cfg.write_text(content)

    def _install(self, hostname, clu_name, clu_ctx, node_type, token=None, rancher1_ip=None):
        """
        Install RKE2 on a node.

        Args:
            hostname    : Target VM hostname or IP.
            clu_name    : Cluster name.
            clu_ctx     : This kcluster's vars (clu_rel, mydomain, install_method, …).
            node_type   : "server" or "agent".
            token       : Join token; None for the first server node.
            rancher1_ip : First server address for joining; None for the first node.

        Returns:
            (token, rancher1_ip) — pass to subsequent nodes.
        """
        clu_type = self.name
        clu_rel = clu_ctx.get("clu_rel") or "stable"
        install_method = clu_ctx.get("install_method", "")
        mydomain = clu_ctx.get("mydomain", "")

        prepare_local_as_kubeclient()

        log("  Configuring host for RKE2 on '{}'".format(hostname))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/NetworkManager/conf.d/rke2-canal.conf; chmod 0420 /etc/NetworkManager/conf.d/rke2-canal.conf".format(_RKE2_NM_CONF))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/sysctl.d/90-rke2.conf; chmod 0420 /etc/sysctl.d/90-rke2.conf".format(_RKE2_SYSCTL))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/profile.d/rke2.sh; chmod 0420 /etc/profile.d/rke2.sh".format(_RKE2_PATH))
        ssh_run(hostname, "mkdir -p /var/lib/rancher/{0} /etc/rancher/{0}".format(clu_type))

        log("  Installing RKE2 on '{}'".format(hostname))
        # install_method and clu_rel are free text from the lab JSON, so they are shell-quoted. clu_type and node_type are fixed
        # literals, so they are not quoted.
        ssh_run(
            hostname,
            "curl -sfL https://get.{clu_type}.io | "
            "INSTALL_RKE2_TYPE={node_type} "
            "INSTALL_RKE2_METHOD={install_method} "
            "INSTALL_RKE2_CHANNEL={clu_rel} "
            "sh -".format(
                clu_type=clu_type, node_type=node_type,
                install_method=shlex.quote(install_method), clu_rel=shlex.quote(clu_rel),
            )
        )

        self.write_node_config(clu_name, clu_ctx)
        config_src = str(Path(clu_name) / "config-{}.yaml".format(node_type))
        subprocess.run(
            ["rsync", "-a", config_src,
             "root@{}:/etc/rancher/{}/config.yaml".format(hostname, clu_type)],
            check=True,
        )

        if token is None:
            log("  Starting RKE2 server on '{}' (first node of '{}')".format(hostname, clu_name))
            ssh_run(hostname, "systemctl enable --now {}-{}.service".format(clu_type, node_type))
            token = ssh_output(hostname, "cat /var/lib/rancher/{}/server/node-token".format(clu_type))
            return token, hostname
        else:
            log("  Joining RKE2 cluster '{}' on '{}'".format(clu_name, hostname))
            ssh_run(hostname, "echo 'server: https://{}:9345' >> /etc/rancher/{}/config.yaml".format(
                rancher1_ip, clu_type))
            ssh_run(hostname, "echo 'token: {}' >> /etc/rancher/{}/config.yaml".format(
                token, clu_type))
            ssh_run(hostname, "systemctl enable --now {}-{}.service".format(clu_type, node_type))
            return token, rancher1_ip


DISTROS = {
    "k3s": K3sDistro,
    "rke2": RKE2Distro,
}


def get_distro(clu_type):
    """Return a K8sDistro instance for clu_type, or die() naming supported distros."""
    cls = DISTROS.get(clu_type)
    if cls is None:
        die("Unknown Kubernetes distribution '{}' — supported: {}".format(
            clu_type, ", ".join(sorted(DISTROS))))
    return cls()


# ── K3s / RKE2 back-compat wrappers ─────────────────────────────────────────

def setup_k3s(hostname, clu_name, clu_rel, mydomain, token=None, rancher1_ip=None):
    """
    Install K3s on a node. Thin wrapper — body moved to K3sDistro.
    """
    clu_ctx = {"clu_rel": clu_rel, "mydomain": mydomain}
    distro = K3sDistro()
    if token is None:
        return distro.install_server(hostname, clu_name, clu_ctx, token=token, rancher1_ip=rancher1_ip)
    return distro.install_agent(hostname, clu_name, clu_ctx, token, rancher1_ip)


def setup_rke2(
    hostname, vm_name, clu_name, clu_type, clu_rel, mydomain,
    node_type="server", token=None, rancher1_ip=None, install_method="",
):
    """
    Install RKE2 on a node. Thin wrapper — body moved to RKE2Distro.

    vm_name/clu_type are accepted for signature back-compat but unused:
    vm_name was already dead in the original (never referenced in its body);
    clu_type is now implied by RKE2Distro itself (this codebase only ever
    called setup_rke2 with clu_type="rke2").
    """
    clu_ctx = {"clu_rel": clu_rel, "mydomain": mydomain, "install_method": install_method}
    distro = RKE2Distro()
    if node_type == "agent":
        return distro.install_agent(hostname, clu_name, clu_ctx, token, rancher1_ip)
    return distro.install_server(hostname, clu_name, clu_ctx, token=token, rancher1_ip=rancher1_ip)


# ── Node iterator helpers ─────────────────────────────────────────────────────

def first_server_node(definition):
    """
    Return (vm_name, ssh_cmd) for the first server node in the definition, or None if there is none. This matches on_first_server.
    A node is a server when INSTALL_RKE2_TYPE is "server" or absent.
    """
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("INSTALL_RKE2_TYPE", "") in ("server", ""):
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -q root@{}".format(vm_name)
            log("# Using node: {}".format(vm_name))
            return vm_name, ssh_cmd
    warn("No server node found in definition")
    return None


def addon_nodes(definition, addon, vm_name=None):
    """
    Return a list of (vm_name, ssh_cmd) for nodes that have addon in their addons[]
    list (mirrors on_addon_nodes).

    If vm_name is given, return only that node without scanning the definition.

    Returns a list of (vm_name, ssh_cmd) tuples.

    An addons[] entry can be a plain "<addon>" string or a single-key
    {"<addon>": {...}} mapping (per-node config override — see
    apps.addon_entry_name()'s docstring); either way counts as "has addon".
    """
    if vm_name:
        ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new root@{}".format(vm_name)
        log("# Using node: {}".format(vm_name))
        return [(vm_name, ssh_cmd)]

    results = []
    for name, node_cfg in definition.get("nodes", {}).items():
        entries = node_cfg.get("addons", []) or []
        if any(apps.addon_entry_name(e) == addon for e in entries):
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new root@{}".format(name)
            log("# Using node: {}".format(name))
            results.append((name, ssh_cmd))

    if not results:
        warn("No node with addon '{}' found in definition".format(addon))
    return results


def addon_node_config(definition, addon, vm_name):
    """
    Effective config for `addon` on `vm_name`: the shared top-level
    definition[addon] section, with any per-node override layered on top
    from nodes[vm_name].addons' own {"<addon>": {...}} entry for this addon
    (see apps.addon_entry_name()/addon_entry_overrides()). A plain "<addon>"
    string entry (or no override dict) means no override — the shared
    section applies as-is, exactly like before this mechanism existed.

    Every VM-level addon whose config can vary per node (e.g.
    client_registration, one activation key per OS registering against the
    same server) should read its config through this instead of
    `definition.get(addon, {})` directly.
    """
    shared = definition.get(addon, {}) or {}
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    for entry in node_cfg.get("addons") or []:
        if apps.addon_entry_name(entry) == addon:
            overrides = apps.addon_entry_overrides(entry)
            if overrides:
                return dict(shared, **overrides)
    return shared


def iter_cluster_nodes(definition, clu_name):
    """
    Yield (vm_name, node_cfg, ssh_cmd) for every node that belongs to clu_name,
    in definition order.
    """
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("kcluster") == clu_name:
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -q root@{}".format(vm_name)
            yield vm_name, node_cfg, ssh_cmd


# ── Traefik on RKE2 ────────────────────────────────────────────────────────────
#
# Switches the RKE2 bundled ingress from nginx to Traefik and exposes extra TCP
# entrypoints, following the SUSE Multi-Linux Manager kubernetes guide: the
# 'ingress-controller: traefik' RKE2 config option plus a HelmChartConfig for
# the packaged rke2-traefik chart (which binds hostPorts 80/443 by default).
# Mirrors setup_traefik_rke2 (bash). Requires hostname to be a cluster server
# node (bash required $ssh_command to already point at one).

def setup_traefik_rke2(hostname, extra_ports=None):
    """
    extra_ports : list of "name:port" strings, e.g. ["salt-publish:4505"].
    """
    extra_ports = extra_ports or []
    log("# Configuring RKE2 Traefik ingress (extra TCP ports: {})".format(
        ", ".join(extra_ports) if extra_ports else "none"))

    # Uninstall the upstream-chart Traefik left by older script versions; it
    # would hold hostPorts 80/443 and block rke2-traefik.
    if ssh_run(hostname, "helm status traefik -n kube-system", check=False, capture=True).returncode == 0:
        log("  Removing upstream-chart Traefik install …")
        ssh_run(hostname, "helm uninstall traefik -n kube-system", check=False)

    # Write the HelmChartConfig with the extra entrypoints before enabling
    # Traefik, so it starts with the right ports on first deploy.
    manifest = [
        "apiVersion: helm.cattle.io/v1",
        "kind: HelmChartConfig",
        "metadata:",
        "  name: rke2-traefik",
        "  namespace: kube-system",
        "spec:",
        "  valuesContent: |-",
        "    ingressClass:",
        "      isDefaultClass: true",
        "    ports:",
    ]
    for ep in extra_ports:
        name, port = ep.split(":", 1)
        manifest += [
            "      {}:".format(name),
            "        port: {}".format(port),
            "        exposedPort: {}".format(port),
            "        protocol: TCP",
            "        hostPort: {}".format(port),
            "        expose:",
            "          default: true",
        ]
    result = ssh_run(hostname, "cat > /var/lib/rancher/rke2/server/manifests/lab-traefik-config.yaml",
                      input_text="\n".join(manifest) + "\n", check=False)
    if result.returncode != 0:
        die("Failed to write the rke2-traefik HelmChartConfig")

    # Replace nginx with traefik in the RKE2 config.
    ssh_run(hostname, """
grep -q '^ingress-controller:' /etc/rancher/rke2/config.yaml 2>/dev/null || \\
    echo 'ingress-controller: traefik' >> /etc/rancher/rke2/config.yaml
if grep -q 'disable:' /etc/rancher/rke2/config.yaml 2>/dev/null; then
    grep -q 'rke2-ingress-nginx' /etc/rancher/rke2/config.yaml || \\
        sed -i '/^disable:/a\\\\  - rke2-ingress-nginx' /etc/rancher/rke2/config.yaml
else
    echo -e 'disable:\\n  - rke2-ingress-nginx' >> /etc/rancher/rke2/config.yaml
fi""", check=False)

    # The config change only takes effect on rke2-server restart.
    traefik_missing = ssh_run(hostname, "kubectl get ds -n kube-system rke2-traefik",
                               check=False, capture=True).returncode != 0
    nginx_present = traefik_missing or ssh_run(
        hostname, "kubectl get ds -n kube-system rke2-ingress-nginx-controller",
        check=False, capture=True).returncode == 0
    if traefik_missing or nginx_present:
        log("  Restarting rke2-server to deploy Traefik …")
        ssh_run(hostname,
                "rm -f /var/lib/rancher/rke2/server/manifests/rke2-ingress-nginx.yaml; "
                "systemctl restart rke2-server", check=False)
        for _ in range(30):
            if ssh_run(hostname, "kubectl get nodes", check=False, capture=True).returncode == 0:
                break
            time.sleep(10)

    log("  Waiting for Traefik to be ready …")
    for _ in range(30):
        if ssh_run(hostname, "kubectl get ds -n kube-system rke2-traefik",
                   check=False, capture=True).returncode == 0:
            break
        time.sleep(10)
    result = ssh_run(hostname, "kubectl rollout status ds/rke2-traefik -n kube-system --timeout=300s", check=False)
    if result.returncode != 0:
        die("Traefik did not become ready — check: kubectl get pods -n kube-system")


def create_basic_auth_secret(hostname, namespace, name, user, password):
    """
    Create or update a username/password secret in a namespace. Mirrors
    create_basic_auth_secret (bash). Requires hostname to be a cluster server node.
    """
    # user/password are free text — shlex.quote every field so a value with a
    # quote / space / $() can't break out of the remote shell command.
    ssh_run(hostname,
            "kubectl create secret generic {} -n {} --from-literal=username={} "
            "--from-literal=password={} --dry-run=client -o yaml | kubectl apply -f -".format(
                shlex.quote(name), shlex.quote(namespace),
                shlex.quote(user), shlex.quote(password)))


def set_longhorn_overprovisioning(hostname, percentage):
    """
    Raise the Longhorn storage-over-provisioning percentage so thin volumes
    with a nominal size larger than the physical disk can still be scheduled
    (lab use). Mirrors set_longhorn_overprovisioning (bash).
    """
    log("# Setting Longhorn over-provisioning to {}%".format(percentage))
    result = ssh_run(
        hostname,
        "kubectl patch settings.longhorn.io storage-over-provisioning-percentage "
        "-n longhorn-system --type=merge -p '{{\"value\":\"{}\"}}'".format(percentage),
        check=False,
    )
    if result.returncode != 0:
        log("  Could not adjust Longhorn over-provisioning (continuing)")


# ── Traefik on K3s ─────────────────────────────────────────────────────────────
#
# Exposes extra TCP entrypoints on the Traefik ingress bundled with K3s via a
# HelmChartConfig. Unlike RKE2, no ingress switch and no hostPorts are needed —
# K3s deploys Traefik by default and its ServiceLB publishes the service ports
# on the node. Mirrors setup_traefik_k3s (bash).

def setup_traefik_k3s(hostname, extra_ports=None):
    """extra_ports : list of "name:port" strings, e.g. ["salt-publish:4505"]."""
    extra_ports = extra_ports or []
    log("# Configuring K3s Traefik ingress (extra TCP ports: {})".format(
        ", ".join(extra_ports) if extra_ports else "none"))

    manifest = [
        "apiVersion: helm.cattle.io/v1",
        "kind: HelmChartConfig",
        "metadata:",
        "  name: traefik",
        "  namespace: kube-system",
        "spec:",
        "  valuesContent: |-",
        "    ports:",
    ]
    for ep in extra_ports:
        name, port = ep.split(":", 1)
        manifest += [
            "      {}:".format(name),
            "        port: {}".format(port),
            "        exposedPort: {}".format(port),
            "        protocol: TCP",
            "        expose:",
            "          default: true",
        ]
    result = ssh_run(hostname, "cat > /var/lib/rancher/k3s/server/manifests/lab-traefik-config.yaml",
                      input_text="\n".join(manifest) + "\n", check=False)
    if result.returncode != 0:
        die("Failed to write the traefik HelmChartConfig")

    # The k3s helm controller re-runs the traefik chart job on its own; wait
    # until the service picks up the first extra port, then for the rollout.
    if extra_ports:
        first_port = extra_ports[0].split(":", 1)[1]
        for _ in range(30):
            out = ssh_run(
                hostname,
                "kubectl get svc traefik -n kube-system -o jsonpath='{.spec.ports[*].port}' 2>/dev/null",
                check=False, capture=True,
            ).stdout or ""
            if first_port in out.split():
                break
            time.sleep(10)

    result = ssh_run(hostname, "kubectl rollout status deploy/traefik -n kube-system --timeout=300s", check=False)
    if result.returncode != 0:
        die("Traefik did not become ready — check: kubectl get pods -n kube-system")


def setup_cnpg_operator(hostname, chart_version=None):
    """
    Install the CloudNativePG operator (HA PostgreSQL) via its Helm chart.
    Mirrors setup_cnpg_operator (bash).
    """
    log("# Installing the CloudNativePG operator")
    version_arg = "--version {}".format(chart_version) if chart_version else ""
    result = ssh_run(
        hostname,
        "helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null; helm repo update >/dev/null; "
        "helm upgrade --install cnpg cnpg/cloudnative-pg "
        "--namespace cnpg-system --create-namespace {}".format(version_arg),
        check=False,
    )
    if result.returncode != 0:
        die("helm install failed for the CloudNativePG operator")
    result = ssh_run(hostname, "kubectl rollout status deploy/cnpg-cloudnative-pg -n cnpg-system --timeout=300s",
                      check=False)
    if result.returncode != 0:
        die("the CloudNativePG operator did not become ready")
