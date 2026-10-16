"""
k8s.py — Kubernetes cluster setup and addon execution helpers.

Python equivalent of k8s_functions.bash.

Typical usage:
    from k8s import (
        list_kclusters, get_vm_kcluster, load_kclu_vars,
        add_kclu_dns,
        first_server_node, addon_nodes,
        setup_traefik_rke2, setup_traefik_k3s,
        create_basic_auth_secret, set_longhorn_overprovisioning,
        setup_cnpg_operator,
    )
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import re
import shlex
import subprocess
import textwrap
import time
from pathlib import Path

from lab_creation import (
    die, log, warn, _RED, _RESET,
    ssh_run, ssh_output,
    prepare_local_as_kubeclient, reboot_into_pending_snapshot, wait_for_transactional_update_lock, is_transactional,
    wait_for_ssh_command, update_kernel,
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
# The RKE2 node config is written by RKE2Distro.write_node_config().

class K8sDistro(object):
    """Interface a Kubernetes distribution implements."""

    name = None

    ready_timeout = 1200

    def install_cluster(self, definition, clu_name, clu_ctx):
        """Install the cluster on its nodes: install_server() or install_agent() on each, in the lab definition's
        order, by KUBERNETES_NODE_TYPE (or the deprecated INSTALL_RKE2_TYPE), default server. Then wait until the
        first server reports every node Ready; raises RuntimeError when that takes longer than ready_timeout seconds."""
        token = None
        rancher1_ip = None
        node_count = 0
        for vm_name, node_cfg in (definition.get("nodes") or {}).items():
            if node_cfg.get("kcluster") != clu_name:
                continue
            node_count += 1
            node_type = node_cfg.get("KUBERNETES_NODE_TYPE") or node_cfg.get("INSTALL_RKE2_TYPE", "server")
            if node_type == "agent":
                token, rancher1_ip = self.install_agent(vm_name, clu_name, clu_ctx, token, rancher1_ip)
            else:
                token, rancher1_ip = self.install_server(vm_name, clu_name, clu_ctx, token=token,
                                                         rancher1_ip=rancher1_ip)
        if rancher1_ip:
            self.wait_nodes_ready(rancher1_ip, clu_name, node_count)

    def wait_nodes_ready(self, server, clu_name, count):
        """Wait until kubectl on `server` lists at least `count` Ready nodes; raises RuntimeError after ready_timeout
        seconds."""
        log("  Waiting for the {} node(s) of '{}' to be Ready".format(count, clu_name))
        check = "[ \"$({} get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/' | wc -l)\" -ge {} ]".format(
            self.kubectl_command(), count)
        if not wait_for_ssh_command(server, check, timeout=self.ready_timeout, interval=15):
            raise RuntimeError("the nodes of '{}' were not all Ready after {}s; check the {} service on '{}'".format(
                clu_name, self.ready_timeout, self.name, server))

    def kubectl_command(self):
        """Shell command that runs kubectl with the cluster's admin kubeconfig on a server node."""
        raise NotImplementedError

    @staticmethod
    def _before_installer(hostname):
        """Run before a vendor installer: its transaction starts from the running snapshot, so a pending one is booted
        first, and it fails while another transactional-update holds the lock. No-op outside transactional-update."""
        reboot_into_pending_snapshot(hostname)
        wait_for_transactional_update_lock(hostname)

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

    def kubectl_command(self):
        return "PATH=$PATH:/usr/local/bin:/opt/bin k3s kubectl"

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
        and install_agent both use this path, and a token makes the node join.

        Returns (token, rancher1_ip) — pass these to subsequent node calls.
        """
        clu_rel = clu_ctx.get("clu_rel") or "stable"
        mydomain = clu_ctx.get("mydomain", "")

        prepare_local_as_kubeclient()
        ssh_run(hostname, "mkdir -p /etc/rancher/k3s")
        self._before_installer(hostname)

        if token is None:
            log("  Installing K3s server on '{}' (first node of '{}')".format(hostname, clu_name))
            # clu_rel, clu_name, mydomain and clu_token are free text from the lab JSON. They are shell-quoted before they reach
            # the remote command. K3S_TOKEN sets the cluster token when clu_token is given.
            clu_token = clu_ctx.get("clu_token") or ""
            ssh_run(
                hostname,
                "curl -sfL https://get.k3s.io | "
                "INSTALL_K3S_CHANNEL={} {}"
                "sh -s - server --tls-san {}.{}".format(
                    shlex.quote(clu_rel), "K3S_TOKEN={} ".format(shlex.quote(clu_token)) if clu_token else "",
                    shlex.quote(clu_name), shlex.quote(mydomain))
            )
            self._start(hostname, "k3s")
            ssh_run(hostname, "echo 'export KUBECONFIG={}' > /etc/profile.d/k3s.sh; chmod 0644 /etc/profile.d/k3s.sh".format(
                self.kubeconfig_path()))
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
            self._start(hostname, "k3s-agent")
            return token, rancher1_ip

    def _start(self, hostname, service):
        """
        Start `service` after the K3s installer has run. On a transactional-update system the installer puts k3s-selinux in a
        new snapshot, does not start K3s and asks for a reboot, so the node first reboots into that snapshot. Dies when SELinux is
        enabled and the k3s SELinux policy is still missing.
        """
        reboot_into_pending_snapshot(hostname)
        missing_policy = ssh_run(hostname, "selinuxenabled && [ ! -f /usr/share/selinux/packages/k3s.pp ]",
                                 check=False, capture=True)
        if missing_policy.returncode == 0:
            die("'{}': SELinux is enabled but the k3s-selinux policy is not installed".format(hostname))
        ssh_run(hostname, "systemctl enable --now {}".format(service))


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

    def kubectl_command(self):
        return "/var/lib/rancher/rke2/bin/kubectl --kubeconfig {}".format(self.kubeconfig_path())

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
        # The tarball method installs no rke2-selinux policy. On transactional-update systems the RPM method installs it,
        # through transactional-update, and needs a reboot. Under that policy the SL Micro 6.1 GM kernel (6.4.0-19) denies
        # RKE2 its MPTCP connections and 6.4.0-52 does not, so the kernel is updated first.
        transactional = is_transactional(hostname)
        if not install_method and transactional:
            install_method = "rpm"

        prepare_local_as_kubeclient()

        log("  Configuring host for RKE2 on '{}'".format(hostname))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/NetworkManager/conf.d/rke2-canal.conf; chmod 0420 /etc/NetworkManager/conf.d/rke2-canal.conf".format(_RKE2_NM_CONF))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/sysctl.d/90-rke2.conf; chmod 0420 /etc/sysctl.d/90-rke2.conf".format(_RKE2_SYSCTL))
        ssh_run(hostname, "echo '{}' | base64 -d > /etc/profile.d/rke2.sh; chmod 0420 /etc/profile.d/rke2.sh".format(_RKE2_PATH))
        ssh_run(hostname, "mkdir -p /var/lib/rancher/{0} /etc/rancher/{0}".format(clu_type))

        self._before_installer(hostname)
        if install_method == "rpm" and transactional:
            log("  Updating the kernel on '{}'".format(hostname))
            update_kernel(hostname)
            reboot_into_pending_snapshot(hostname)
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
        reboot_into_pending_snapshot(hostname)
        ssh_run(hostname, "if selinuxenabled && rpm -q --quiet {0}-selinux; then echo 'selinux: true' >> "
                          "/etc/rancher/{0}/config.yaml; fi".format(clu_type))

        if token is None:
            log("  Starting RKE2 server on '{}' (first node of '{}')".format(hostname, clu_name))
            if clu_ctx.get("clu_token"):
                ssh_run(hostname, "echo {} >> /etc/rancher/{}/config.yaml".format(
                    shlex.quote("token: {}".format(clu_ctx["clu_token"])), clu_type))
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


class KubeadmDistro(K8sDistro):
    """
    Upstream Kubernetes, installed with kubeadm from the community packages at pkgs.k8s.io. Unlike RKE2 and K3s
    there is no vendor installer script: the node gets the kubelet, kubeadm and kubectl packages of the requested
    minor, containerd as its runtime, and then kubeadm builds the cluster.

    clu_rel is the Kubernetes minor to install ("1.33", or "v1.33.4", whose minor is used). clu_cni picks the
    network plugin the first server applies: flannel (the default), calico, or none to apply nothing, for a lab that
    brings its own. clu_pod_cidr overrides the pod network, which each plugin's default manifest expects to match.
    """

    name = "kubeadm"

    CNI_MANIFESTS = {
        "flannel": "https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml",
        "calico": "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml",
    }
    # Each plugin's own manifest carries this network, so the cluster has to be initialised with the same one.
    CNI_POD_CIDRS = {"flannel": "10.244.0.0/16", "calico": "192.168.0.0/16"}
    DEFAULT_CNI = "flannel"

    def kubeconfig_path(self):
        return "/etc/kubernetes/admin.conf"

    def token_path(self):
        """kubeadm keeps no join token on disk: a token is created when a node joins and expires by itself."""
        return ""

    def kubectl_command(self):
        return "kubectl --kubeconfig {}".format(self.kubeconfig_path())

    @staticmethod
    def minor(clu_rel):
        """The repository minor for a clu_rel: "1.33", "v1.33" and "v1.33.4+ignored" all give "v1.33"."""
        match = re.search(r"(\d+)\.(\d+)", str(clu_rel or ""))
        if not match:
            die("clu_type kubeadm needs clu_rel to name a Kubernetes minor, for example \"1.33\" — got "
                "{!r}".format(clu_rel))
        return "v{}.{}".format(match.group(1), match.group(2))

    def cni(self, clu_ctx):
        """The network plugin to apply, validated against what this distro knows."""
        name = str(clu_ctx.get("clu_cni") or self.DEFAULT_CNI).strip().lower()
        if name not in list(self.CNI_MANIFESTS) + ["none"]:
            die("clu_cni '{}' is invalid — use one of: {}, none".format(name, ", ".join(sorted(self.CNI_MANIFESTS))))
        return name

    def pod_cidr(self, clu_ctx):
        """The pod network: the lab's own clu_pod_cidr, else the one the chosen plugin's manifest expects."""
        return clu_ctx.get("clu_pod_cidr") or self.CNI_POD_CIDRS.get(self.cni(clu_ctx), "10.244.0.0/16")

    def prepare_node(self, hostname, clu_rel):
        """
        Make a node able to run the kubelet: swap off, the modules and sysctls Kubernetes requires, containerd with
        the systemd cgroup driver, and the kubelet, kubeadm and kubectl packages of this cluster's minor.

        The package manager is detected rather than derived from the OS name, as ensure_podman_available() does. On a
        transactional system the packages land in a new snapshot, so the node reboots into it before kubeadm runs.
        """
        minor = self.minor(clu_rel)
        ssh_run(hostname, "swapoff -a; sed -i '/\\sswap\\s/s/^/#/' /etc/fstab", check=False)
        ssh_run(hostname, "printf 'overlay\\nbr_netfilter\\n' > /etc/modules-load.d/k8s.conf; "
                          "modprobe overlay; modprobe br_netfilter")
        ssh_run(hostname, "printf 'net.bridge.bridge-nf-call-iptables=1\\nnet.ipv4.ip_forward=1\\n"
                          "net.bridge.bridge-nf-call-ip6tables=1\\n' > /etc/sysctl.d/99-k8s.conf; sysctl --system "
                          ">/dev/null")

        transactional = is_transactional(hostname)
        prefix = "transactional-update -n pkg install -y" if transactional else None
        if prefix is None:
            for manager, install in (("zypper", "zypper --non-interactive install -y"),
                                     ("dnf", "dnf install -y"), ("apt-get", "apt-get install -y")):
                if ssh_run(hostname, "command -v {}".format(manager), check=False).returncode == 0:
                    prefix = install
                    break
        if prefix is None:
            die("'{}': none of zypper, dnf or apt-get is available, so the Kubernetes packages cannot be "
                "installed".format(hostname))

        # The repository and its key are set up before anything is installed: zypper skips a repository whose key
        # it does not trust and then exits 106 for every later install, which looks like a failure of whatever was
        # being installed at the time.
        if "apt-get" in prefix:
            ssh_run(hostname, "apt-get update -qq && apt-get install -y apt-transport-https ca-certificates "
                              "curl gpg && mkdir -p /etc/apt/keyrings")
            ssh_run(hostname, "curl -fsSL https://pkgs.k8s.io/core:/stable:/{m}/deb/Release.key | "
                              "gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg".format(m=minor))
            ssh_run(hostname, "echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] "
                              "https://pkgs.k8s.io/core:/stable:/{m}/deb/ /' > /etc/apt/sources.list.d/"
                              "kubernetes.list && apt-get update -qq".format(m=minor))
        else:
            repo_url = "https://pkgs.k8s.io/core:/stable:/{}/rpm/".format(minor)
            repo = ("[kubernetes]\\nname=Kubernetes\\nbaseurl={url}\\nenabled=1\\nautorefresh=1\\n"
                    "type=rpm-md\\ngpgcheck=1\\ngpgkey={url}repodata/repomd.xml.key\\n").format(url=repo_url)
            ssh_run(hostname, "curl -fsSL {}repodata/repomd.xml.key -o /tmp/kubernetes-release.key && "
                              "rpm --import /tmp/kubernetes-release.key && "
                              "rm -f /tmp/kubernetes-release.key".format(repo_url))
            ssh_run(hostname, "printf '{repo}' > /etc/zypp/repos.d/kubernetes.repo 2>/dev/null || "
                              "printf '{repo}' > /etc/yum.repos.d/kubernetes.repo".format(repo=repo))

        self._install_packages(hostname, prefix, "containerd")
        ssh_run(hostname, "mkdir -p /etc/containerd && containerd config default > /etc/containerd/config.toml && "
                          "sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml")
        self._install_packages(hostname, prefix, "kubelet kubeadm kubectl")
        missing = ssh_run(hostname, "for b in kubeadm kubelet kubectl; do command -v $b >/dev/null || echo $b; done",
                          check=False, capture=True)
        if (missing.stdout or "").strip():
            die("'{}': {} missing after the install — the Kubernetes {} repository at pkgs.k8s.io was not usable on "
                "this node".format(hostname, ", ".join((missing.stdout or "").split()), minor))

        if transactional:
            reboot_into_pending_snapshot(hostname)
        ssh_run(hostname, "systemctl enable --now containerd && systemctl enable kubelet")

    @staticmethod
    def _install_packages(hostname, prefix, packages):
        """
        Install `packages` with the node's package manager. zypper exits 106 when it had to skip a repository, which
        says nothing about whether the packages went in, so that code is not a failure here; what the packages were
        for is checked afterwards instead.
        """
        result = ssh_run(hostname, "{} {}".format(prefix, packages), check=False)
        if result.returncode not in (0, 106):
            die("'{}': installing {} failed (exit {})".format(hostname, packages, result.returncode))

    def install_server(self, hostname, clu_name, clu_ctx, token=None, rancher1_ip=None):
        self.prepare_node(hostname, clu_ctx.get("clu_rel"))
        if token is None:
            return self._init(hostname, clu_name, clu_ctx)
        log("  Joining '{}' as a control-plane node of '{}'".format(hostname, clu_name))
        ssh_run(hostname, "{} --control-plane --certificate-key {}".format(
            token["join"], shlex.quote(token["certificate_key"])))
        self._write_root_kubeconfig(hostname)
        return token, rancher1_ip

    def install_agent(self, hostname, clu_name, clu_ctx, token, rancher1_ip):
        self.prepare_node(hostname, clu_ctx.get("clu_rel"))
        log("  Joining '{}' as a worker of '{}'".format(hostname, clu_name))
        ssh_run(hostname, token["join"])
        return token, rancher1_ip

    def _init(self, hostname, clu_name, clu_ctx):
        """
        Build the cluster on its first node and return (join, hostname), where join carries the command another node
        runs to join and the certificate key a further control-plane node needs.
        """
        mydomain = clu_ctx.get("mydomain", "")
        endpoint = "{}.{}".format(clu_name, mydomain) if mydomain else hostname
        log("  Initialising Kubernetes {} on '{}' (first node of '{}')".format(
            self.minor(clu_ctx.get("clu_rel")), hostname, clu_name))
        ssh_run(hostname, "kubeadm init --pod-network-cidr {} --control-plane-endpoint {}:6443 "
                          "--upload-certs --ignore-preflight-errors=NumCPU".format(
                              shlex.quote(self.pod_cidr(clu_ctx)), shlex.quote(endpoint)))
        self._write_root_kubeconfig(hostname)
        ssh_run(hostname, "echo 'export KUBECONFIG={}' > /etc/profile.d/kubeadm.sh; "
                          "chmod 0644 /etc/profile.d/kubeadm.sh".format(self.kubeconfig_path()))

        cni = self.cni(clu_ctx)
        if cni == "none":
            log("  clu_cni is \"none\": no network plugin applied, so the nodes stay NotReady until one is")
        else:
            log("  Applying the {} network plugin".format(cni))
            ssh_run(hostname, "{} apply -f {}".format(self.kubectl_command(),
                                                      shlex.quote(self.CNI_MANIFESTS[cni])))

        # A single-node cluster has nowhere else to put pods, so the control-plane taint comes off.
        if self._node_count(clu_ctx) == 1:
            ssh_run(hostname, "{} taint nodes --all node-role.kubernetes.io/control-plane- ".format(
                self.kubectl_command()), check=False)

        join = ssh_output(hostname, "kubeadm token create --print-join-command").strip()
        certificate_key = ssh_output(
            hostname, "kubeadm init phase upload-certs --upload-certs 2>/dev/null | tail -1").strip()
        return {"join": join, "certificate_key": certificate_key}, hostname

    def install_cluster(self, definition, clu_name, clu_ctx):
        """
        As the base does, with the node count recorded first: a single-node cluster has nowhere but its control
        plane to run pods, so _init() takes the control-plane taint off, and it can only know that in advance.
        """
        clu_ctx = dict(clu_ctx, _node_count=sum(
            1 for cfg in (definition.get("nodes") or {}).values() if cfg.get("kcluster") == clu_name))
        super(KubeadmDistro, self).install_cluster(definition, clu_name, clu_ctx)

    @staticmethod
    def _node_count(clu_ctx):
        """How many nodes this kcluster has, as install_cluster() recorded; 0 when it is not known."""
        return int(clu_ctx.get("_node_count") or 0)

    def _write_root_kubeconfig(self, hostname):
        """Put the admin kubeconfig where a plain kubectl finds it, as the kubeadm documentation does."""
        ssh_run(hostname, "mkdir -p /root/.kube && cp -f {} /root/.kube/config && "
                          "chmod 0600 /root/.kube/config".format(self.kubeconfig_path()))


class HarvesterDistro(K8sDistro):
    """
    Harvester / SUSE Virtualization (see libs/harvester.py). Nothing is installed over SSH: the cluster forms at first
    boot from each node's user-data (written by setup_vm.py). install_cluster() waits until the create node answers on
    HTTPS, fetches the kubeconfig to harvester.kubeconfig_path(), waits until every node is Ready and applies
    harvester_settings.
    """
    name = "harvester"

    def kubeconfig_path(self):
        return "/etc/rancher/rke2/rke2.yaml"

    def token_path(self):
        return "/var/lib/rancher/rke2/server/node-token"

    def install_cluster(self, definition, clu_name, clu_ctx):
        import socket
        import harvester
        nodes = harvester.cluster_nodes(definition, clu_name)
        first = nodes[0]
        address = socket.gethostbyname(first)
        log("Waiting for Harvester on \"{}{}{}\" ({}) to answer on HTTPS".format(_RED, first, _RESET, address))
        harvester.wait_https(address)
        # The API server certificate covers "localhost", not the node's public address.
        kubeconfig = harvester.fetch_kubeconfig(address, harvester.kubeconfig_path(clu_name),
                                                "https://{}:6443".format(address), tls_server_name="localhost")
        log("Waiting for {} Harvester node(s) to be Ready".format(len(nodes)))
        harvester.wait_nodes_ready(kubeconfig, len(nodes))
        harvester.apply_settings(kubeconfig, harvester.cluster_cfg(definition, clu_name).get("harvester_settings"))
        log("Harvester cluster \"{}{}{}\": https://{}/ (kubeconfig {})".format(
            _RED, clu_name, _RESET, address, kubeconfig))


DISTROS = {
    "harvester": HarvesterDistro,
    "k3s": K3sDistro,
    "kubeadm": KubeadmDistro,
    "rke2": RKE2Distro,
}


def get_distro(clu_type):
    """Return a K8sDistro instance for clu_type, or die() naming supported distros."""
    cls = DISTROS.get(clu_type)
    if cls is None:
        die("Unknown Kubernetes distribution '{}' — supported: {}".format(
            clu_type, ", ".join(sorted(DISTROS))))
    return cls()


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
