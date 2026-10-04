#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install wordpress
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "wordpress" — WordPress + MySQL on Kubernetes
#
#   wordpress_ns      : [OPTIONAL] Kubernetes namespace                (default: wordpress)
#   wordpress_name    : [OPTIONAL] Deployment and service name         (default: wordpress)
#   wordpress_shorthn : [OPTIONAL] Short hostname for ingress          (default: wordpress)
#   wordpress_version : [OPTIONAL] Helm chart version                  (empty = latest)

__version__ = "a45abd4"

PLUGIN = {
    "name": "wordpress",
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
from lab_creation import ssh_run, process_template, add_service_dns  # noqa: E402

_TEMPLATES = ("kustomization.yaml", "mysql_install.yml", "wordpress_deployment.yaml")


def setup_wordpress(hostname, templ_addons_loc, wordpress_cfg):
    """Delete any pre-existing resources, then render+apply the wordpress manifests. Mirrors setup_wordpress (bash)."""
    # wordpress_ns/wordpress_name are interpolated unquoted into the remote
    # Validated here at runtime, because the --validate block in main() does not call the
    # validators. A value with a shell metacharacter would otherwise reach the remote shell unescaped.
    ns = ac.require_k8s_name(wordpress_cfg, "wordpress_ns", "wordpress")
    name = ac.require_k8s_name(wordpress_cfg, "wordpress_name", "wordpress")

    ssh_run(hostname,
            "kubectl delete -n {ns} Service/{n} PersistentVolumeClaim/mysql-pv-claim "
            "Deployment.app/{n} Service/{n}-mysql PersistentVolumeClaim/wp-pv-claim "
            "Deployment.app/{n}-mysql".format(ns=ns, n=name), check=False)

    for tmpl_name in _TEMPLATES:
        tmpl = "{}/wordpress/{}.tmpl".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        rendered = process_template(tmpl, wordpress_cfg)
        ssh_run(hostname, "kubectl apply -f -", input_text=rendered)

    print("wordpress should be available in a few minutes in: {}".format(
        wordpress_cfg.get("wordpress_shorthn") or "wordpress"))


def main():
    # bash's --validate block here defines the usual helpers but never calls
    # any of them — always exits 0.
    ac.handle_common_args(__file__, __version__, validate_fn=None, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    cfg = definition.get("wordpress", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    name = cfg.get("wordpress_name") or "wordpress"

    # NOTE: bash has NO break/exit and NO server-role filter here — it runs
    # setup_wordpress + add_service_dns on EVERY node in the definition, not
    # just the first/a server node. Same quirk as install_neuvector; matched
    # exactly rather than "fixed" to a single-node pattern.
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        print("# Using node: {}".format(vm_name))
        setup_wordpress(vm_name, templ_addons_loc, cfg)

        dns_entry = "{}.{}".format(name, clu_name)
        add_service_dns(definition, clu_name, clu_cfg.get("clu_type", ""), dns_entry, clu_cfg.get("mydomain", ""))
        print("Service will be ready at {}.{}.{}".format(name, clu_name, clu_cfg.get("mydomain", "")))
        time.sleep(60)


if __name__ == "__main__":
    main()
