#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a standalone Grafana server as a
# podman container directly on the target host (no Kubernetes involved) —
# built 2026-09-16 alongside install_prometheus.py, to give the "prometheus"
# addon's own metrics somewhere to actually be graphed/dashboarded.
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://grafana.com/docs/grafana/latest/administration/provisioning/
#            https://grafana.com/grafana/dashboards/10277-uyuni-suse-manager-server/
#            (real community dashboard for exactly the exporter set install_prometheus.py's
#            own _smlm_scrape_job() targets — confirmed live 2026-09-16: id 10277, uid
#            "2p2qPSUik", revision 2 at time of writing, 21 panels. Its raw JSON (as downloaded
#            from grafana.com's own API, NOT the Web UI "import" flow) carries two unfilled
#            template placeholders — ${DS_PROMETHEUS} for the datasource name and ${VAR_JOB} for
#            the Prometheus job label — normally prompted for interactively on import; this addon
#            substitutes them for unattended provisioning instead, matching the datasource name it
#            itself provisions and the job name install_prometheus.py's own scrape config uses.)
#
# ─── JSON section: "grafana" ─────────────────────────────────────────────────
#
# OPTIONAL
#   grafana_version            : container image tag                (default: "latest")
#   grafana_image              : full image reference                (default:
#                                 "docker.io/grafana/grafana" — the standard upstream image;
#                                 there is no SUSE-branded Grafana image, same reasoning as
#                                 install_prometheus.py's own grafana_image-equivalent field)
#   grafana_port               : port Grafana listens on directly (host networking, same
#                                 reasoning as install_prometheus.py's own --network host fix —
#                                 see setup_grafana()'s own comment)    (default: "3000")
#   grafana_admin_user         : initial admin username                (default: "admin")
#   grafana_admin_password     : initial admin password                (default: "admin" — CHANGE
#                                 THIS; Grafana forces a change on first login only if this is
#                                 left at the literal default "admin")
#   grafana_prometheus_url     : URL of the Prometheus this Grafana should query — e.g.
#                                 "http://sol.mydemo.lab:9090" (the "prometheus" addon's own
#                                 default port). REQUIRED if grafana_import_smlm_dashboard or
#                                 grafana_dashboards is set (a dashboard needs a datasource to
#                                 query); provisions one Prometheus datasource named "Prometheus".
#   grafana_import_smlm_dashboard : "true" to auto-provision the real Grafana Labs community
#                                 dashboard (id 10277) for an smlm/uyuni server's own bundled
#                                 exporters (see this file's own top-of-file note for exactly
#                                 what it visualizes). REQUIRES grafana_smlm_job_name.
#   grafana_smlm_job_name      : the Prometheus scrape job name to bind dashboard 10277's own
#                                 "$job" template variable to — MUST match the job_name
#                                 install_prometheus.py's own scrape config uses for that server
#                                 (its auto-generated smlm job is literally "smlm-<hostname>",
#                                 e.g. "smlm-sol.mydemo.lab" — see install_prometheus.py's own
#                                 _smlm_scrape_job()).
#   grafana_dashboards         : [{"id": 12345, "name": "my-dashboard"}, ...]   # extra
#                                 Grafana Labs community dashboards to auto-provision the same
#                                 way, by numeric id — fetched at their latest revision, no
#                                 placeholder substitution attempted (that's specific to 10277's
#                                 own two variables) beyond pointing every panel at the
#                                 "Prometheus" datasource this addon provisions.
#
# Target node(s): any node listing "grafana" in its own addons[] list — same dispatch shape as
# install_prometheus.py. Reachable remotely on grafana_port — the NODE ITSELF needs that port
# open (this addon does not manage firewalls/security groups; see the node's own aws_open_ports).

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "grafana",
    "targets": ["vm", "baremetal"],
    "layers": ["standalone-container"],
    "aux_services": [],
}

import os
import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, die  # noqa: E402


def _validate(v):
    v.vver("grafana")


_SMLM_DASHBOARD_ID = 10277
_SMLM_DASHBOARD_REVISION = 2


def _dashboard_provisioning_config():
    return (
        "apiVersion: 1\n"
        "providers:\n"
        "  - name: lab-in-a-box\n"
        "    orgId: 1\n"
        "    folder: ''\n"
        "    type: file\n"
        "    disableDeletion: false\n"
        "    updateIntervalSeconds: 30\n"
        "    allowUiUpdates: true\n"
        "    options:\n"
        "      path: /etc/grafana/provisioning/dashboards\n"
    )


def _datasource_provisioning_config(prometheus_url):
    return (
        "apiVersion: 1\n"
        "datasources:\n"
        "  - name: Prometheus\n"
        "    type: prometheus\n"
        "    access: proxy\n"
        "    url: {}\n"
        "    isDefault: true\n"
        "    editable: true\n"
    ).format(prometheus_url)


def setup_grafana(hostname, cfg):
    """
    Deploys a standalone Grafana server as a podman container directly on `hostname` — no
    Kubernetes, matching install_prometheus.py's own style exactly (raw SSH, heredoc config,
    --network host, a host-level systemd unit for persistence). See that file's own comment on
    setup_prometheus() for why host networking specifically: the same container-DNS-resolves-a-
    real-hostname-to-127.0.0.1 pitfall applies here whenever Grafana's own datasource URL uses a
    real FQDN (e.g. the smlm server's own hostname) rather than a bare IP.
    """
    image = cfg.get("grafana_image") or "docker.io/grafana/grafana"
    version = cfg.get("grafana_version") or "latest"
    port = cfg.get("grafana_port") or "3000"
    admin_user = cfg.get("grafana_admin_user") or "admin"
    admin_password = cfg.get("grafana_admin_password") or "admin"
    prometheus_url = cfg.get("grafana_prometheus_url")

    import_smlm = (cfg.get("grafana_import_smlm_dashboard") or "").lower() == "true"
    extra_dashboards = cfg.get("grafana_dashboards") or []
    if (import_smlm or extra_dashboards) and not prometheus_url:
        die("grafana: grafana_prometheus_url is required when grafana_import_smlm_dashboard or "
            "grafana_dashboards is set — a dashboard needs a datasource to query")
    if import_smlm and not cfg.get("grafana_smlm_job_name"):
        die("grafana: grafana_smlm_job_name is required when grafana_import_smlm_dashboard is "
            "\"true\" — must match install_prometheus.py's own scrape job_name for that server "
            "(its auto-generated smlm job is \"smlm-<hostname>\")")

    print("- Installing podman (if not already present)")
    ssh_run(hostname, "command -v podman >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y podman 2>/dev/null || "
                       "apt-get install -y podman 2>/dev/null || "
                       "dnf install -y podman 2>/dev/null)", check=False)
    ssh_run(hostname, "systemctl enable --now podman.socket", check=False)

    ssh_run(hostname, "mkdir -p /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards")

    if prometheus_url:
        print("- Provisioning the Prometheus datasource ({})".format(prometheus_url))
        ssh_run(hostname, "cat > /etc/grafana/provisioning/datasources/prometheus.yaml <<'EOF'\n{}EOF".format(
            _datasource_provisioning_config(prometheus_url)))
        ssh_run(hostname, "cat > /etc/grafana/provisioning/dashboards/provider.yaml <<'EOF'\n{}EOF".format(
            _dashboard_provisioning_config()))

    def _sed_escape(value):
        """Escapes a value for safe use as a sed replacement (between /../ delimiters) —
        backslash, forward-slash and & (sed's own "whole match" token) all need escaping."""
        return value.replace("\\", "\\\\").replace("/", "\\/").replace("&", "\\&")

    if import_smlm:
        job = cfg.get("grafana_smlm_job_name")
        print("- Fetching and provisioning the SUSE Manager/Uyuni community dashboard "
              "(id {})".format(_SMLM_DASHBOARD_ID))
        # Fetched and placeholder-substituted ON THE TARGET HOST (not embedded in this repo) so
        # this addon always picks up whatever revision is live at deploy time, and never carries
        # a multi-hundred-line JSON blob to keep in sync by hand.
        sed_script = "s/${{DS_PROMETHEUS}}/Prometheus/g; s/${{VAR_JOB}}/{}/g".format(_sed_escape(job))
        fetch_cmd = (
            "curl -fsSL https://grafana.com/api/dashboards/{did}/revisions/{rev}/download "
            "| sed {sed} > /etc/grafana/provisioning/dashboards/smlm.json"
        ).format(did=_SMLM_DASHBOARD_ID, rev=_SMLM_DASHBOARD_REVISION, sed=shlex.quote(sed_script))
        ssh_run(hostname, fetch_cmd)

    for entry in extra_dashboards:
        dash_id = entry.get("id")
        name = entry.get("name") or "dashboard-{}".format(dash_id)
        if not dash_id:
            die("grafana_dashboards: an entry is missing required 'id'")
        print("- Fetching and provisioning dashboard id {} ({})".format(dash_id, name))
        ssh_run(hostname,
                "curl -fsSL https://grafana.com/api/dashboards/{did}/revisions/1/download "
                "| sed {sed} > {dest}".format(
                    did=dash_id, sed=shlex.quote("s/${DS_PROMETHEUS}/Prometheus/g"),
                    dest=shlex.quote("/etc/grafana/provisioning/dashboards/{}.json".format(name))))

    print("- Deploying the Grafana container (port {})".format(port))
    ssh_run(hostname, "podman rm -f grafana 2>/dev/null", check=False)
    ssh_run(hostname,
            "podman run -d --name grafana --restart=always --network host "
            "-e GF_SECURITY_ADMIN_USER={admin_user} "
            "-e GF_SECURITY_ADMIN_PASSWORD={admin_password} "
            "-e GF_SERVER_HTTP_PORT={port} "
            "-v /etc/grafana/provisioning:/etc/grafana/provisioning:ro,Z "
            "-v grafana-data:/var/lib/grafana "
            "{image}:{version}".format(
                admin_user=shlex.quote(admin_user), admin_password=shlex.quote(admin_password),
                port=shlex.quote(str(port)), image=shlex.quote(image), version=shlex.quote(version)))

    print("- Generating a systemd unit so the container survives a reboot")
    unit = (
        "[Unit]\n"
        "Description=Grafana (lab-in-a-box)\n"
        "After=network-online.target podman.socket\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Restart=always\n"
        "ExecStart=/usr/bin/podman start -a grafana\n"
        "ExecStop=/usr/bin/podman stop -t 10 grafana\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    ssh_run(hostname, "cat > /etc/systemd/system/grafana.service <<'EOF'\n{}EOF".format(unit))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable grafana.service", check=False)

    print("Grafana available at: http://{}:{}  ({} / {})".format(
        hostname, port, admin_user, "<as configured>" if admin_password != "admin" else "admin"))
    if admin_password == "admin":
        print("WARNING: grafana_admin_password not set — using the literal default \"admin\". "
              "Change it in the JSON before treating this as anything but a throwaway lab.")


def main():
    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    definition = primary.load_definition(sys.argv[1])
    cfg = definition.get("grafana", {}) or {}

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "grafana", vm_name=env_vm_name):
        setup_grafana(vm_name, cfg)


if __name__ == "__main__":
    main()
