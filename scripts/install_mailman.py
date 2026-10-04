#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs GNU Mailman 3, the mailing-list manager with its web interface.
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "mailman"
#   mailman_ns          : namespace (default mailman)
#   mailman_shorthn     : hostname prefix for the mailman-web ingress (default mailman)
#   mailman_version     : tag of the maxking/mailman-core and mailman-web images (default 0.5)
#   mailman_admin_user  : admin username (default admin)
#   mailman_admin_email : admin e-mail (default admin@lab.local)
#
# The addon deploys the official maxking/docker-mailman images, mailman-core and mailman-web, with a PostgreSQL database. The
# topology follows that project's docker-compose file, written as plain Kubernetes manifests. No official Helm chart exists for Mailman 3.
#
# The secrets (the HyperKitty API key, the Django SECRET_KEY and the PostgreSQL password) are created once, in the mailman-secrets
# Secret. A re-run keeps them, so a running deployment is not broken by rotation.
#
# Known limitation: Mailman's host-key allowlist does not handle changing pod addresses. After a pod is rescheduled, the REST connection
# between mailman-core and mailman-web can be refused, and a manual fix is needed.
#
# All data uses emptyDir volumes, so it is ephemeral and suited to a demonstration. For durable data, use PersistentVolumeClaims and a
# StorageClass, which a bare RKE2 cluster does not have by default.

__version__ = "5fc5f69"

PLUGIN = {
    "name": "mailman",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import secrets as _secrets
import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402

_TEMPLATES = ("namespace.yml.tmpl", "postgres.yml.tmpl", "core.yml.tmpl", "web.yml.tmpl")


def _validate(v):
    v.vns("mailman")
    v.vver("mailman")


def _get_or_create_secret_value(hostname, ns, key, length=32):
    """Idempotently fetch an existing mailman-secrets key, or generate a new random one."""
    existing = ssh_run(hostname,
                        "kubectl get secret mailman-secrets -n {} -o jsonpath='{{.data.{}}}' 2>/dev/null "
                        "| base64 -d".format(shlex.quote(ns), key),
                        check=False, capture=True)
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()
    return _secrets.token_urlsafe(length)


def setup_mailman(hostname, templ_addons_loc, cfg):
    """Deploy Mailman 3 (core + web + postgres)."""
    ns = cfg.get("mailman_ns") or "mailman"
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(shlex.quote(ns)), check=False)

    db_password = _get_or_create_secret_value(hostname, ns, "db_password")
    hyperkitty_api_key = _get_or_create_secret_value(hostname, ns, "hyperkitty_api_key")
    django_secret_key = _get_or_create_secret_value(hostname, ns, "django_secret_key", length=50)

    secret_cmd = (
        "kubectl create secret generic mailman-secrets "
        "--from-literal=db_password=$MAILMAN_DB_PASSWORD "
        "--from-literal=hyperkitty_api_key=$MAILMAN_HYPERKITTY_KEY "
        "--from-literal=django_secret_key=$MAILMAN_DJANGO_KEY "
        "--namespace {} --dry-run=client -o yaml | kubectl apply -f -"
    ).format(shlex.quote(ns))
    ssh_run(hostname, "MAILMAN_DB_PASSWORD={} MAILMAN_HYPERKITTY_KEY={} MAILMAN_DJANGO_KEY={} bash -c {}".format(
        shlex.quote(db_password), shlex.quote(hyperkitty_api_key), shlex.quote(django_secret_key),
        shlex.quote(secret_cmd)))

    render_cfg = dict(cfg)
    render_cfg["mailman_db_password"] = db_password

    for tmpl_name in _TEMPLATES:
        tmpl = "{}/mailman/{}".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, render_cfg))

    shorthn = cfg.get("mailman_shorthn") or "mailman"
    print("Mailman 3 deployed. Namespace: {}".format(ns))
    print("Web UI available at: http://{}.{}.{}".format(shorthn, cfg.get("clu_name"), cfg.get("mydomain")))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = dict(definition.get("mailman", {}) or {})
    cfg["clu_name"] = clu_name
    cfg["mydomain"] = clu_cfg.get("mydomain")

    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    setup_mailman(vm_name, templ_addons_loc, cfg)


if __name__ == "__main__":
    main()
