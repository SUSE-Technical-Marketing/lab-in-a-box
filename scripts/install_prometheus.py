#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs a standalone Prometheus server as a podman container on the target host, with no Kubernetes.
# It scrapes the exporters of an smlm server that has smlm_monitoring_enabled set (see install_smlm.py).
# Author/s: Raul Mahiques
# License: GPLv3
#
# References: https://prometheus.io/docs/prometheus/latest/configuration/configuration/
#            https://documentation.suse.com/multi-linux-manager/5.2/en/docs/administration/monitoring.html
#
# ─── JSON section: "prometheus" ─────────────────────────────────────────────
#
# OPTIONAL
#   prometheus_version         : container image tag (default "latest")
#   prometheus_image           : full image reference (default "docker.io/prom/prometheus"). There is no SUSE-branded image. The SUSE
#                                guide configures a third-party Prometheus against the smlm exporters.
#   prometheus_port            : port Prometheus listens on, with host networking (default "9090")
#   prometheus_retention       : --storage.tsdb.retention.time value (default "15d")
#   prometheus_scrape_smlm     : FQDN of an smlm server with smlm_monitoring_enabled set. A scrape job is generated for its exporter
#                                ports 9100, 9187, 5556, 5557 and 9800, plus the message-queue job at <host>:80 with the metrics path /rhn/metrics.
#   prometheus_scrape_configs  : [{"job_name": "...", "targets": ["host:port", ...], "metrics_path": "/metrics"}]. Extra jobs, added
#                                after the generated smlm job, if any.
#
# Target node(s): any node listing "prometheus" in addons[], as for the podman deployment of install_smlm.py.
# The node must have prometheus_port open. This addon does not manage firewalls or security groups. For AWS nodes, add the port to aws_open_ports.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "prometheus",
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
    v.vver("prometheus")


# Exporter ports: node 9100, postgres 9187, tomcat JMX 5556, taskomatic JMX 5557 and taskomatic direct 9800. The message-queue job uses
# the web port and the path /rhn/metrics, and it is handled separately.
_SMLM_EXPORTER_PORTS = {
    "node": 9100,
    "postgres": 9187,
    "tomcat-jmx": 5556,
    "taskomatic-jmx": 5557,
    "taskomatic": 9800,
}


def _smlm_scrape_job(smlm_host):
    """
    Build the two scrape_configs entries for one smlm server with monitoring enabled. One job covers the bundled exporter ports. A
    second job scrapes the message-queue metrics, which the existing web port serves at /rhn/metrics.
    """
    return [
        {
            "job_name": "smlm-{}".format(smlm_host),
            "targets": ["{}:{}".format(smlm_host, port) for port in _SMLM_EXPORTER_PORTS.values()],
        },
        {
            "job_name": "smlm-{}-mq".format(smlm_host),
            "targets": ["{}:80".format(smlm_host)],
            "metrics_path": "/rhn/metrics",
        },
    ]


def _render_prometheus_yml(scrape_configs):
    lines = ["global:", "  scrape_interval: 30s", "", "scrape_configs:"]
    for job in scrape_configs:
        lines.append("  - job_name: '{}'".format(job["job_name"]))
        if job.get("metrics_path"):
            lines.append("    metrics_path: '{}'".format(job["metrics_path"]))
        lines.append("    static_configs:")
        lines.append("      - targets:")
        for target in job["targets"]:
            lines.append("          - '{}'".format(target))
    return "\n".join(lines) + "\n"


def setup_prometheus(hostname, cfg):
    """
    Deploys a standalone Prometheus server as a podman container directly on `hostname` — no
    Kubernetes, matching install_smlm.py's own "podman" deployment mode's own style (raw SSH,
    heredoc config, a host-level systemd unit for persistence across reboots — the container
    itself is NOT --rm, podman's own restart policy handles crash recovery, matching the "small
    container, default safest mode" convention this project already uses for other new
    infra services, e.g. the PXE service).
    """
    image = cfg.get("prometheus_image") or "docker.io/prom/prometheus"
    version = cfg.get("prometheus_version") or "latest"
    port = cfg.get("prometheus_port") or "9090"
    retention = cfg.get("prometheus_retention") or "15d"

    scrape_configs = []
    smlm_host = cfg.get("prometheus_scrape_smlm")
    if smlm_host:
        scrape_configs.extend(_smlm_scrape_job(smlm_host))
    scrape_configs.extend(cfg.get("prometheus_scrape_configs") or [])
    if not scrape_configs:
        die("prometheus: set prometheus_scrape_smlm and/or prometheus_scrape_configs — a "
            "Prometheus with nothing to scrape isn't useful")

    print("- Installing podman (if not already present)")
    ssh_run(hostname, "command -v podman >/dev/null 2>&1 || "
                       "(zypper --non-interactive install -y podman 2>/dev/null || "
                       "apt-get install -y podman 2>/dev/null || "
                       "dnf install -y podman 2>/dev/null)", check=False)
    ssh_run(hostname, "systemctl enable --now podman.socket", check=False)

    print("- Writing prometheus.yml (scrape config for {} job(s))".format(len(scrape_configs)))
    prometheus_yml = _render_prometheus_yml(scrape_configs)
    ssh_run(hostname, "mkdir -p /etc/prometheus")
    ssh_run(hostname, "cat > /etc/prometheus/prometheus.yml <<'EOF'\n{}EOF".format(prometheus_yml))

    print("- Deploying the Prometheus container (port {})".format(port))
    ssh_run(hostname, "podman rm -f prometheus 2>/dev/null", check=False)
    # The container uses --network host, not a -p port mapping. With bridge networking, the container resolves a scrape target's
    # hostname to its own loopback address, so scrapes fail. Host networking resolves names as the host does, and the container listens
    # on the host's own network stack.
    ssh_run(hostname,
            "podman run -d --name prometheus --restart=always --network host "
            "-v /etc/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro,Z "
            "-v prometheus-data:/prometheus "
            "{image}:{version} "
            "--config.file=/etc/prometheus/prometheus.yml "
            "--storage.tsdb.retention.time={retention} "
            "--web.listen-address=:{port}".format(
                port=shlex.quote(str(port)), image=shlex.quote(image), version=shlex.quote(version),
                retention=shlex.quote(retention)))

    print("- Generating a systemd unit so the container survives a reboot")
    unit = (
        "[Unit]\n"
        "Description=Prometheus (lab-in-a-box)\n"
        "After=network-online.target podman.socket\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Restart=always\n"
        "ExecStart=/usr/bin/podman start -a prometheus\n"
        "ExecStop=/usr/bin/podman stop -t 10 prometheus\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    ssh_run(hostname, "cat > /etc/systemd/system/prometheus.service <<'EOF'\n{}EOF".format(unit))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable prometheus.service", check=False)

    print("Prometheus available at: http://{}:{}".format(hostname, port))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    definition = primary.load_definition(sys.argv[1])
    cfg = definition.get("prometheus", {}) or {}

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "prometheus", vm_name=env_vm_name):
        setup_prometheus(vm_name, cfg)


if __name__ == "__main__":
    main()
