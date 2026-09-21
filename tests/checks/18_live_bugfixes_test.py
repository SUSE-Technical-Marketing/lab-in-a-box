#!/usr/bin/env python3
# Regression tests for bugs live disposable-VM smoke testing on
# nuc6.mydemo.lab surfaced during the python_migration cutover (2026-08-28),
# fixed in the same session. No live host needed here — SSH/subprocess are
# mocked. Run from 18_live_bugfixes.sh, in its own container — see
# tests/run_tests.sh.
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

# Several blocks below do `backends.subprocess.run = _fake_...` — since
# `backends.subprocess` IS the same shared subprocess module object (not a
# copy), that reassigns subprocess.run GLOBALLY for the rest of this
# process, not just within backends.py. Captured here, before any of those
# run, so process_template()'s own real subprocess.run(["bash", "-c", ...])
# call (genuine heredoc rendering, nothing to mock) can be restored around
# any later prepare_install_iso() test that needs it to actually execute.
_real_subprocess_run = subprocess.run

# Tolerant of pyyaml not being installed in this container — same pattern
# already used in 11_primary_test.py/13_addon_common_test.py/
# 30_setup_harvester_cluster_test.py.
try:
    import yaml as _yaml
    _has_yaml = True
except ImportError:
    _has_yaml = False

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import services  # noqa: E402
import backends  # noqa: E402
import lab_creation as lc  # noqa: E402
import mgradm_common  # noqa: E402

sys.path.insert(0, str(_REPO / "scripts"))
import install_uyuni  # noqa: E402
import install_smlm  # noqa: E402
import install_postgresql  # noqa: E402
import install_struts_demo  # noqa: E402
import install_wordpress  # noqa: E402
import install_smlm_proxy  # noqa: E402

# This test container has no local virsh/virt-install — force
# run_libvirt_tool()'s local branch so the mocked subprocess.run below is
# what actually gets called (matching the real automation VM host, which
# always has these tools locally), not the SSH-fallback branch. See
# 10_lab_creation_core_test.py's identical patch for the full rationale.
lc._has_local_binary = lambda binary: True

# add_to_dns/del_from_dns/add_service_dns also touch a LOCAL zone file
# (NAMED_ZONE_DIR / "<domain>.lan") — real absolute paths under
# /var/lib/named, which must never be touched for real in this containerized
# test. Point it at a scratch tempdir instead.
services.NAMED_ZONE_DIR = Path(tempfile.mkdtemp())

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeSSH:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []

    def __call__(self, hostname, cmd, **kwargs):
        self.calls.append((hostname, cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeResult()


# ── Bug 1: an unreachable secondary DNS server must not abort provisioning ──
# (found live: REMOTE_DNS_SERVERS' second host was down — "No route to
# host" — and add_to_dns raised RuntimeError, aborting VM creation entirely,
# even though bash's own add_to_dns has no such check and just moves on.)
def _dying_ssh(hostname, cmd, **kwargs):
    if kwargs.get("check", True):
        raise RuntimeError("SSH command failed (rc=255) on {}: no route to host".format(hostname))
    return FakeResult(returncode=255)


services.ssh_run = _dying_ssh
svc = services.DNSService()
svc.restart_named = lambda: None  # avoid a real local `systemctl restart named`

try:
    svc.add_to_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                    remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.add_to_dns: an unreachable secondary DNS server does not raise", ok)

try:
    svc.del_from_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                      remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.del_from_dns: an unreachable secondary DNS server does not raise", ok)

try:
    svc.add_service_dns(
        {"nodes": {"vm1": {"myip": "192.168.88.10", "kcluster": "c1"}}},
        "c1", "rke2", "cluster1", "mydemo.lab", remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.add_service_dns: an unreachable secondary DNS server does not raise", ok)

# And confirm it's genuinely check=False being passed, not just a lucky
# no-op — every remote-server SSH call the two DNS-mutating paths make must
# explicitly opt out of raising.
fake = FakeSSH()
services.ssh_run = fake
svc.add_to_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                remote_dns_servers=["1.2.3.4"])
remote_calls = [kw for h, c, kw in fake.calls if h == "1.2.3.4"]
check("DNSService.add_to_dns: every remote-server call passes check=False",
      len(remote_calls) >= 3 and all(kw.get("check") is False for kw in remote_calls))


# ── Bug 2: --qemu-commandline must be one argv element, not two ─────────────
# (found live: virt-install rejected the two-element ["--qemu-commandline",
# "-fw_cfg ..."] form with "expected one argument" — argparse saw the
# leading "-" in the value and treated it as a new flag. bash's own
# equivalent uses --qemu-commandline="..." — a single token — precisely to
# avoid this.)
subproc_calls = []


def _fake_run(args, **kwargs):
    subproc_calls.append(args)
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_run
backend = backends.LibvirtBackend(
    "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa",
    remote_host="hv1", iso_loc="/iso", vm_img_loc="/var/lib/libvirt/images",
    lab_setup_path="/srv/www/htdocs/lab_creation",
)
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1",
)
argv = subproc_calls[-1]
check("create_vm (ignition+combustion): --qemu-commandline is a single argv element",
      "--qemu-commandline" not in argv)
qemu_arg = next((a for a in argv if a.startswith("--qemu-commandline=")), None)
check("create_vm (ignition+combustion): --qemu-commandline=<value> form is used", qemu_arg is not None)
check("create_vm (ignition+combustion): value carries both fw_cfg entries",
      qemu_arg is not None and "opt/com.coreos/config" in qemu_arg and "opt/org.opensuse.combustion/script" in qemu_arg)


# ── disk_format="raw": the USB-delivery lab-host VM's own disk needs to be
# dd-able directly onto a USB block device afterward, which QCOW2's own
# container format doesn't allow — a deliberate, narrow exception to this
# project's usual QCOW2-everywhere convention. Default stays qcow2,
# byte-for-byte unchanged.
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1",
)
disk_arg = next(a for a in subproc_calls[-1] if a.startswith("size="))
check("create_vm: default disk_format is still qcow2, no driver.type override",
      ".qcow2" in disk_arg and "driver.type" not in disk_arg)

subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1", disk_format="raw",
)
disk_arg = next(a for a in subproc_calls[-1] if a.startswith("size="))
check("create_vm: disk_format='raw' uses a .raw path with an explicit driver.type",
      ".raw" in disk_arg and ".qcow2" not in disk_arg and "driver.type=raw" in disk_arg)


# ── vm_machine: a 2015-era CentOS 7 GenericCloud image (kernel 3.10.0-229)
# hangs in a dracut emergency shell ("Not all disks have been found") when
# booted under virt-install's own q35 default — confirmed live 2026-09-02
# on a completely unmodified clone of the source image (so this is a
# genuine chipset/old-kernel incompatibility, not anything config_method-
# specific). --machine pc (legacy i440fx) boots it cleanly. Left empty by
# default so every already-working image is unaffected.
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="virt_customize", ign_file="vm1.ign", com_file="vm1",
)
check("create_vm: no vm_machine given -> no --machine flag at all, virt-install picks its own default",
      "--machine" not in subproc_calls[-1])

subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="virt_customize", ign_file="vm1.ign", com_file="vm1", vm_machine="pc",
)
argv = subproc_calls[-1]
check("create_vm: vm_machine='pc' adds --machine pc to the virt-install invocation",
      "--machine" in argv and argv[argv.index("--machine") + 1] == "pc")


# ── config_method="install_iso" (Ubuntu autoinstall): two real bugs found
# live 2026-09-03, back to back, on the same VM:
#
# 1. Boot order: a bare `--cdrom PATH` (no explicit order) alongside the
#    disk's old boot.order=2 left SeaBIOS with only the (empty) disk in its
#    boot list — it booted straight into "Boot failed: not a bootable disk /
#    No bootable device" and sat there for the VM's entire lifetime (zero
#    installer output, zero network activity, ~11 minutes of real CPU time
#    spread across 18 hours of wall-clock, confirmed via `virsh screenshot`).
#
# 2. Once the boot order was fixed and the installer actually started,
#    subiquity found and parsed the autoinstall config fine but then stopped
#    at an interactive prompt — "Confirmation is required to continue. Add
#    'autoinstall' to your kernel command line to avoid this. Continue with
#    autoinstall? (yes|no)" — and sat there forever with --noautoconsole and
#    nobody at the console (also confirmed via `virsh screenshot`). Subiquity
#    gates unattended mode on literally seeing "autoinstall" on
#    /proc/cmdline, regardless of the seed config's own content. The normal
#    fix (`--location URL` + `--extra-args autoinstall`) doesn't work here:
#    `--extra-args` only applies to a `--location` boot, and `--location`
#    itself only works for install trees the *client* can read directly — a
#    local path on the remote hypervisor fails with "Cannot access install
#    tree on remote connection".
#
# Both fixed together: extract the ISO's own casper/vmlinuz+initrd on the
# hypervisor (xorriso, no mount needed) and boot them directly via
# `--boot kernel=,initrd=,cmdline=autoinstall` — a boot mechanism entirely
# separate from cdrom/hd boot order, so it settles bug 1 too. --cdrom stays
# attached as a device (the extracted initrd's own init script mounts it as
# the install source once booted).
_real_os_unlink = backends.os.unlink
backends.os.unlink = lambda path: None  # mkisofs/scp are mocked, so the seed
                                          # .iso this branch tries to unlink
                                          # after "uploading" was never really
                                          # created — avoid a real ENOENT.
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="install_iso", install_type="autoinstall",
    iso_image="ubuntu-24.04-live-server-amd64.iso", iso_loc="/iso",
)
backends.os.unlink = _real_os_unlink
extract_call = next(c for c in subproc_calls if "xorriso" in c[-1])
check("create_vm (autoinstall): extracts vmlinuz+initrd from the ISO via xorriso (no mount needed)",
      "-extract /casper/vmlinuz" in extract_call[-1] and "-extract /casper/initrd" in extract_call[-1]
      and "/iso/ubuntu-24.04-live-server-amd64.iso" in extract_call[-1])
install_call = next(c for c in subproc_calls if "virt-install" in c[0])
boot_arg = install_call[install_call.index("--boot") + 1]
check("create_vm (autoinstall): --boot carries kernel=, initrd=, and cmdline=autoinstall",
      "kernel=" in boot_arg and "initrd=" in boot_arg and "cmdline=autoinstall" in boot_arg)
check("create_vm (autoinstall): --cdrom is still attached (the initrd mounts it as the install source)",
      "--cdrom" in install_call
      and "ubuntu-24.04-live-server-amd64.iso" in install_call[install_call.index("--cdrom") + 1])
check("create_vm (autoinstall): no disk carries a per-device boot.order= "
      "(direct kernel boot bypasses cdrom/hd boot order entirely)",
      not any("boot.order" in a for a in install_call))

# ── config_method="install_iso" (autoinstall): post-install boot reset.
# Found live 2026-09-03, immediately after fixing the confirmation-prompt
# bug above: virt-install's own "Restarting guest" step (part of --wait -1
# finishing) brought the domain back up on the exact same kernel/initrd/
# cmdline as the installer boot, since nothing about that lower-level --boot
# mechanism knows the install is now done. The freshly-installed VM booted
# straight back into the live installer's initrd hunting for a live
# filesystem on /dev/sr0 and hung at "Attempt interactive netboot from a
# URL?" forever — confirmed via `virsh screenshot`. A --location-based
# install wouldn't need any of this (virt-install's own installer-aware
# machinery resets the boot config itself), but --location doesn't work over
# a remote hypervisor connection here (see the comment above). Fixed by
# destroying the auto-restarted domain, resetting it to plain disk boot via
# virt-xml, detaching the now-stale seed cdrom, then starting it for real —
# --edit on a *running* domain only touches the offline definition, so the
# destroy has to come first or the very next start just reboots the old
# (bad) config again (also confirmed live).
calls_after_install = subproc_calls[subproc_calls.index(install_call) + 1:]
destroy_call = next((c for c in calls_after_install if "destroy" in c), None)
edit_call = next((c for c in calls_after_install if "--edit" in c), None)
remove_call = next((c for c in calls_after_install if "--remove-device" in c), None)
start_call = next((c for c in calls_after_install if "start" in c), None)
check("create_vm (autoinstall): destroys the auto-restarted domain before touching its boot config",
      destroy_call is not None)
check("create_vm (autoinstall): virt-xml --edit resets boot to kernel=,initrd=,cmdline=,hd",
      edit_call is not None
      and edit_call[edit_call.index("--boot") + 1] == "kernel=,initrd=,cmdline=,hd")
check("create_vm (autoinstall): virt-xml --remove-device detaches the stale seed cdrom",
      remove_call is not None and "seed_vm1.iso" in remove_call[remove_call.index("--disk") + 1])
check("create_vm (autoinstall): destroy happens before the boot-config edit",
      destroy_call is not None and edit_call is not None
      and calls_after_install.index(destroy_call) < calls_after_install.index(edit_call))
check("create_vm (autoinstall): the domain is started again after the boot-config reset",
      start_call is not None and edit_call is not None
      and calls_after_install.index(edit_call) < calls_after_install.index(start_call))

# ── xorriso extraction command must quote its vm_name-derived paths ─────────
# Found in code review 2026-09-05: the very next line after this extraction
# (the cleanup `rm -f '{seed}' '{vmlinuz}' '{initrd}'`) already single-quotes
# these same paths, but the extraction command that builds vmlinuz_remote/
# initrd_remote in the first place did not — and both embed vm_name, a lab.
# json node hostname never validated against shell metacharacters anywhere
# in this codebase. Run over ssh_run(), which hands the whole string to the
# remote shell, an unquoted vm_name containing a space (or worse) could
# break — or inject into — this command.
backends.os.unlink = lambda path: None
subproc_calls.clear()
backend.create_vm(
    "two words", "2", "4096", "40", "network=default,model=virtio",
    config_method="install_iso", install_type="autoinstall",
    iso_image="ubuntu-24.04-live-server-amd64.iso", iso_loc="/iso",
)
backends.os.unlink = _real_os_unlink
extract_call = next(c for c in subproc_calls if "xorriso" in c[-1])
check("create_vm (autoinstall): xorriso extraction quotes the vm_name-derived vmlinuz/initrd paths",
      "'/var/lib/libvirt/images/two words_vmlinuz'" in extract_call[-1]
      and "'/var/lib/libvirt/images/two words_initrd'" in extract_call[-1])
check("create_vm (autoinstall): xorriso extraction quotes the ISO source path too",
      "'/iso/ubuntu-24.04-live-server-amd64.iso'" in extract_call[-1])


# ── kickstart/autoyast/preseed (--location-based installs): two real bugs found
# live 2026-09-17, back to back, the first time this path was actually
# live-tested end to end. (1) --location with a bare hypervisor-local path
# fails ("Cannot access install tree on remote connection") whenever
# virt-install runs on a different host than the hypervisor — fixed by
# ensure_iso_install_tree() (see its own tests in
# 10_lab_creation_core_test.py); this block only verifies backends.py's own
# call site actually uses its return value as --location. (2) once that was
# fixed and a real kickstart install actually ran end to end, the domain came
# back up on plain SeaBIOS/legacy firmware and failed to boot ("Boot failed:
# not a bootable disk") — this --location-based branch builds its OWN argv
# from scratch (not base_args above) and had never included --boot at all,
# silently ignoring VM_BOOT (every lab JSON in this project defaults to
# "uefi") regardless of what firmware the guest's own kickstart bootloader
# step assumed.
_real_ensure_iso_install_tree = backends.ensure_iso_install_tree
iso_tree_calls = []
backends.ensure_iso_install_tree = lambda remote_host, iso_loc, iso_image: (
    iso_tree_calls.append((remote_host, iso_loc, iso_image))
    or "http://hv1:8890/{}/".format(iso_image))
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="install_iso", install_type="kickstart",
    iso_image="rhel-10.2-x86_64-dvd.iso", iso_loc="/iso", boot="uefi",
)
backends.ensure_iso_install_tree = _real_ensure_iso_install_tree
check("create_vm (kickstart): calls ensure_iso_install_tree with the hypervisor/iso_loc/iso_image",
      iso_tree_calls == [("hv1", "/iso", "rhel-10.2-x86_64-dvd.iso")])
install_call = next(c for c in subproc_calls if "virt-install" in c[0])
check("create_vm (kickstart): --location uses ensure_iso_install_tree's own URL, not a bare path",
      install_call[install_call.index("--location") + 1] == "http://hv1:8890/rhel-10.2-x86_64-dvd.iso/")
check("create_vm (kickstart): --boot carries the resolved boot_flag (matches VM_BOOT, "
      "not virt-install's own legacy-BIOS default)",
      "--boot" in install_call and install_call[install_call.index("--boot") + 1] == "uefi")
check("create_vm (kickstart): --extra-args still carries the real inst.ks= URL",
      "inst.ks=" in install_call[install_call.index("--extra-args") + 1])
check("create_vm (kickstart): --extra-args carries inst.text — confirmed live 2026-09-17 "
      "that without it, RHEL10's own Anaconda silently tries to start its default "
      "graphical/WebUI path in a --noautoconsole environment and hangs forever with "
      "zero further disk/network activity, no error at all",
      "inst.text" in install_call[install_call.index("--extra-args") + 1])
check("create_vm (kickstart): --extra-args carries TERM=vt100 — confirmed live 2026-09-17, "
      "with hard evidence (real disk writes/CPU time appearing only after manually "
      "sending one arbitrary keystroke to the guest's serial console): Anaconda's "
      "newt/slang text UI queries the terminal's capabilities on startup and blocks "
      "forever waiting for a reply nothing is attached (--noautoconsole) to ever send",
      "TERM=vt100" in install_call[install_call.index("--extra-args") + 1])


# ── prepare_install_iso() autoinstall hostname: found live 2026-09-03, on the
# same VM as the two bugs above, once it actually finished installing and
# booted the real (fixed) disk — `hostname` inside the freshly-installed,
# fully SSH-reachable VM read back "localhost", not "venus.mydemo.lab". The
# autoinstall user-data deliberately has no `identity:` section (it would
# force a separate default user this project doesn't want — root-only
# access is the point), but `identity` is autoinstall's only mechanism for
# setting /etc/hostname at install time, so without it curtin just leaves
# whatever the live installer environment defaulted to. meta-data's
# local-hostname doesn't help either — that's a cloud-init concept, and the
# seed cdrom (cloud-init's own NoCloud datasource) is detached again right
# after this install finishes, so nothing ever re-reads it on a later real
# boot. Fixed with an explicit late-command, the same mechanism already used
# two lines above it for the sshd config.
pubkey_path = Path("/root/.ssh/id_rsa.pub")
pubkey_path.parent.mkdir(parents=True, exist_ok=True)
if not pubkey_path.exists():
    pubkey_path.write_text("ssh-rsa AAAAtest test@test\n")
with tempfile.TemporaryDirectory() as tmp:
    lc.prepare_install_iso(
        "venus.mydemo.lab", tmp, "autoinstall", "ubuntu-24.04-live-server-amd64.iso",
        "52:54:00:aa:bb:cc", "192.168.88.116", "24", "192.168.88.1", "192.168.88.73",
        "mydemo.lab", "x",
    )
    autoinstall_user_data = (Path(tmp) / "install_iso" / "venus.mydemo.lab" / "user-data").read_text()
check("prepare_install_iso (autoinstall): a late-command sets /etc/hostname to the real node name",
      'echo "venus.mydemo.lab" > /target/etc/hostname' in autoinstall_user_data)

# vm_name is quoted in that late-command — found in code review 2026-09-05:
# this string runs as a real shell command inside the install target, and
# vm_name (a lab.json node hostname) is never validated against shell
# metacharacters anywhere in this codebase. A name with an embedded space
# must stay one shell word, not become "echo two words > ..." unquoted.
with tempfile.TemporaryDirectory() as tmp:
    lc.prepare_install_iso(
        "two words", tmp, "autoinstall", "ubuntu-24.04-live-server-amd64.iso",
        "52:54:00:aa:bb:cc", "192.168.88.116", "24", "192.168.88.1", "192.168.88.73",
        "mydemo.lab", "x",
    )
    autoinstall_user_data = (Path(tmp) / "install_iso" / "two words" / "user-data").read_text()
check("prepare_install_iso (autoinstall): the hostname late-command quotes vm_name",
      'echo "two words" > /target/etc/hostname' in autoinstall_user_data)

# ── prepare_install_iso (autoinstall): the #cloud-config YAML must escape
# its own network/credential values, not just quote (or not even quote) them
# raw ─────────────────────────────────────────────────────────────────────
# Found in code review 2026-09-05, confirmed by direct execution: myip/
# mymask/mygw/mydns/mydomain were bare (not even hand-quoted) YAML scalars,
# and root_pwd_hash/root_ssh_pubkey were hand-quoted but not escaped — the
# exact same bug already found and fixed in setup_harvester_cluster.py's
# own hand-built YAML, just worse here (some fields had no quoting at all).
# A mydomain value with an embedded colon+newline injected two new,
# unrelated top-level keys straight into the rendered document.
malicious_domain = "mydemo.lab]\nssh_pwauth: false\nfake_key: injected"
with tempfile.TemporaryDirectory() as tmp:
    lc.prepare_install_iso(
        "venus.mydemo.lab", tmp, "autoinstall", "ubuntu-24.04-live-server-amd64.iso",
        "52:54:00:aa:bb:cc", "192.168.88.116", "24", "192.168.88.1", "192.168.88.73",
        malicious_domain, "x",
    )
    autoinstall_user_data = (Path(tmp) / "install_iso" / "venus.mydemo.lab" / "user-data").read_text()
if _has_yaml:
    parsed = _yaml.safe_load(autoinstall_user_data)
    check("prepare_install_iso (autoinstall): a mydomain value with an embedded colon+newline "
          "is preserved as DATA, not parsed as new YAML structure — only 'autoinstall' is a "
          "top-level key, nothing got injected",
          list(parsed.keys()) == ["autoinstall"])
    check("prepare_install_iso (autoinstall): the malicious value itself is still there, intact",
          malicious_domain in parsed["autoinstall"]["network"]["network"]["ethernets"]["id0"]
          ["nameservers"]["search"])
else:
    check("prepare_install_iso (autoinstall): the malicious value's own colon+newline never "
          "appears un-escaped in the rendered YAML (substring check, no pyyaml)",
          "\nssh_pwauth: false" not in autoinstall_user_data)

# ── prepare_install_iso (kickstart/autoyast): must inject ROOT_SSH_PUBKEY
# (the automation VM's own real, current key), not the raw ROOT_SSH_KEY
# config-file value ─────────────────────────────────────────────────────
# Confirmed live 2026-09-17: lab_creation.cfg's ROOT_SSH_KEY had drifted from
# this automation VM's actual ~/.ssh/id_rsa.pub. Kickstart/autoyast echoed
# ROOT_SSH_KEY straight into authorized_keys (unlike ignition/combustion/
# cloud-init/preseed, which all end up using the real id_rsa.pub) — the VM
# provisioned fine but was permanently SSH-unreachable ("Permission denied
# (publickey)"), for deimos.mydemo.lab specifically and for every other
# kickstart/autoyast install generally. A stale/mismatched ROOT_SSH_KEY
# value must never end up in the rendered answer file again.
_stale_config_key = "ssh-rsa AAAAstaleconfigkey stale@config"
_saved_subprocess_run = subprocess.run
subprocess.run = _real_subprocess_run  # process_template() genuinely shells out — nothing to mock
try:
    for _itype, _ext in (("kickstart", "ks"), ("autoyast", "xml")):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "install_iso").mkdir(parents=True)
            real_tpl = _REPO / "templates" / "install_iso.template_{}".format(_itype)
            (Path(tmp) / "install_iso" / "template_{}".format(_itype)).write_text(real_tpl.read_text())
            lc.prepare_install_iso(
                "deimos.mydemo.lab", tmp, _itype, "rhel-10.2-x86_64-dvd.iso",
                "52:54:00:aa:bb:cc", "192.168.88.146", "24", "192.168.88.1", "192.168.88.73",
                "mydemo.lab", "x", root_ssh_key=_stale_config_key,
            )
            rendered = (Path(tmp) / "install_iso" / "deimos.mydemo.lab.{}".format(_ext)).read_text()
        check("prepare_install_iso ({}): the automation VM's real pubkey is injected".format(_itype),
              pubkey_path.read_text().strip() in rendered)
        check("prepare_install_iso ({}): a stale/mismatched ROOT_SSH_KEY config value is NOT "
              "injected".format(_itype),
              _stale_config_key not in rendered)
finally:
    subprocess.run = _saved_subprocess_run


# ── copy_vm_image / disk_format: found live on nuc6 (2026-08-31) — create_vm's
# disk_format="raw" landed its own new disk at <vm_name>.raw, but
# copy_vm_image() (which actually populates the disk with the source
# image's content, called BEFORE create_vm) still unconditionally wrote to
# <vm_name>.qcow2 regardless — create_vm's --import would then have found
# nothing at .raw and silently created a blank, non-bootable disk instead
# of using the copied image. Also: a plain `cp` renamed to .raw would not
# even be a valid raw disk (the source is genuinely QCOW2-container
# content) — needs a real `qemu-img convert -O raw`, not a rename.
subproc_calls.clear()
backend.copy_vm_image("source.qcow2", "vm1", "40", config_method="cloud-init")
cmds = [" ".join(c) for c in subproc_calls]
check("copy_vm_image: default disk_format still plain `cp` to <vm_name>.qcow2, unchanged",
      any("cp" in c and "vm1.qcow2" in c for c in cmds)
      and not any("qemu-img convert" in c for c in cmds))
check("copy_vm_image: default disk_format resizes with -f qcow2",
      any("qemu-img resize -f qcow2" in c and "vm1.qcow2" in c for c in cmds))
check("copy_vm_image: default disk_format never runs a GPT repair (qcow2 disks aren't grown post-copy)",
      not any("sgdisk" in c for c in cmds))

subproc_calls.clear()
backend.copy_vm_image("source.qcow2", "vm1", "40", config_method="cloud-init", disk_format="raw")
cmds = [" ".join(c) for c in subproc_calls]
check("copy_vm_image: disk_format='raw' converts (not cp's) the source into <vm_name>.raw",
      any("qemu-img convert -O raw" in c and "source.qcow2" in c and "vm1.raw" in c for c in cmds))
check("copy_vm_image: disk_format='raw' never does a plain cp of the source image",
      not any("cp /iso/source.qcow2" in c for c in cmds))
check("copy_vm_image: disk_format='raw' resizes with -f raw against the .raw path",
      any("qemu-img resize -f raw" in c and "vm1.raw" in c for c in cmds))
# GPT backup header/table repair: growing a raw GPT-partitioned disk with
# qemu-img resize leaves the backup GPT structures at the old end of the
# disk instead of the new one — confirmed live 2026-09-01 as the likely
# cause of a lab-host VM's root filesystem appearing to reset to its
# pristine first-boot Btrfs snapshot after a reboot (dmesg: "GPT: Use GNU
# Parted to correct GPT errors."). sgdisk -e moves them back to the end.
check("copy_vm_image: disk_format='raw' repairs the GPT backup header/table after resize",
      any("sgdisk -e" in c and "vm1.raw" in c for c in cmds))


# ── Bug 3: delete_vm must not undefine the domain before removing storage ───
# (found live, twice: a bare `undefine --nvram` succeeded regardless of
# whether the domain was running, removing its definition before the real
# `undefine --nvram --remove-all-storage` call ever ran — which then failed
# with "domain not found", leaving the qcow2 file on disk. Fixed by dropping
# the redundant first undefine: `destroy` alone guarantees a stopped domain
# without touching its definition, so the one remaining undefine call always
# finds it and removes storage too.)
virsh_calls = []


def _fake_virsh_run(args, **kwargs):
    virsh_calls.append(args)
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_run
backend.delete_vm("vm1")
check("delete_vm: issues exactly 2 virsh calls (destroy, then undefine+remove-all-storage)",
      len(virsh_calls) == 2)
check("delete_vm: never calls a bare 'undefine' without --remove-all-storage",
      not any("undefine" in a and "--remove-all-storage" not in a for a in virsh_calls))
check("delete_vm: destroy runs before the storage-removing undefine",
      "destroy" in virsh_calls[0] and "undefine" in virsh_calls[1])
check("delete_vm: the undefine call includes --remove-all-storage",
      "--remove-all-storage" in virsh_calls[1])


# ── Bug 4: reboot_vm must prefer a direct guest reboot over virsh/ACPI ──────
# (found live, on a fresh Uyuni server install: virsh's ACPI-triggered
# `reboot` routinely fails to produce a lifecycle event within 120s in this
# nested-virt environment. The ORIGINAL fallback, an immediate hard `reset`,
# is confirmed live — reproduced TWICE on two separate disposable VMs, even
# with a guest-side `sync` added first — to silently lose a just-installed
# transactional-update snapshot: `transactional-update pkg install` returns
# and correctly marks the new snapshot as default, but a `reset` (the
# hardware reset line, not a guest/qemu-mediated shutdown) can still boot
# back into the OLD snapshot. A first fix escalated through ACPI `reboot`
# then ACPI `shutdown` before falling back to `reset` — confirmed live this
# never loses a snapshot, but ACPI signals routinely never reach the guest
# in this environment either, so it still fell through to `reset` most of
# the time. What actually works, confirmed live: a plain `ssh vm "reboot"`
# — bypassing ACPI-signal-forwarding through qemu entirely — completed in
# ~15s on a VM where the ACPI path had just failed twice in a row. Fixed:
# reboot via SSH directly whenever the guest is reachable; only fall back
# to the virsh ACPI/reset escalation when it isn't.)
sync_ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kwargs):
    sync_ssh_calls.append((hostname, cmd, kwargs))
    return FakeResult(returncode=0)


class _FakeSocket:
    def close(self):
        pass


# Happy path: the guest is reachable over SSH -> reboots directly, never
# touches virsh at all.
virsh_calls.clear()
sync_ssh_calls.clear()
backends.ssh_run = _fake_ssh_run
backends.subprocess.run = _fake_virsh_run
backends.socket.create_connection = lambda addr, timeout=None: _FakeSocket()
backend.reboot_vm("vm1")
check("reboot_vm: reboots directly over SSH when the guest is reachable",
      any(h == "vm1" and c == "sync && reboot" for h, c, kw in sync_ssh_calls))
check("reboot_vm: the SSH reboot command is check=False (a dropped connection is expected)",
      all(kw.get("check") is False for h, c, kw in sync_ssh_calls))
check("reboot_vm: never touches virsh when the guest is reachable over SSH",
      len(virsh_calls) == 0)

# The guest is NOT reachable over SSH -> falls back to virsh's ACPI reboot,
# which succeeds this time -> nothing further needed.
virsh_calls.clear()
sync_ssh_calls.clear()


def _unreachable(addr, timeout=None):
    raise OSError("connection refused")


backends.socket.create_connection = _unreachable
backends.subprocess.run = _fake_virsh_run
backend.reboot_vm("vm1")
check("reboot_vm: falls back to virsh when the guest isn't reachable over SSH",
      len(sync_ssh_calls) == 0 and any("reboot" in a for a in virsh_calls))
check("reboot_vm: unreachable-guest happy path never calls shutdown or reset",
      not any("shutdown" in a or "reset" in a for a in virsh_calls))

# Unreachable guest, AND the ACPI reboot's event never arrives, but the
# escalated ACPI shutdown's event DOES -> starts the domain back up, never
# touches `reset`.
virsh_calls.clear()


def _fake_virsh_reboot_fails_shutdown_works(args, **kwargs):
    virsh_calls.append(args)
    if "event" in args:
        # First event call is for the reboot; second is for the shutdown.
        is_first = sum(1 for a in virsh_calls if "event" in a) == 1
        return FakeResult(returncode=1 if is_first else 0)
    if "domstate" in args:
        return FakeResult(returncode=0, stdout="shut off\n")
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_reboot_fails_shutdown_works
backend.reboot_vm("vm1")
check("reboot_vm: escalates to a graceful shutdown+start cycle, not reset",
      any("shutdown" in a for a in virsh_calls) and any("start" in a for a in virsh_calls)
      and not any("reset" in a for a in virsh_calls))

# Unreachable guest, and neither the reboot's nor the shutdown's lifecycle
# event arrives, and domstate never reports "shut off" either -> only THEN
# falls back to a hard reset, as a genuine last resort.
virsh_calls.clear()


def _fake_virsh_run_no_event(args, **kwargs):
    virsh_calls.append(args)
    if "event" in args:
        return FakeResult(returncode=1)
    if "domstate" in args:
        return FakeResult(returncode=0, stdout="running\n")
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_run_no_event
backend.reboot_vm("vm1")
check("reboot_vm: still falls back to a hard reset when nothing graceful works",
      any("reset" in a for a in virsh_calls))
check("reboot_vm: tries shutdown before giving up and resetting",
      virsh_calls.index(next(a for a in virsh_calls if "shutdown" in a))
      < virsh_calls.index(next(a for a in virsh_calls if "reset" in a)))

backends.socket.create_connection = socket.create_connection


# ── vm_is_reusable / reboot_vm: their domstate check must use 3.6-compatible
# subprocess.run() kwargs ────────────────────────────────────────────────────
# Found in code review 2026-09-05: both called self._virsh("domstate", ...,
# capture_output=True, text=True) -- Python 3.7+-only kwargs. This project's
# containerized test suite (and even the real automation VM's own bare
# python3) runs Python 3.6, where subprocess.run() rejects those two kwargs
# outright with a TypeError. Not currently reachable from any bare-python3
# entry point (every real caller goes through a python3.11-shebanged
# script), but a landmine for a future one -- fixed to the same
# stdout=PIPE/stderr=PIPE/universal_newlines=True form used everywhere else
# in this codebase.
def _py36_strict_run(args, **kwargs):
    # Mimics real Python 3.6's subprocess.run(): TypeError on either
    # Python-3.7+-only kwarg, exactly what the fixed code must never pass.
    if "capture_output" in kwargs or "text" in kwargs:
        raise TypeError("run() got an unexpected keyword argument (mimics Python 3.6)")
    if "event" in args:
        return FakeResult(returncode=1)  # no lifecycle event -> forces reboot_vm's escalation
    if "domstate" in args:
        return FakeResult(returncode=0, stdout="shut off\n")
    return FakeResult(returncode=0)


backends.subprocess.run = _py36_strict_run
domstate_error = None
try:
    # mymac="" skips the MAC-mismatch branch entirely (_empty(mymac) is
    # True), and state != "running" (from the fake above) returns False
    # immediately after the one call this test cares about -- isolating
    # exactly the domstate check, without needing to also fake DNS/SSH.
    backend.vm_is_reusable("vm1", "", "10.0.0.1")
except TypeError as e:
    domstate_error = e
check("vm_is_reusable: its domstate check never raises TypeError under Python-3.6-strict "
      "subprocess.run kwargs", domstate_error is None)

backends.socket.create_connection = lambda addr, timeout=None: (_ for _ in ()).throw(OSError("unreachable"))
domstate_error = None
try:
    # Guest unreachable over SSH -> falls to the virsh escalation path;
    # reboot/shutdown both "fail" (no event) under this fake -> reaches the
    # domstate check (the second fix) -> "shut off" -> starts the domain.
    backend.reboot_vm("vm1")
except TypeError as e:
    domstate_error = e
check("reboot_vm: its domstate check never raises TypeError under Python-3.6-strict "
      "subprocess.run kwargs", domstate_error is None)
backends.socket.create_connection = socket.create_connection


# ── push_provisioning_files (cloud-init): quote vm_name-derived paths ───────
# Found in code review 2026-09-05: the remote shell command that assembles
# the NoCloud cidata ISO on the hypervisor built its rm-f/-o/mv paths from
# vm_name (a lab.json node hostname, never validated against shell
# metacharacters) unquoted — a name with an embedded space broke those
# paths outright, and a shell metacharacter could inject into the command.
# The "for i in {vm}*" glob and the "${{i/{vm}_/}}" pattern-expansion stay
# unquoted on purpose (mirrors bash's own unquoted-glob behavior — see the
# comment above sources= in push_provisioning_files itself), but every
# other use of vm_name here doesn't need to be a glob and is now
# shlex.quote()'d. This whole cloud-init branch of push_provisioning_files
# had no test coverage at all before this.
rsync_calls = []


def _fake_rsync_run(args, **kwargs):
    rsync_calls.append(args)
    return FakeResult(returncode=0)


with tempfile.TemporaryDirectory() as tmp:
    ci_dir = Path(tmp) / "cloud-init"
    ci_dir.mkdir()
    for suffix in ("user-data", "meta-data", "network-config"):
        (ci_dir / "two words_{}".format(suffix)).write_text("x")

    backend2 = backends.LibvirtBackend(
        "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa",
        remote_host="hv1", iso_loc="/iso", vm_img_loc="/var/lib/libvirt/images",
        lab_setup_path=tmp,
    )
    rsync_calls.clear()
    sync_ssh_calls.clear()
    backends.subprocess.run = _fake_rsync_run
    backend2.push_provisioning_files("two words", config_method="cloud-init")

ci_call = next(c for h, c, kw in sync_ssh_calls if "mkisofs" in c)
check("push_provisioning_files (cloud-init): rm -f target is quoted",
      "rm -f '/var/lib/libvirt/images/two words_ci.iso'" in ci_call)
check("push_provisioning_files (cloud-init): mkisofs -o target is quoted",
      "-o '/tmp/ci_two words.iso'" in ci_call)
check("push_provisioning_files (cloud-init): the final mv's source and destination are both quoted",
      "mv '/tmp/ci_two words.iso' '/var/lib/libvirt/images/two words_ci.iso'" in ci_call)
check("push_provisioning_files (cloud-init): the cp step's variable expansions are quoted",
      'cp "${i}" "/tmp/${i/two words_/}"' in ci_call)


# ── Bug 5: mgradm install's pg_hba/IPv6 race must be pre-empted, not ────────
# recovered from after the fact
# (found live on cutoveruyuni2.mydemo.lab, 2026-08-28: the original recovery
# only patched pg_hba and did `systemctl restart uyuni-server` AFTER
# `mgradm install` had already died — but `mgradm install` itself performs
# schema/org/admin bootstrap, so a plain restart just brings the Tomcat
# process back up against a completely empty database (confirmed directly:
# spacecmd login "Invalid credentials", zero rows in web_contact, zero
# tables at all). And `mgradm install` refuses to simply be re-run once its
# containers/volumes exist ("Server is already initialized!"). Fixed by
# running install in the background and patching pg_hba the moment uyuni-db
# is ready, before uyuni-server's first connection attempt — so the ONE
# install command completes end-to-end.)
# _run_install_with_pg_hba_guard/_ensure_server_container_active now live in
# libs/mgradm_common.py (moved 2026-09-12 — see that module's own docstring:
# `from install_uyuni import ...` broke once install_uyuni.py was deployed
# without its .py suffix). install_uyuni._run_install_with_pg_hba_guard is
# still the SAME function object (aliased back on import), so calling it
# through install_uyuni is unchanged — but its actual ssh_run/time/die come
# from mgradm_common's own module globals now, so that's what needs patching.
mgradm_common.time.sleep = lambda *a, **kw: None

fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=0)),
                          ("cat ", FakeResult(returncode=0, stdout="0\n"))])
mgradm_common.ssh_run = fake
install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman --admin-login admin")
launch_calls = [c for h, c, kw in fake.calls if "nohup" in c]
check("_run_install_with_pg_hba_guard: launches mgradm install in the background",
      len(launch_calls) == 1 and "mgradm install podman" in launch_calls[0])
hba_calls = [c for h, c, kw in fake.calls if "pg_hba_custom.conf" in c]
check("_run_install_with_pg_hba_guard: patches pg_hba once uyuni-db is ready",
      len(hba_calls) == 1)
pg_isready_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if "pg_isready" in c)
hba_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if "pg_hba_custom.conf" in c)
check("_run_install_with_pg_hba_guard: pg_hba patch happens after uyuni-db is confirmed ready",
      hba_idx > pg_isready_idx)

# Timeout: the rc-file marker never appears -> die(), not a silent return.
fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=1))])
mgradm_common.ssh_run = fake
try:
    install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman", timeout=1, poll_interval=1)
    died = False
except SystemExit:
    died = True
check("_run_install_with_pg_hba_guard: dies if the install never finishes within the timeout", died)

# The install command itself reports a non-zero exit in its rc file -> die().
fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=0)),
                          ("cat ", FakeResult(returncode=0, stdout="1\n"))])
mgradm_common.ssh_run = fake
try:
    install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman", timeout=5, poll_interval=1)
    died = False
except SystemExit:
    died = True
check("_run_install_with_pg_hba_guard: dies if mgradm install's own exit code is non-zero", died)

# setup_uyuni(): install_cmd's --organization must be shell-quoted as ONE
# argument. Real bug found live 2026-09-13 in install_smlm.py's identical
# command-construction pattern: an unquoted multi-word --organization
# ("SUSE Test") got split by the remote shell into two tokens, and mgradm
# misinterpreted the stray second word as its own optional FQDN positional
# argument ("Test is not a valid FQDN"), failing the whole install. Same
# latent bug existed here — uyuni_org just never happened to contain a
# space in practice, so it was never hit live.
install_uyuni.ssh_run = FakeSSH()
install_uyuni.reboot_vm = lambda virt_srv, hostname: None
install_uyuni.check_ssh_conn = lambda hostname: None
install_uyuni.time.sleep = lambda s: None
captured_install_cmd = []
install_uyuni._run_install_with_pg_hba_guard = lambda hostname, cmd: captured_install_cmd.append(cmd)
install_uyuni._ensure_server_container_active = lambda hostname: None
install_uyuni.setup_uyuni("host1", "virt1", {"uyuni_org": "SUSE Test"})
check("setup_uyuni(): a multi-word uyuni_org is shell-quoted as ONE argument, not split",
      "--organization 'SUSE Test'" in captured_install_cmd[0])


# ── Bug 6: CLM stuck-build restart-and-retry wrapper ─────────────────────────
# (Round 4 of the CLM stuck-build investigation, 2026-08-28 — see
# MIGRATION_TODO.md, "the Web UI 'Build' button theory, tested and
# disproven": Uyuni's own async CLM align worker can get itself wedged
# after a small, non-deterministic number of builds; the only confirmed
# mitigation is restarting uyuni-server.service and re-triggering the
# same build/promote action. _wait_for_clm_with_restart_retry wraps
# sc.wait_for_content_environment with exactly that recovery, and
# run_clm_actions/_trigger_clm_action carry the validation + dispatch
# that used to live in spacecmd_common.run_content_lifecycle_actions
# before this wrapper needed to intercept it.)

# _trigger_clm_action: build vs promote dispatch, and promote's own
# 'from_env' validation.
build_calls = []
promote_calls = []
install_uyuni.sc.build_content_project = lambda h, e, p, msg: build_calls.append((h, e, p, msg))
install_uyuni.sc.promote_content_project = lambda h, e, p, frm: promote_calls.append((h, e, p, frm))
install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "build", {"message": "m"})
check("_trigger_clm_action: build dispatches to sc.build_content_project with the message",
      build_calls == [("host1", "mgrctl exec --", "proj", "m")])

install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "promote", {"from_env": "dev"})
check("_trigger_clm_action: promote dispatches to sc.promote_content_project with from_env",
      promote_calls == [("host1", "mgrctl exec --", "proj", "dev")])

died = False
try:
    install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "promote", {})
except SystemExit:
    died = True
check("_trigger_clm_action: promote without 'from_env' dies", died)

# _wait_for_clm_with_restart_retry — happy path: reaches a terminal status
# on the very first (short) poll, never restarts, never re-triggers.
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_ok(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    return "built"


install_uyuni.sc.wait_for_content_environment = _fake_wait_ok
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
status = install_uyuni._wait_for_clm_with_restart_retry(
    "host1", "mgrctl exec --", "proj", "build", {}, "dev", timeout=1800, stall_timeout=300)
check("_wait_for_clm_with_restart_retry: happy path returns immediately, no restart",
      status == "built" and len(calls["wait"]) == 1 and len(calls["restart"]) == 0)
check("_wait_for_clm_with_restart_retry: first poll uses the short stall_timeout, not the full timeout, "
      "and does not ask wait_for_content_environment to die on timeout",
      calls["wait"][0] == (300, False))

# Stuck once, then recovers after a restart + retry of the same action.
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_stuck_then_ok(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    return "built" if len(calls["wait"]) > 1 else "building"


install_uyuni.sc.wait_for_content_environment = _fake_wait_stuck_then_ok
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
install_uyuni.time.sleep = lambda s: None
install_uyuni._ensure_server_container_active = lambda hostname, **kw: None
status = install_uyuni._wait_for_clm_with_restart_retry(
    "host1", "mgrctl exec --", "proj", "build", {"message": "m"}, "dev", timeout=1800, stall_timeout=300)
check("_wait_for_clm_with_restart_retry: recovers after one restart+retry", status == "built")
check("_wait_for_clm_with_restart_retry: polls exactly twice (initial + one retry)", len(calls["wait"]) == 2)
check("_wait_for_clm_with_restart_retry: restarts uyuni-server.service before retrying",
      any("systemctl restart uyuni-server.service" in str(c) for c in calls["restart"]))
check("_wait_for_clm_with_restart_retry: re-triggers the same action (build) after restart",
      len(calls["trigger"]) == 1)
check("_wait_for_clm_with_restart_retry: only the LAST poll asks wait_for_content_environment to die on timeout",
      calls["wait"][0][1] is False and calls["wait"][1][1] is True)

# Never recovers -> the final poll's own die_on_timeout=True is what
# actually raises (simulated here, since the real wait_for_content_environment
# implementation's own polling loop isn't what's under test in this file).
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_always_stuck(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    if die_on_timeout:
        raise SystemExit(1)
    return "building"


install_uyuni.sc.wait_for_content_environment = _fake_wait_always_stuck
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
died = False
try:
    install_uyuni._wait_for_clm_with_restart_retry(
        "host1", "mgrctl exec --", "proj", "build", {}, "dev", timeout=1800, stall_timeout=300)
except SystemExit:
    died = True
check("_wait_for_clm_with_restart_retry: dies for real if the retry doesn't recover either",
      died and len(calls["wait"]) == 2)

# run_clm_actions: no-op, validation, and full orchestration (trigger + wait).
install_uyuni.sc.ensure_spacecmd_config = lambda *a, **kw: None
calls = {"trigger": []}
install_uyuni.sc.build_content_project = lambda h, e, p, msg: calls["trigger"].append(("build", p))
install_uyuni.sc.promote_content_project = lambda h, e, p, frm: calls["trigger"].append(("promote", p, frm))
install_uyuni.run_clm_actions("host1", {})
check("run_clm_actions: no-op when uyuni_content_lifecycle_actions is unset", calls["trigger"] == [])

died = False
try:
    install_uyuni.run_clm_actions("host1", {"uyuni_content_lifecycle_actions": [{"project": "p", "action": "bogus"}]})
except SystemExit:
    died = True
check("run_clm_actions: invalid action dies", died)

died = False
try:
    install_uyuni.run_clm_actions(
        "host1", {"uyuni_content_lifecycle_actions": [{"project": "proj", "action": "build", "wait": True}]})
except SystemExit:
    died = True
check("run_clm_actions: 'wait' without 'wait_env' dies", died)

calls = {"trigger": []}
install_uyuni.sc.wait_for_content_environment = lambda *a, **kw: "built"
install_uyuni.run_clm_actions(
    "host1", {"uyuni_content_lifecycle_actions": [
        {"project": "proj", "action": "build", "message": "m"},
        {"project": "proj", "action": "promote", "from_env": "dev"},
    ]})
check("run_clm_actions: runs build then promote in order",
      calls["trigger"] == [("build", "proj"), ("promote", "proj", "dev")])


# ── install_smlm.main() must not crash with NameError on its normal path ───
# Found in code review 2026-09-05, confirmed by direct execution: main()
# unconditionally assigned to _DEFINITION[0]/_CLU_TYPE[0]/_MYDOMAIN[0] —
# three names never declared anywhere else in the file (verified by
# repo-wide grep) and never read anywhere either, a pure porting leftover.
# Every single normal invocation of `install_smlm.py <lab.json>` raised
# "NameError: name '_DEFINITION' is not defined" right after resolving the
# target node, before ever reaching the real install logic. Removed the
# three dead (write-only, unread) lines entirely.
with tempfile.TemporaryDirectory() as tmp:
    smlm_json = Path(tmp) / "lab.json"
    smlm_json.write_text(
        '{"common": {}, "nodes": {"srv1.mydemo.lab": {"myip": "10.0.0.1", "kcluster": "c1", '
        '"INSTALL_RKE2_TYPE": "server"}}, "kclusters": {"c1": {"clu_type": "rke2", '
        '"mydomain": "mydemo.lab"}}, "smlm": {"smlm_fqdn": "smlm.mydemo.lab", '
        '"smlm_scc_user": "u", "smlm_scc_password": "p"}}'
    )
    install_smlm.setup_helm = lambda *a, **kw: None
    install_smlm.setup_smlm_traefik = lambda *a, **kw: None
    install_smlm.ssh_run = lambda *a, **kw: FakeResult()  # setup_smlm_prereqs's own direct calls
    install_smlm.k8s.ssh_run = lambda *a, **kw: FakeResult()  # ...and its k8s.create_basic_auth_secret() calls
    smlm_setup_calls = []
    install_smlm.setup_smlm = lambda *a, **kw: smlm_setup_calls.append(a)
    old_argv = sys.argv
    sys.argv = ["install_smlm.py", str(smlm_json)]
    smlm_error = None
    try:
        install_smlm.main()
    except Exception as e:  # noqa: BLE001 — we need to see exactly what (if anything) escapes
        smlm_error = e
    finally:
        sys.argv = old_argv
check("install_smlm.main(): no longer raises NameError on its normal (non-flag) path",
      not isinstance(smlm_error, NameError))
check("install_smlm.main(): actually reaches setup_smlm() (proves it got all the way "
      "through the previously-crashing segment, not just past an earlier early-return)",
      len(smlm_setup_calls) == 1)


# ── install_postgresql._digits_only(): guards postgresql_port/pg_version ───
# Found in code review 2026-09-05: both were interpolated unquoted into
# remote shell commands (package/service/unit names, "port = {port}") all
# over this file, and _validate()'s own checks are never actually invoked
# by the real deploy pipeline.
check("_digits_only: a plain digit string passes through unchanged",
      install_postgresql._digits_only({"p": "5432"}, "p", "1", "label") == "5432")
check("_digits_only: a missing value falls back to the given default",
      install_postgresql._digits_only({}, "p", "16", "label") == "16")

_digits_only_died = False
try:
    install_postgresql._digits_only({"p": "16; rm -rf /"}, "p", "1", "label")
except SystemExit:
    _digits_only_died = True
check("_digits_only: a value with a shell metacharacter dies rather than being "
      "returned for interpolation into a remote command",
      _digits_only_died)


# ── install_struts_demo/install_wordpress: struts_demo_ns/wordpress_ns must
# be validated before ever reaching a remote kubectl command ───────────────
# Found in code review 2026-09-05: neither script had a _validate() at all
# (confirmed by grep), so struts_demo_ns/name and wordpress_ns/name reached
# "kubectl delete -n {ns} ..." completely unvalidated and unquoted.
struts_ssh_calls = []
install_struts_demo.ssh_run = lambda *a, **kw: struts_ssh_calls.append(a)
_struts_died = False
try:
    install_struts_demo.setup_struts_demo("host1", "/tmpl", {"struts_demo_ns": "bad;ns"})
except SystemExit:
    _struts_died = True
check("setup_struts_demo: a malicious struts_demo_ns exits before ever calling ssh_run",
      _struts_died and not struts_ssh_calls)

wordpress_ssh_calls = []
install_wordpress.ssh_run = lambda *a, **kw: wordpress_ssh_calls.append(a)
_wordpress_died = False
try:
    install_wordpress.setup_wordpress("host1", "/tmpl", {"wordpress_name": "bad;name"})
except SystemExit:
    _wordpress_died = True
check("setup_wordpress: a malicious wordpress_name exits before ever calling ssh_run",
      _wordpress_died and not wordpress_ssh_calls)


# ── install_smlm_proxy.generate_smlm_proxy_config(): nested spacecmd command
# must be built with shlex.quote(), not hand-rolled single quotes ──────────
# Found in code review 2026-09-05: admin_user/admin_pass/fqdn/server/email
# are free-text addon-config values with no format validation at all, and
# were embedded via hand-rolled single quotes that an embedded single quote
# in any of them would have broken out of.
proxy_subproc_calls = []
install_smlm_proxy.subprocess.run = lambda args, **kw: proxy_subproc_calls.append(args) or FakeResult(returncode=1)
proxy_cfg = {
    "smlm_proxy_server_node": "smlm.mydemo.lab", "smlm_proxy_server_ns": "uyuni-server",
    "smlm_proxy_admin_user": "it's-admin", "smlm_proxy_admin_pass": "pass",
    "smlm_proxy_fqdn": "proxy.mydemo.lab", "smlm_proxy_server": "smlm.mydemo.lab",
}
try:
    install_smlm_proxy.generate_smlm_proxy_config("proxy1.mydemo.lab", proxy_cfg)
except SystemExit:
    pass
remote_cmd = proxy_subproc_calls[0][-1]
check("generate_smlm_proxy_config: an embedded single quote in admin_user doesn't break "
      "out of the remote command's own shell quoting (round-trips through shlex correctly)",
      shlex.split(remote_cmd)[shlex.split(remote_cmd).index("-u") + 1] == "it's-admin")


# ── setup_smlm_prereqs/setup_smlm_proxy_prereqs: the self-signed-TLS-cert
# heredoc's _fqdn='{fqdn}' must not break on an embedded single quote ──────
# Found in code review 2026-09-05: smlm_fqdn/smlm_proxy_fqdn are free-text
# with no format validation at all, and were embedded via hand-rolled
# single quotes in a multi-line heredoc — an embedded single quote would
# have broken out of that quoting.
fqdn_calls = []
install_smlm.ssh_run = lambda *a, **kw: fqdn_calls.append(a[1] if len(a) > 1 else kw.get("cmd", ""))
install_smlm.k8s.ssh_run = lambda *a, **kw: FakeResult()
install_smlm.k8s.set_longhorn_overprovisioning = lambda *a, **kw: None
install_smlm.k8s.create_basic_auth_secret = lambda *a, **kw: None
install_smlm.setup_smlm_prereqs("host1", {"smlm_fqdn": "a.b'; rm -rf /; echo '"})
tls_cmd = next(c for c in fqdn_calls if "openssl" in c)
check("setup_smlm_prereqs: an embedded single quote in smlm_fqdn round-trips through "
      "shlex correctly (the TLS heredoc's _fqdn= line stays one shell assignment)",
      shlex.split(tls_cmd.split("\n")[1])[0] == "_fqdn=a.b'; rm -rf /; echo '")

fqdn_calls.clear()
install_smlm_proxy.ssh_run = lambda *a, **kw: fqdn_calls.append(a[1] if len(a) > 1 else kw.get("cmd", ""))
install_smlm_proxy.k8s.set_longhorn_overprovisioning = lambda *a, **kw: None
install_smlm_proxy.setup_smlm_proxy_prereqs("host1", {"smlm_proxy_fqdn": "a.b'; rm -rf /; echo '"})
tls_cmd = next(c for c in fqdn_calls if "openssl" in c)
check("setup_smlm_proxy_prereqs: an embedded single quote in smlm_proxy_fqdn round-trips "
      "through shlex correctly (the TLS heredoc's _fqdn= line stays one shell assignment)",
      shlex.split(tls_cmd.split("\n")[1])[0] == "_fqdn=a.b'; rm -rf /; echo '")


# ── DNSService: real concurrency stress test for the 2026-09-21 _dns_lock ──
# setup_lab.py's new --parallel VM-creation mode means multiple real
# threads can now call add_to_dns() concurrently for DIFFERENT nodes
# sharing the SAME zone file. _dns_add_line()/_dns_remove_line() do a
# plain read-whole-file -> mutate -> write-whole-file, non-atomically — a
# real lost-update race without a lock. This spawns REAL threading.Thread
# workers (not a mock of the lock itself) hammering add_to_dns()
# concurrently and verifies every single one of their entries survives.
#
# With ssh_run/file-I/O mocked this fast, the actual race window is too
# narrow for the GIL's own scheduling to reliably interleave two threads
# mid-read-modify-write in one run — confirmed by hand: with _dns_lock
# swapped for a no-op, the same test still "passed" most runs purely by
# luck. So Path.read_text is wrapped here with a small artificial delay —
# widening the window deterministically — for every caller, including the
# real _dns_add_line/_dns_remove_line inside add_to_dns() itself. This is
# what actually proves _dns_lock serializes access, not just that the
# lock object exists.
import threading  # noqa: E402
import time as _time  # noqa: E402

_real_read_text = Path.read_text


def _slow_read_text(self, *a, **kw):
    result = _real_read_text(self, *a, **kw)
    _time.sleep(0.005)  # widen the read -> write race window on purpose
    return result


Path.read_text = _slow_read_text

services.ssh_run = lambda *a, **kw: FakeResult()
stress_svc = services.DNSService()
stress_svc.restart_named = lambda: None  # avoid a real local `systemctl restart named`
services.NAMED_ZONE_DIR = Path(tempfile.mkdtemp())

N_WORKERS = 12
threads = [
    threading.Thread(target=stress_svc.add_to_dns,
                      args=("host{}.paralleltest.lab".format(i), "10.99.0.{}".format(i),
                            "paralleltest.lab", "99.10"))
    for i in range(N_WORKERS)
]
for t in threads:
    t.start()
for t in threads:
    t.join()

Path.read_text = _real_read_text  # restore before any later test reads a real file

lan_file = services.NAMED_ZONE_DIR / "paralleltest.lab.lan"
rev_file = services.NAMED_ZONE_DIR / "99.10.db"
lan_lines = lan_file.read_text().splitlines()
rev_lines = rev_file.read_text().splitlines()
check("DNSService.add_to_dns under real concurrent threads (artificially widened race window): "
      "every single A record survives — {} of {} present".format(
          sum(1 for i in range(N_WORKERS) if any("host{}".format(i) in l for l in lan_lines)), N_WORKERS),
      all(any("host{}".format(i) in line for line in lan_lines) for i in range(N_WORKERS)))
check("DNSService.add_to_dns under real concurrent threads: every single PTR record survives too",
      all(any("host{}.paralleltest.lab".format(i) in line for line in rev_lines) for i in range(N_WORKERS)))
check("DNSService.add_to_dns under real concurrent threads: no duplicate/corrupted lines "
      "(exactly {} A record lines, not more or fewer)".format(N_WORKERS),
      len(lan_lines) == N_WORKERS)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all live-bugfix regression checks passed")
