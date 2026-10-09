#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Insecure_app
# Author/s: Raul Mahiques
# License: GPLv3
#
# Schema version: 1.0
#
# JSON section: "insecure_app" — intentionally vulnerable web application (demo/training)
#
#   insecure_app_ns         : [OPTIONAL] Kubernetes namespace          (default: insecure-apps)
#   insecure_app_name       : [OPTIONAL] App name and ingress hostname (default: webphotobook)
#   insecure_app_long_name  : [OPTIONAL] App display name             (default: My Buggy App)
#   insecure_app_admin_pwd  : [OPTIONAL] Basic auth password          (default: admin123)
#   insecure_app_port       : [OPTIONAL] App listening port           (default: 5000)
#   insecure_app_DB_TYPE    : [OPTIONAL] Database type                (default: sqlite3)
#   insecure_app_DB_HOST    : [OPTIONAL] Database host                (default: mysql)
#   insecure_app_DB_USER    : [OPTIONAL] Database user                (default: root)
#   insecure_app_DB_PWD     : [OPTIONAL] Database password            (default: password_change_me)
#   insecure_app_DBNAME     : [OPTIONAL] Database name                (default: photos)
#   insecure_app_SECRET_KEY : [OPTIONAL] App secret key               (default: abcde1234)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "insecure_app",
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


def setup_insecure_app(hostname, templ_addons_loc, cfg, clu_name, mydomain, mariadb_name=None):
    """
    Deploy the intentionally-vulnerable demo app, and (if configured for
    mysql) initialise its database and push runtime settings via its own
    HTTP settings API. Mirrors setup_insecure_app (bash).
    """
    ns = cfg.get("insecure_app_ns") or "insecure-apps"
    name = cfg.get("insecure_app_name") or "webphotobook"

    ssh_run(hostname, "kubectl delete -n {} deployment.app/{}".format(ns, name), check=False)

    for tmpl_name in ("install.yml", "service.yml", "ingress.yml"):
        tmpl = "{}/insecure_app/{}.tmpl".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    if (cfg.get("insecure_app_DB_TYPE") or "sqlite3") == "mysql":
        tmpl = "{}/insecure_app/myconf.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
        ssh_run(hostname, "cat - > /tmp/myconf_temp.yml", input_text=process_template(tmpl, cfg))

        # need some time until the pod is up
        time.sleep(100)
        pods = ssh_output(hostname, "kubectl get pods -n {}".format(ns))
        # NOTE: bash used the invalid bracket range [a-Z0-9] here (grep would
        # reject it outright on most systems) — fixed to [a-zA-Z0-9], same as
        # the bash-side fix.
        m = re.search(r"{}-[a-z0-9]*-[a-zA-Z0-9]*".format(re.escape(name)), pods)
        pod_name = m.group(0) if m else ""
        print("pod name: {}".format(pod_name))

        ssh_run(hostname, "kubectl cp /tmp/myconf_temp.yml {}/{}:myconf_temp.yml".format(ns, pod_name))
        ssh_run(hostname, "kubectl exec -n {} -i {} -- ping -c 1 {}".format(
            ns, pod_name, mariadb_name or "mariadb"))
        print("## Prepare database")
        ssh_run(hostname, "kubectl exec -n {} -i {} -- env CONFIG_FILE=myconf_temp.yml /usr/bin/python3 init_db.py".format(
            ns, pod_name))
        ssh_run(hostname, "kubectl exec -n {} -i {} -- cp myconf_temp.yml myconf.yml".format(ns, pod_name))

        params = [
            "APP_NAME={}".format(cfg.get("insecure_app_long_name") or "My Buggy App"),
            "BASIC_AUTH_PASSWORD={}".format(cfg.get("insecure_app_admin_pwd") or "admin123"),
            "DBNAME={}".format(cfg.get("insecure_app_DBNAME") or "photos"),
            "DB_FILE={}".format(cfg.get("insecure_app_DB_FILE") or "database.db"),
            "DB_TABLE_NAME={}".format(cfg.get("insecure_app_DB_TABLE_NAME") or "photos"),
            "DB_TYPE={}".format(cfg.get("insecure_app_DB_TYPE") or "sqlite3"),
            "DB_HOST={}".format(cfg.get("insecure_app_DB_HOST") or "mysql"),
            "DB_USER={}".format(cfg.get("insecure_app_DB_USER") or "root"),
            "DB_PWD={}".format(cfg.get("insecure_app_DB_PWD") or "password_change_me"),
            "IMAGES_LOCATION={}".format(cfg.get("insecure_app_IMAGES_LOCATION") or "images/"),
            "PORT={}".format(cfg.get("insecure_app_port") or "5000"),
            "SECRET_KEY={}".format(cfg.get("insecure_app_SECRET_KEY") or "abcde1234"),
        ]
        fqdn = "{}.{}.{}".format(name, clu_name, mydomain)
        for param in params:
            print("     Setting {}".format(param.split("=", 1)[0]))
            ssh_run(hostname, "curl http://{}/api/settings -d '{}' -s >/dev/null || echo failed".format(
                fqdn, param), check=False)

    print("Insecure app should be available in a few minutes")


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

    cfg = definition.get("insecure_app", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    mariadb_name = (definition.get("mariadb", {}) or {}).get("mariadb_name")
    name = cfg.get("insecure_app_name") or "webphotobook"

    # NOTE: same no-break, no-server-filter pattern as install_neuvector/
    # install_wordpress — this loop has an `exit 1` inside it, so bash DOES
    # stop after the first node (unlike wordpress). Matched with a plain
    # break after the first iteration.
    nodes = definition.get("nodes", {})
    for vm_name, node_cfg in nodes.items():
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
        mydomain = clu_cfg.get("mydomain", "")

        print("# Using node: {}".format(vm_name))
        setup_insecure_app(vm_name, templ_addons_loc, cfg, clu_name, mydomain, mariadb_name=mariadb_name)

        dns_entry = "{}.{}".format(name, clu_name)
        add_service_dns(definition, clu_name, clu_cfg.get("clu_type", ""), dns_entry, mydomain)
        print("Service will be ready at {}.{}.{}".format(name, clu_name, mydomain))
        time.sleep(60)
        break


if __name__ == "__main__":
    main()
