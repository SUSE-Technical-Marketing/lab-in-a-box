#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install example NV testing apps found in the documentation https://open-docs.neuvector.com/testing/testing
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "nv_testing" — NeuVector security testing workloads (nginx/node/redis pods)
#
#   nv_testing_ns       : [OPTIONAL] Kubernetes namespace                  (default: demo)
#   nv_testing_name     : [OPTIONAL] Service name and ingress hostname     (default: nv-testing)
#   nv_testing_attacker : [OPTIONAL] "1" to re-apply the attacker pod manifest a second
#                                     time (it's already in the main template list and
#                                     deploys unconditionally either way — see
#                                     setup_nv_testing()'s own docstring for why this flag
#                                     doesn't actually gate anything, a preserved bash oddity)

__version__ = "526bc48"

PLUGIN = {
    "name": "nv_testing",
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

_TEMPLATES = (
    "namespace.yml.tmpl", "redis_install.yml.tmpl", "nodejs_install.yml.tmpl",
    "nginx_install.yml.tmpl", "ingress.yml.tmpl", "attacker_install.yml.tmpl",
)


def setup_nv_testing(hostname, templ_addons_loc, cfg):
    """
    Deploy NeuVector security-testing workloads. Mirrors setup_nv_testing (bash).

    NOTE: attacker_install.yml.tmpl is in the main template list (_TEMPLATES)
    AND applied again below if nv_testing_attacker=="1" — so the attacker
    workload actually deploys unconditionally either way; the flag only
    controls a redundant second apply. Preserved exactly (kubectl apply is
    idempotent, so this is a harmless bash oddity, not something corrupting
    data — the flag not actually gating deployment looks unintentional, but
    there's no unambiguous "intended" behavior to substitute for it).
    """
    ns = cfg.get("nv_testing_ns") or "demo"
    ssh_run(hostname, "kubectl delete deployment -n {} nginx-pod node-pod redis-pod test".format(ns), check=False)

    for tmpl_name in _TEMPLATES:
        tmpl = "{}/nv_testing/{}".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    if str(cfg.get("nv_testing_attacker") or "") == "1":
        tmpl = "{}/nv_testing/attacker_install.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    print("NV testing app should be available in a few minutes")


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

    cfg = definition.get("nv_testing", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    name = cfg.get("nv_testing_name") or "nv-testing"

    for vm_name, node_cfg in definition.get("nodes", {}).items():
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        mydomain = clu_cfg.get("mydomain", "")

        print("# Using node: {}".format(vm_name))
        setup_nv_testing(vm_name, templ_addons_loc, cfg)

        dns_entry = "{}.{}".format(name, clu_name)
        add_service_dns(definition, clu_name, clu_cfg.get("clu_type", ""), dns_entry, mydomain)
        print("Service will be ready at {}.{}.{}".format(name, clu_name, mydomain))
        time.sleep(60)
        break


if __name__ == "__main__":
    main()
