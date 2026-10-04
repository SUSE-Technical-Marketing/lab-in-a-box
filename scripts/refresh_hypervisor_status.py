#!/usr/bin/env python3
# Part of lab-in-a-box — refreshes the cached hypervisor status snapshot the
# lab-builder webui's status panel and live ISO_IMAGE dropdown read from.
# Author/s: Raul Mahiques
# License: GPLv3
#
# Run as root on a schedule (cron/systemd timer — see
# install_automation_node_scripts.sh), NEVER by the webui's CGI itself: the
# CGI runs as the Apache user and never runs anything privileged (see
# webui/apache/lab-builder.conf's header) — SSH to the hypervisor needs
# root's own key (root:root 0600, unreadable by the CGI's user), so this
# script is the only thing that ever queries the hypervisor directly. It
# writes a plain JSON snapshot containing no credentials — any config value
# shaped like a secret is masked before it ever reaches disk — that the CGI
# just reads (webui/lib/discovery.py's status()).
#
# Usage:
#   refresh_hypervisor_status.py [output_path]
#   (output_path defaults to $LABBUILDER_STATUS_FILE or
#    /srv/www/lab-builder/status.json)

__version__ = "bac232b"

import json
import os
import re
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation",
                   str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation as lc  # noqa: E402

_SECRET_NAME_RE = re.compile(r'(PASS|PWD|SECRET|TOKEN|KEY)', re.IGNORECASE)
_IMAGE_RE = re.compile(r'\.(?:iso|qcow2)$', re.IGNORECASE)

# Non-secret config keys worth surfacing in the status panel. Still run
# through _mask() as a safety net if any of these ever turn out to hold
# something secret-shaped.
_CONFIG_KEYS = ("REMOTE_HOST", "KVM_HOSTS", "VIRT_SRV", "BACKEND")
_DEFAULTS_KEYS = ("ISO_LOC", "VM_IMG_LOC")


def _mask(key, value):
    return "********" if _SECRET_NAME_RE.search(key) else value


def _configured_hosts(cfg):
    hosts_raw = cfg.get("KVM_HOSTS") or cfg.get("REMOTE_HOST") or ""
    return [h for h in hosts_raw.split() if h]


def host_status(host, vm_img_loc):
    """
    Free vCPUs/RAM/disk on one host, over SSH. virsh runs LOCALLY on `host`
    itself (qemu:///system) — NOT via a qemu+ssh:// URI back to `host`. We
    are already executing remotely on that exact host via ssh_output, so
    reconnecting via qemu+ssh://root@{host} from within that same host is a
    redundant loopback SSH hop: libvirt's own internal SSH client then
    prompts to accept a host key for "localhost"/"::1" (from that host's own
    point of view) that nothing has ever pre-accepted, and hangs
    indefinitely waiting for interactive confirmation. This script runs
    unattended via cron/systemd timer with nobody present to type "yes".
    Never raises — a query failure is reported per-host so one unreachable
    host doesn't blank the whole snapshot.
    """
    try:
        total_cpus = int(lc.ssh_output(host, "nproc"))
        running = [d for d in lc.ssh_output(
            host, "virsh --connect qemu:///system list --name").splitlines() if d.strip()]
        used_cpus = 0
        for dom in running:
            used_cpus += int(lc.ssh_output(
                host, "virsh --connect qemu:///system vcpucount --current {}".format(dom.strip())))
        free_cpu = max(total_cpus - used_cpus, 0)
        free_mem = int(lc.ssh_output(host, "free -m | awk '/^Mem:/{print $7}'"))
        free_disk = int(re.sub(r"[^0-9]", "", lc.ssh_output(
            host, "df -BM --output=avail {} | tail -1".format(vm_img_loc))))
        return {"host": host, "free_cpu": free_cpu, "free_mem_mb": free_mem,
                "free_disk_mb": free_disk, "error": None}
    except Exception as e:
        return {"host": host, "free_cpu": None, "free_mem_mb": None,
                "free_disk_mb": None, "error": "{}: {}".format(type(e).__name__, e)}


def list_images(host, iso_loc):
    """.iso/.qcow2 filenames at iso_loc on host. Never raises — an
    unreachable host or missing ISO_LOC just yields no images."""
    if not host or not iso_loc:
        return []
    try:
        out = lc.ssh_output(host, "ls -1 '{}' 2>/dev/null".format(iso_loc))
    except Exception:
        return []
    return sorted(f for f in out.splitlines() if _IMAGE_RE.search(f))


def build_status():
    cfg = primary.load_config()
    try:
        defaults = primary.load_defaults()
    except SystemExit:
        defaults = {}

    default_host = cfg.get("REMOTE_HOST", "")
    hosts = _configured_hosts(cfg) or ([default_host] if default_host else [])
    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/")
    iso_loc = defaults.get("ISO_LOC", "")

    host_statuses = [host_status(h, vm_img_loc) for h in hosts]
    images = list_images(hosts[0] if hosts else "", iso_loc)

    config_out = {}
    for k in _CONFIG_KEYS:
        if k in cfg:
            config_out[k] = _mask(k, cfg[k])
    for k in _DEFAULTS_KEYS:
        if k in defaults:
            config_out[k] = _mask(k, defaults[k])

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": config_out,
        "hosts": host_statuses,
        "images": images,
    }


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("LABBUILDER_STATUS_FILE", "/srv/www/lab-builder/status.json")
    try:
        status = build_status()
    except SystemExit as e:
        # lab_creation.cfg itself missing/unreadable — still write something,
        # so a broken config doesn't leave the webui stuck on a stale-forever
        # snapshot from before the breakage.
        status = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config": {}, "hosts": [], "images": [],
            "error": "could not load lab_creation.cfg: {}".format(e),
        }

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, out_path)
    os.chmod(out_path, 0o644)


if __name__ == "__main__":
    main()
