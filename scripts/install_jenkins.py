#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install jenkins
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.1
#
# JSON section: "jenkins" — Jenkins CI on Kubernetes via Helm
#
#   jenkins_rel      : [OPTIONAL] Helm repo alias                      (default: jenkins)
#   jenkins_repo_url : [OPTIONAL] Helm repo URL                        (default: https://charts.jenkins.io)
#   jenkins_version  : [OPTIONAL] Helm chart version                   (empty = latest)
#   jenkins_ns       : [OPTIONAL] namespace                            (default: jenkins)
#   jenkins_shorthn  : [OPTIONAL] hostname prefix of the web UI        (default: jenkins)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "jenkins",
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
    v.vns("jenkins")
    v.vver("jenkins")
    v.vurl("jenkins")


def setup_jenkins_repo(hostname, jenkins_rel=None, jenkins_repo_url=None):
    """Add the Jenkins Helm repo."""
    helm_repo_add(hostname, jenkins_rel or "jenkins", jenkins_repo_url or "https://charts.jenkins.io")


def setup_jenkins(hostname: str, clu_name: str, mydomain: str, cfg: dict) -> str:
    """Install Jenkins from <jenkins_rel>/jenkins with the chart's own ingress; returns the web UI hostname."""
    rel = cfg.get("jenkins_rel") or "jenkins"
    ns = cfg.get("jenkins_ns") or "jenkins"
    ver_arg = " --version {}".format(shlex.quote(cfg["jenkins_version"])) if cfg.get("jenkins_version") else ""
    fqdn = "{}.{}.{}".format(cfg.get("jenkins_shorthn") or "jenkins", clu_name, mydomain)
    ssh_run(hostname, "helm upgrade --install jenkins {}/jenkins --namespace {} --create-namespace{} --wait --timeout 10m "
                      "--set controller.ingress.enabled=true --set controller.ingress.hostName={} "
                      "--set controller.jenkinsUrl=http://{}/".format(
                          shlex.quote(rel), shlex.quote(ns), ver_arg, fqdn, fqdn))
    print("Jenkins installed. Namespace: {}".format(ns))
    print("UI available at: http://{}  (user: admin)".format(fqdn))
    print("Admin password: kubectl -n {} get secret jenkins -o jsonpath='{{.data.jenkins-admin-password}}' | base64 -d".format(ns))
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
    cfg = definition.get("jenkins", {}) or {}

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    setup_jenkins_repo(vm_name, jenkins_rel=cfg.get("jenkins_rel"), jenkins_repo_url=cfg.get("jenkins_repo_url"))
    fqdn = setup_jenkins(vm_name, clu_name, clu_cfg.get("mydomain", ""), cfg)
    ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
