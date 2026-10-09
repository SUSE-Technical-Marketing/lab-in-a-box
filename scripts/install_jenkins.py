#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install jenkins
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "jenkins" — Jenkins CI on Kubernetes via Helm
#
#   jenkins_rel      : [OPTIONAL] Helm repo alias                      (default: jenkins)
#   jenkins_repo_url : [OPTIONAL] Helm repo URL                        (default: https://charts.jenkins.io)
#   jenkins_version  : [OPTIONAL] Helm chart version                   (empty = latest)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "jenkins",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import helm_repo_add, ssh_run  # noqa: E402


def setup_jenkins_repo(hostname, jenkins_rel=None, jenkins_repo_url=None):
    """Add the Jenkins Helm repo. Mirrors setup_jenkins_repo (bash)."""
    helm_repo_add(hostname, jenkins_rel or "jenkins", jenkins_repo_url or "https://charts.jenkins.io")


def setup_jenkins(hostname, clu_name, mydomain):
    """
    Install Jenkins. Mirrors setup_jenkins (bash).

    NOTE: bash never calls setup_helm here (unlike almost every other addon) —
    this addon assumes Helm is already installed on the node by something
    else that ran first. Not fixed — matching current behavior exactly.
    """
    ssh_run(hostname, "kubectl create namespace jenkins")
    ssh_run(hostname, "helm upgrade -i jenkins jenkins/jenkins -n jenkins")
    ssh_run(hostname,
            'export jsonpath="{.data.jenkins-admin-password}"; '
            'export secret=$(kubectl get secret -n jenkins jenkins -o jsonpath=$jsonpath); '
            'echo "ADMIN PASSWORD: $(echo $secret | base64 --decode)"')
    ssh_run(hostname, "kubectl create -n jenkins ingress jenkins --rule=jenkins.{}.{}/=jenkins-server:443".format(
        clu_name, mydomain))


def main():
    # bash's --validate block here defines the usual helpers but never
    # actually calls any of them — always exits 0. validate_fn=None mirrors
    # that (addon_common exits 0 on --validate when no validate_fn is given).
    ac.handle_common_args(__file__, __version__, validate_fn=None, plugin=PLUGIN)

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

    # bash also does load_rancher_vars here — confirmed unused by setup_jenkins/
    # setup_jenkins_repo; not ported (same as install_longhorn/install_mariadb).
    setup_jenkins_repo(vm_name, jenkins_rel=cfg.get("jenkins_rel"), jenkins_repo_url=cfg.get("jenkins_repo_url"))
    setup_jenkins(vm_name, clu_name, clu_cfg.get("mydomain", ""))
    time.sleep(60)


if __name__ == "__main__":
    main()
