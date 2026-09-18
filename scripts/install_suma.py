#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUMA 5.0 beta
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "suma" — SUSE Multi-Linux Manager (SUMA) host-level deployment
#   NOTE: Installed directly on the OS via mgradm, not in Kubernetes.
#         The target node must list "suma" in its nodes[x].addons[] array.
#
#   suma_reg_username : [OPTIONAL] registry.suse.com login username    (default: admin@mydemo.lab)
#   suma_reg_pwd      : [OPTIONAL] registry.suse.com login password    (default: aaaaaaaa)
#   suma_adm_pwd      : [OPTIONAL] SUMA web UI admin password          (default: admin123)
#   suma_channel      : [OPTIONAL] SCC product/channel identifier the host registers
#                                  against via `transactional-update register -p ...`
#                                  (default: SUSE-Manager-Server/5.0/x86_64)
#   suma_key          : [OPTIONAL] SCC registration key for suma_channel (default: aaaaaa)
#   suma_channels     : [OPTIONAL] space-separated list of additional channels to wait
#                                  for sync after install (default: none — skips the wait)

__version__ = "ca2d2d5"

PLUGIN = {
    "name": "suma",
    "targets": ["vm", "baremetal"],
    # standalone-container, not os-native: confirmed live 2026-08-29 — this
    # script's own install() runs `mgradm ... install podman`, a real
    # podman container on the host, not a bare package/binary install (see
    # libs/layers.py's own LAYER_OS_NATIVE docstring: "no container
    # involved" — mgradm's podman-based install doesn't qualify).
    "layers": ["standalone-container"],
    "requires_kubernetes": None,
    "aux_services": [],
}

import os
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, process_template, reboot_vm, check_ssh_conn  # noqa: E402


def setup_suma(hostname, virt_srv, templ_addons_loc, cfg, node_cfg):
    """Install SUSE Multi-Linux Manager on a host VM via mgradm. Mirrors setup_suma (bash)."""
    print("- Registering system to SUMA channel")
    # Default channel deliberately keeps the "SUSE-Manager-Server" product name (not renamed to
    # "SUSE-Multi-Linux-Manager-Server") — this addon defaults to version 5.0, and SCC's own real
    # product/channel identifiers only switched naming starting with 5.1 (confirmed against the
    # actual source images on nuc6: SUSE-Manager-Server.x86_64-5.0.x-*.qcow2 vs.
    # SUSE-Multi-Linux-Manager-Server.x86_64-5.1.x-*.qcow2 both genuinely exist, distinct names per
    # version). A real registration string, not documentation prose — do not rename this one.
    ssh_run(hostname,
            "transactional-update --quiet register -p {} -r {} ; reboot".format(
                cfg.get("suma_channel") or "SUSE-Manager-Server/5.0/x86_64", cfg.get("suma_key") or "aaaaaa"),
            check=False)
    time.sleep(5)
    check_ssh_conn(hostname)

    print("- install utility packages")
    ssh_run(hostname,
            "transactional-update --quiet pkg install -y mgradm-bash-completion mgrctl-bash-completion "
            "mgradm-zsh-completion mgrctl-zsh-completion; reboot", check=False)
    time.sleep(5)
    check_ssh_conn(hostname)

    extra_dsk = node_cfg.get("extra_dsk") or ""
    if extra_dsk:
        for dsk in extra_dsk.split():
            print("- Adding extra disk {}".format(dsk))
            # NOTE: bash unconditionally (re-)adds this same hardcoded 9p mirror-mount
            # line + mkdir on EVERY entry in extra_dsk, regardless of that entry's own
            # value — the UUID/mgr-storage-server handling visible in bash's comments was
            # never finished and is dead code. Preserved exactly, including the resulting
            # duplicate fstab line if extra_dsk lists more than one disk (harmless: mkdir
            # on an existing dir just errors quietly, no -p flag either way).
            ssh_run(hostname,
                    "echo \"mirror /srv/mirror 9p trans=virtio,version=9p2000.L,nofail,_netdev,x-mount.mkdir 0 0\" "
                    ">> /etc/fstab ;  mkdir /srv/mirror", check=False)
            device, _, mountpoint = dsk.partition(",")
            ssh_run(hostname, "echo \"{} {} xfs defaults,nofail 1 2\" >>/etc/fstab".format(device, mountpoint))
        reboot_vm(virt_srv, hostname)
        time.sleep(5)
        check_ssh_conn(hostname)
    else:
        print("NO EXTRA_DSK ")

    print("- Installing SUMA")
    ssh_run(hostname, "podman login --username  {} --password {} registry.suse.com".format(
        cfg.get("suma_reg_username") or "admin@mydemo.lab", cfg.get("suma_reg_pwd") or "aaaaaaaa"))

    tmpl = "{}/suma/mgradm.yaml.tmpl".format(str(templ_addons_loc).rstrip("/"))
    ssh_run(hostname, "cat > /var/tmp/mgradm.yaml", input_text=process_template(tmpl, cfg))

    ssh_run(hostname, "mgradm -c /var/tmp/mgradm.yaml install podman ")
    time.sleep(120)
    ssh_run(hostname, "reboot", check=False)
    time.sleep(5)
    check_ssh_conn(hostname)

    print("- Sync admin user password")
    adm_pwd = cfg.get("suma_adm_pwd") or "admin123"
    ssh_run(hostname,
            "mgrctl exec -- \"echo -e 'mgrsync.user = admin\\nmgrsync.password = {}' >~/.mgr-sync\"".format(adm_pwd))

    print("SUMA should be available in a few minutes, visit https://{}/".format(hostname))

    channels = cfg.get("suma_channels") or ""
    if channels:
        count = 0
        print("- Let's wait for the channel list to sync'")
        while True:
            time.sleep(5)
            count += 1
            print("Retry {}".format(count), end="\r")
            out = ssh_run(hostname, "mgrctl exec -- mgr-sync list channels 2>/dev/null ",
                          check=False, capture=True).stdout or ""
            if any("no channels found." not in line.lower() for line in out.splitlines()):
                break
        print("- We will start the process of synching the selected channels, this may take hours")
        print("list: {} ".format(channels))
        time.sleep(300)
        ssh_run(hostname, "mgrctl exec -- mgr-sync add channels {}".format(channels))


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
    config = primary.load_config()

    cfg = definition.get("suma", {}) or {}
    virt_srv = config.get("VIRT_SRV", "")
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")

    # bash hand-rolls the same logic as on_addon_nodes here (respect an
    # inherited _vm_name, else scan for nodes with "suma" in their addons[]).
    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "suma", vm_name=env_vm_name):
        print("- Using node: {}".format(vm_name))
        node_cfg = definition.get("nodes", {}).get(vm_name, {})
        setup_suma(vm_name, virt_srv, templ_addons_loc, cfg, node_cfg)


if __name__ == "__main__":
    main()
