#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install a standalone Prometheus server as a
# podman container directly on the target host (no Kubernetes involved) —
# built 2026-09-16 at the user's explicit request for an "external"
# Prometheus to scrape an smlm addon's own bundled exporters
# (smlm_monitoring_enabled, see install_smlm.py's own schema docs).
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://prometheus.io/docs/prometheus/latest/configuration/configuration/
#            https://documentation.suse.com/suma/5.2/en/docs/administration/monitoring.html
#            (exact exporter ports/scrape-config shape confirmed live 2026-09-16 against a real
#            SMLM 5.2 server — see this file's own _smlm_scrape_job() docstring)
#
# ─── JSON section: "prometheus" ─────────────────────────────────────────────
#
# OPTIONAL
#   prometheus_version         : container image tag                (default: "latest")
#   prometheus_image           : full image reference                (default:
#                                 "docker.io/prom/prometheus" — the standard upstream image;
#                                 there is no SUSE-branded Prometheus image, per SUSE's own docs
#                                 above, which document configuring a THIRD-PARTY Prometheus
#                                 against SMLM's bundled exporters rather than shipping one)
#   prometheus_port            : port Prometheus listens on directly (host networking, no
#                                 container port-publish layer — see setup_prometheus()'s own
#                                 comment on why)                     (default: "9090")
#   prometheus_retention       : --storage.tsdb.retention.time value  (default: "15d")
#   prometheus_scrape_smlm     : FQDN of an smlm-addon server (with smlm_monitoring_enabled:
#                                 "true") to auto-generate a scrape job for — every port
#                                 documentation.suse.com/suma/5.2's own Monitoring guide lists
#                                 (9100 node, 9187 postgres, 5556 tomcat JMX, 5557 taskomatic JMX,
#                                 9800 taskomatic direct), plus the message-queue job at
#                                 "<host>:80" with metrics_path "/rhn/metrics" — confirmed live
#                                 2026-09-16, see _smlm_scrape_job() below for the exact shape.
#   prometheus_scrape_configs  : [{"job_name": "...", "targets": ["host:port", ...],
#                                  "metrics_path": "/metrics"}, ...]   # extra jobs, appended
#                                 after the auto-generated smlm one (if any)
#
# Target node(s): any node listing "prometheus" in its own addons[] list — same
# dispatch shape as install_smlm.py's own "podman" deployment mode (k8s.addon_nodes()).
# Reachable remotely on prometheus_port — the NODE ITSELF needs that port open (this addon
# does not manage firewalls/security groups; see the node's own aws_open_ports for AWS-backed
# nodes, matching the existing pattern create_vm() already established for smlm's own ports).

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


# Real ports confirmed live 2026-09-16 against documentation.suse.com/suma/5.2's own Monitoring
# guide and a real enabled server: name -> port. The message-queue job (Apache/existing web port,
# metrics path /rhn/metrics, not a dedicated port) is handled separately below.
_SMLM_EXPORTER_PORTS = {
    "node": 9100,
    "postgres": 9187,
    "tomcat-jmx": 5556,
    "taskomatic-jmx": 5557,
    "taskomatic": 9800,
}


def _smlm_scrape_job(smlm_host):
    """
    Builds the two scrape_configs entries documentation.suse.com/suma/5.2's own example
    prometheus.yml shows for one smlm server with monitoring enabled: one job with every bundled
    exporter's own port, and a second for the message-queue metrics served on the existing web
    port at path /rhn/metrics (not a separate port at all — confirmed live).
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
    # --network host, not -p {port}:9090: confirmed live 2026-09-16 that bridge
    # networking's own container DNS/hosts resolves the SAME hostname a scrape
    # target uses (e.g. the smlm server's own FQDN) to 127.0.0.1 — the
    # container's OWN loopback, not the real host — silently breaking every
    # scrape ("wget: can't connect to remote host (127.0.0.1): Connection
    # refused" from inside the container, even though the exact same URL
    # curled fine from the host's own shell). Host networking makes hostname
    # resolution behave identically to the host itself, and doubles as the
    # simplest way to make this "available remotely" — no NAT/port-publish
    # layer at all, the container just binds directly to the host's own
    # network stack, matching whatever port it's told to listen on.
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
