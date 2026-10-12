#!/usr/bin/env python3
# Mocked unit tests for libs/lab_creation.py's provisioning helpers: _generate_unused_mac, _host_resources,
# clean_ssh_keys, copy_to_hypervisor, prepare_ignition_combustion, prepare_virt_customize_for_vm, setup_salt and the
# network auto-detection used by load_vm_vars (_detect_dns/_detect_gateway/_detect_domain/_detect_netmask).
# Run from 10_lab_creation_core.sh, in its own container — see tests/run_tests.sh.
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402
import lab_creation as lc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def done(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


# ── _generate_unused_mac ───────────────────────────────────────────────────────────────────────────────────────────
picks = iter([0x12, 1, 2, 3, 4, 5, 0x13, 6, 7, 8, 9, 10])
with mock.patch.object(lc.random, "randint", side_effect=lambda a, b: next(picks)):
    mac = lc._generate_unused_mac({"12:01:02:03:04:05"})
check("_generate_unused_mac: skips a MAC already in use", mac == "12:06:07:08:09:0a")
first = int(lc._generate_unused_mac(set()).split(":")[0], 16)
check("_generate_unused_mac: locally administered unicast", first & 0x02 and not first & 0x01)

# ── _host_resources / copy_to_hypervisor: build a LibvirtBackend for the host ──────────────────────────────────────
with mock.patch.object(backends.LibvirtBackend, "host_resources", return_value=(8, 16384, 99999)) as res:
    check("_host_resources: the host's free CPU, memory and disk", lc._host_resources("hv1", "/var/lib/libvirt/images") ==
          (8, 16384, 99999))
with mock.patch.object(backends.LibvirtBackend, "push_provisioning_files") as push:
    lc.copy_to_hypervisor("hv1", "/srv/www", "vm1", config_method="cloud-init", vm_img_loc="/img")
check("copy_to_hypervisor: pushes the VM's provisioning files with its config method",
      push.call_args[0] == ("vm1",) and push.call_args[1] == {"config_method": "cloud-init", "vm_img_loc": "/img"})

# ── clean_ssh_keys ─────────────────────────────────────────────────────────────────────────────────────────────────
with mock.patch.object(lc.subprocess, "run", return_value=done()) as run:
    lc.clean_ssh_keys("vm1.lab", "10.0.0.7")
check("clean_ssh_keys: removes the name and the IP from known_hosts",
      [c[0][0][-1] for c in run.call_args_list] == ["vm1.lab", "10.0.0.7"]
      and all(c[0][0][:2] == ["ssh-keygen", "-f"] for c in run.call_args_list))

# ── prepare_ignition_combustion ────────────────────────────────────────────────────────────────────────────────────
home_key = Path("/root/.ssh/id_rsa.pub")
if not home_key.exists():
    home_key.parent.mkdir(parents=True, exist_ok=True)
    home_key.write_text("ssh-ed25519 LOCALKEY root@automation\n")
local_key = home_key.read_text().strip()
web = Path(tempfile.mkdtemp())
(web / "ignition").mkdir()
(web / "combustion").mkdir()
(web / "ignition" / "template").write_text('{"hostname": "TEMPLATE_HN", "hash": "ROOT_PWD_HASH", "key": "ROOT_SSH_KEY"}')
(web / "combustion" / "template").write_text("#!/bin/bash\n#local vars\necho ROOT_SSH_KEY\n")
lc.prepare_ignition_combustion("vm1", str(web), "$6$h", "ssh-ed25519 CFGKEY", "automation.lab", "/lab/scripts/",
                               "10.0.0.53", "10.0.0.7", "24", "10.0.0.1", suse_email="me@x", suse_regcode="R", suse_url="U")
ign = (web / "ignition" / "vm1.ign").read_text()
cmb = (web / "combustion" / "vm1").read_text()
check("prepare_ignition_combustion: ignition gets the host name, hash and this machine's key",
      ign == '{"hostname": "vm1", "hash": "$6$h", "key": "' + local_key + '"}')
check("prepare_ignition_combustion: combustion gets the node's variables after '#local vars'",
      "#local vars\nmysource=automation.lab\nsourcepath=/lab/scripts/\nmydns=10.0.0.53\nmyip=10.0.0.7\nmymask=24\n"
      "mygw=10.0.0.1\nSUSE_email=me@x\nSUSE_regcode=R\nSUSE_url=U\n" in cmb)
check("prepare_ignition_combustion: combustion gets ROOT_SSH_KEY from the config", "echo ssh-ed25519 CFGKEY" in cmb)

# ── prepare_virt_customize_for_vm: password and key fallbacks ──────────────────────────────────────────────────────
keydir = Path(tempfile.mkdtemp())
(keydir / "lab_key.pub").write_text("ssh-ed25519 LABKEY\n")
with mock.patch.object(lc, "prepare_virt_customize") as pvc:
    lc.prepare_virt_customize_for_vm("hv1", "/img", "vm1", "10.0.0.7", "", "10.0.0.1", "10.0.0.53", "lab", "52:54:00:aa:bb:cc",
                                     vm_root_pass="pw", root_pwd_hash="$6$h", root_ssh_key_path=str(keydir / "lab_key"))
    kw = pvc.call_args[1]
    check("prepare_virt_customize_for_vm: VM_ROOT_PASS wins, as a plain password; prefix defaults to 24",
          (kw["pass_type"], kw["pass_val"], kw["prefix"], kw["img_path"]) == ("plain", "pw", "24", "/img/vm1.qcow2"))
    check("prepare_virt_customize_for_vm: the configured key's .pub is injected", kw["pubkey"] == "ssh-ed25519 LABKEY")
    lc.prepare_virt_customize_for_vm("hv1", "/img", "vm1", "10.0.0.7", "16", "", "", "", "", root_pwd_hash="$6$h")
    kw = pvc.call_args[1]
    check("prepare_virt_customize_for_vm: else ROOT_PWD_HASH as a crypted password, and ~/.ssh/id_rsa.pub",
          (kw["pass_type"], kw["pass_val"], kw["prefix"], kw["pubkey"]) == ("crypted", "$6$h", "16", local_key))
    lc.prepare_virt_customize_for_vm("hv1", "/img", "vm1", "10.0.0.7", "24", "", "", "", "")
    check("prepare_virt_customize_for_vm: else the plain password 'linux'",
          (pvc.call_args[1]["pass_type"], pvc.call_args[1]["pass_val"]) == ("plain", "linux"))

# ── setup_salt ─────────────────────────────────────────────────────────────────────────────────────────────────────
home = Path(tempfile.mkdtemp())
(web / "salt-ssh").mkdir()
for state in ("base.sls", "web.sls"):
    (web / "salt-ssh" / state).write_text("state for ${_vm_name}\n")
with mock.patch.object(lc.Path, "home", return_value=home):
    lc.setup_salt("vm1", "base.sls web.sls", str(web))
check("setup_salt: writes a roster for the VM as root", "managed:\n  host: vm1\n  user: root\n"
      in (home / "salt-ssh" / "roster").read_text())
check("setup_salt: renders every state with the VM name",
      [(home / "salt-ssh" / "states" / s).read_text() for s in ("base.sls", "web.sls")] == ["state for vm1\n"] * 2)

# ── network auto-detection ─────────────────────────────────────────────────────────────────────────────────────────
resolv = Path(tempfile.mkdtemp()) / "resolv.conf"
resolv.write_text("search lab\nnameserver 10.0.0.53\nnameserver 10.0.0.54\n")
real_path = lc.Path
with mock.patch.object(lc, "Path", side_effect=lambda p, *a: real_path(str(resolv)) if p == "/etc/resolv.conf"
                       else real_path(p, *a)):
    check("_detect_dns: the first nameserver of /etc/resolv.conf", lc._detect_dns() == "10.0.0.53")
outputs = {"route": "default via 10.0.0.1 dev eth0 proto static\n",
           "addr": "2: eth0    inet 10.0.0.7/22 brd 10.0.3.255 scope global eth0\n"}
with mock.patch.object(lc.subprocess, "run",
                       side_effect=lambda cmd, **k: done(outputs["route" if "route" in cmd else "addr"])) as run:
    check("_detect_gateway: the default route's gateway", lc._detect_gateway() == "10.0.0.1")
    check("_detect_netmask: the prefix of the default route's device", lc._detect_netmask() == "22"
          and run.call_args[0][0][-1] == "eth0")
with mock.patch.object(lc.subprocess, "run", return_value=done("")):
    check("_detect_gateway/_detect_netmask: empty without a default route",
          lc._detect_gateway() == "" and lc._detect_netmask() == "")
with mock.patch("socket.getfqdn", return_value="automation.mydemo.lab"), \
        mock.patch("socket.gethostname", return_value="automation"):
    check("_detect_domain: the domain part of this host's FQDN", lc._detect_domain() == "mydemo.lab")
with mock.patch("socket.getfqdn", return_value="automation"), mock.patch("socket.gethostname", return_value="automation"):
    check("_detect_domain: empty without a domain", lc._detect_domain() == "")


# ── wait_for_ssh_command(): retries until the command succeeds or the deadline passes ──
clock = [0.0]
sleeps = []
with mock.patch.object(lc.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 255),
                                                           subprocess.CompletedProcess([], 1),
                                                           subprocess.CompletedProcess([], 0)]) as run, \
        mock.patch.object(lc.time, "monotonic", side_effect=lambda: clock[0]), \
        mock.patch.object(lc.time, "sleep", side_effect=lambda t: sleeps.append(t) or clock.__setitem__(0, clock[0] + t)):
    ok = lc.wait_for_ssh_command("10.0.0.7", "test -f /x", timeout=60, interval=5)
check("wait_for_ssh_command: True once the command exits 0, after failed SSH and command attempts",
      ok is True and run.call_count == 3 and sleeps == [5, 5])
check("wait_for_ssh_command: runs the command as root in batch mode",
      run.call_args_list[0][0][0][-2:] == ["root@10.0.0.7", "test -f /x"] and "BatchMode=yes" in run.call_args_list[0][0][0])
clock[0] = 0.0
sleeps.clear()
with mock.patch.object(lc.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run, \
        mock.patch.object(lc.time, "monotonic", side_effect=lambda: clock[0]), \
        mock.patch.object(lc.time, "sleep", side_effect=lambda t: sleeps.append(t) or clock.__setitem__(0, clock[0] + t)):
    ok = lc.wait_for_ssh_command("10.0.0.7", "false", timeout=12, interval=5)
check("wait_for_ssh_command: False after the timeout, without sleeping past it", ok is False and sum(sleeps) <= 12)

# ── boot_id(), wait_for_reboot(), pending_snapshot(), reboot_into_pending_snapshot() ──
with mock.patch.object(lc, "ssh_run", return_value=subprocess.CompletedProcess([], 0, stdout="abc-123\n")):
    check("boot_id: the stripped boot ID", lc.boot_id("vm1") == "abc-123")
with mock.patch.object(lc, "ssh_run", return_value=subprocess.CompletedProcess([], 255, stdout="")):
    check("boot_id: empty when SSH fails", lc.boot_id("vm1") == "")
with mock.patch.object(lc, "wait_for_ssh_command", return_value=True) as wait:
    lc.wait_for_reboot("vm1", "abc-123", timeout=30)
check("wait_for_reboot: waits for a boot ID other than the old one",
      "/proc/sys/kernel/random/boot_id" in wait.call_args[0][1] and "!= abc-123" in wait.call_args[0][1]
      and wait.call_args[1]["timeout"] == 30)
with mock.patch.object(lc, "ssh_run", return_value=subprocess.CompletedProcess([], 1)) as run:
    check("pending_snapshot: False when the check fails", lc.pending_snapshot("vm1") is False)
check("pending_snapshot: needs transactional-update and a default snapshot that is not the running one",
      "/usr/sbin/transactional-update" in run.call_args[0][1] and "'no,yes'" in run.call_args[0][1])
with mock.patch.object(lc, "pending_snapshot", return_value=False), mock.patch.object(lc, "ssh_run") as run:
    check("reboot_into_pending_snapshot: no reboot when nothing is pending",
          lc.reboot_into_pending_snapshot("vm1") is False and run.call_count == 0)
with mock.patch.object(lc, "pending_snapshot", return_value=True), \
        mock.patch.object(lc, "boot_id", return_value="abc-123"), \
        mock.patch.object(lc, "ssh_run") as run, \
        mock.patch.object(lc, "wait_for_reboot", return_value=True) as wait:
    rebooted = lc.reboot_into_pending_snapshot("vm1")
check("reboot_into_pending_snapshot: reboots and waits for a new boot ID",
      rebooted is True and run.call_args[0][1] == "systemctl reboot" and wait.call_args[0][:2] == ("vm1", "abc-123"))
with mock.patch.object(lc, "pending_snapshot", return_value=True), \
        mock.patch.object(lc, "boot_id", return_value="abc-123"), \
        mock.patch.object(lc, "ssh_run"), \
        mock.patch.object(lc, "wait_for_reboot", return_value=False):
    died = False
    try:
        lc.reboot_into_pending_snapshot("vm1", timeout=5)
    except SystemExit:
        died = True
check("reboot_into_pending_snapshot: dies when the host does not come back", died)
with mock.patch.object(lc, "wait_for_ssh_command", return_value=True) as wait:
    lc.wait_for_transactional_update_lock("vm1", timeout=30)
check("wait_for_transactional_update_lock: free when the lock file is absent or its PID is gone",
      "[ ! -e /run/transactional-update.pid ]" in wait.call_args[0][1] and "kill -0" in wait.call_args[0][1])
with mock.patch.object(lc, "wait_for_ssh_command", return_value=False):
    died = False
    try:
        lc.wait_for_transactional_update_lock("vm1", timeout=5)
    except SystemExit:
        died = True
check("wait_for_transactional_update_lock: dies when the lock is still held after the timeout", died)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab_creation provisioning checks passed")
