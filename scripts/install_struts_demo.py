#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install strus demo apps
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "struts_demo" — Apache Struts2 vulnerable demo application (CVE-2017-5638)
#
#   struts_demo_ns       : [OPTIONAL] Kubernetes namespace                  (default: struts)
#   struts_demo_name     : [OPTIONAL] Deployment, service and ingress name  (default: struts)
#   struts_demo_attacker : [OPTIONAL] "1" to also deploy an attacker container/pod
#                                      demonstrating exploitation of this app's CVE
#                                      (default: not deployed)

__version__ = "a45abd4"

PLUGIN = {
    "name": "struts_demo",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import re
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, ssh_output, process_template, add_service_dns  # noqa: E402


def setup_struts_demo(hostname, templ_addons_loc, cfg):
    """
    Deploy the Struts2 CVE-2017-5638 demo app, and optionally an attacker pod.
    Mirrors setup_struts_demo (bash).
    """
    # struts_demo_ns/struts_demo_name are interpolated unquoted into remote
    # kubectl commands below (and this script has no _validate() at all —
    # found in code review 2026-09-05), so validate them here at runtime
    # instead: a value with a shell metacharacter would otherwise reach a
    # real remote shell unescaped.
    ns = ac.require_k8s_name(cfg, "struts_demo_ns", "struts")
    name = ac.require_k8s_name(cfg, "struts_demo_name", "struts")

    ssh_run(hostname,
            "kubectl delete -n {ns} service/{n} deployment.apps/{n} deployment.apps/attacker "
            "ingress/{n}".format(ns=ns, n=name), check=False)

    for tmpl_name in ("namespace.yml.tmpl", "install.yml.tmpl", "service.yml.tmpl", "ingress.yml.tmpl"):
        tmpl = "{}/struts_demo/{}".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    if str(cfg.get("struts_demo_attacker") or "") == "1":
        attacker_tmpl = "{}/struts_demo/attacker_install.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
        # bash applies this same manifest twice (once here, once again below,
        # with an `apk add` in between) — preserved verbatim; kubectl apply
        # is idempotent so the second apply is a harmless no-op either way.
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(attacker_tmpl, cfg))
        time.sleep(60)

        pods = ssh_output(hostname, "kubectl get pods -n {}".format(ns))
        m = re.search(r"{}-[a-z0-9]*-[a-zA-Z0-9]*".format(re.escape(name)), pods)
        pod_name = m.group(0) if m else ""
        ssh_run(hostname, "kubectl exec -n {} -i {} -- apk add --update --no-cache python3".format(ns, pod_name))

        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(attacker_tmpl, cfg))

    print("Struts demo app ( https://github.com/skywalke34/struts-demo ) should be available in a few seconds")


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

    cfg = definition.get("struts_demo", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    name = cfg.get("struts_demo_name") or "struts"

    # Same pattern as install_insecure_app: bash's loop has an `exit 1`
    # inside it, so it only ever processes the first node.
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        mydomain = clu_cfg.get("mydomain", "")

        print("# Using node: {}".format(vm_name))
        setup_struts_demo(vm_name, templ_addons_loc, cfg)

        dns_entry = "{}.{}".format(name, clu_name)
        add_service_dns(definition, clu_name, clu_cfg.get("clu_type", ""), dns_entry, mydomain)
        print("Service will be ready at http://{}.{}.{}/super-app".format(name, clu_name, mydomain))
        time.sleep(60)
        break


if __name__ == "__main__":
    main()
