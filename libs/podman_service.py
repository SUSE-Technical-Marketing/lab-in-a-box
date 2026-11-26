#!/usr/bin/env python3
# Part of lab-in-a-box — one long-running container on a node.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/podman_service.py: run one service as a podman container on a node, and keep it running.

Several add-ons install a service that is simply one container on a VM or a bare-metal host rather than anything
Kubernetes: pull the image, replace whatever ran before, publish a port or two, keep its data in a volume or a
host directory, write a systemd unit so it comes back after a reboot, and wait for it to answer. That is the whole
shape, and it lives here once.

An add-on supplies the image, the ports, the environment and the volumes; what the service then does is its own
business. Nothing here assumes Kubernetes, a cluster or a DNS record: a service on a node is reached at that
node's own name, which the lab already registered when it created it.
"""

import shlex

from lab_creation import ssh_run, ssh_output, ensure_podman, log, warn

UNIT_DIR = "/etc/systemd/system"


def _pairs(flag, values):
    """`values`, a dict or a list of strings, as repeated `flag` arguments, each one shell-quoted."""
    if not values:
        return []
    if isinstance(values, dict):
        values = ["{}={}".format(name, value) for name, value in sorted(values.items())]
    return ["{} {}".format(flag, shlex.quote(str(v))) for v in values]


def run_command(name, image, ports=(), env=None, volumes=(), options=(), args=(), network_host=False,
                env_file=""):
    """
    The podman command that runs this service, built from what an add-on knows about it. `ports` are
    "host:container" (or "address:host:container") strings, `env` a dict or NAME=VALUE list, `volumes`
    "source:destination[:flags]" strings, `options` further podman flags as given, and `args` the container's own
    arguments. With `network_host` the container shares the node's network and `ports` are left out, which is what
    a service that has to see the node's own interfaces needs.

    A secret belongs in `env_file`, a path on the node that podman reads for itself, and never in `env`: the
    command this returns is what the process list and the systemd unit show to anyone on that node.
    """
    parts = ["podman run -d --name {}".format(shlex.quote(name)), "--restart=always"]
    if network_host:
        parts.append("--network host")
    else:
        parts += _pairs("-p", list(ports))
    if env_file:
        parts.append("--env-file {}".format(shlex.quote(str(env_file))))
    parts += _pairs("-e", env)
    parts += _pairs("-v", list(volumes))
    parts += [str(o) for o in options]
    parts.append(shlex.quote(image))
    parts += [shlex.quote(str(a)) for a in args]
    return " ".join(parts)


def unit_text(name, description):
    """The systemd unit that starts this container at boot, the same shape every add-on here writes."""
    return (
        "[Unit]\n"
        "Description={}\n"
        "After=network-online.target podman.socket\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Restart=always\n"
        "ExecStart=/usr/bin/podman start -a {}\n"
        "ExecStop=/usr/bin/podman stop -t 10 {}\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).format(description, name, name)


def install_unit(hostname, name, description):
    """
    Write and enable the unit, so the container is back after a reboot. Enabling is best effort: a node without
    systemd still has the container's own --restart=always.
    """
    ssh_run(hostname, "cat > {}/{}.service".format(UNIT_DIR, name), input_text=unit_text(name, description))
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable {}.service".format(shlex.quote(name)),
            check=False)


def deploy(hostname, name, image, description="", ports=(), env=None, volumes=(), options=(), args=(),
           network_host=False, pull=True, env_file=""):
    """
    Put this service on `hostname`: podman, the image, a fresh container in place of whatever ran before, and the
    unit that brings it back. Returns the command it ran, which is what a --check can compare against.
    """
    ensure_podman(hostname)
    if pull:
        ssh_run(hostname, "podman pull {}".format(shlex.quote(image)))
    ssh_run(hostname, "podman rm -f {} 2>/dev/null".format(shlex.quote(name)), check=False)
    command = run_command(name, image, ports=ports, env=env, volumes=volumes, options=options, args=args,
                          network_host=network_host, env_file=env_file)
    ssh_run(hostname, command)
    install_unit(hostname, name, description or "{} (lab-in-a-box)".format(name))
    log("- The {} container is running on {}".format(name, hostname))
    return command


def wait_for_http(hostname, url, timeout=300, what=""):
    """
    Wait, on the node itself, for `url` to answer. True when it did. A service that is still downloading a model
    on first boot takes minutes, so the caller's timeout is what decides how long a lab waits, and a timeout is
    reported rather than raised: the container is there either way, and the operator can watch its log.
    """
    deadline = max(int(timeout), 1)
    script = ("for i in $(seq 1 {n}); do curl -fsS -o /dev/null {url} && exit 0; sleep 1; done; exit 1").format(
        n=deadline, url=shlex.quote(url))
    if ssh_run(hostname, script, check=False).returncode == 0:
        log("- {} answers on {}".format(what or url, hostname))
        return True
    warn("- {} did not answer within {}s on {}: the container is running, so watch 'podman logs' there".format(
        what or url, deadline, hostname))
    return False


def container_image(hostname, name):
    """The image the named container is actually running on the node, "" when there is no such container."""
    return ssh_output(hostname, "podman inspect -f '{{{{.ImageName}}}}' {} 2>/dev/null".format(
        shlex.quote(name))).strip()


def is_running(hostname, name):
    """Whether the named container is running on the node."""
    return ssh_run(hostname, "podman ps --filter name=^{}$ --filter status=running --format '{{{{.Names}}}}' "
                             "| grep -qx {}".format(shlex.quote(name), shlex.quote(name)),
                   check=False).returncode == 0
