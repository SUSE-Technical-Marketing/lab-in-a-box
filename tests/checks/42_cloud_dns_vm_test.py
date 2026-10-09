#!/usr/bin/env python3
# Unit tests for libs/backends.py's ensure_cloud_dns_vm() and its cloud-init generator
# (_cloud_dns_vm_user_data()). No real cloud account needed: a fake backend records every call made on
# it. Verifies the reuse-vs-create decision, the generated YAML's validity and shell-script
# correctness, and CLOUD_BACKEND_NAMES' own membership — not real provider behavior. Run from
# 42_cloud_dns_vm.sh, in its own container — see tests/run_tests.sh.
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _cp(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


# ── CLOUD_BACKEND_NAMES: every registered backend except libvirt/harvester ─
check("CLOUD_BACKEND_NAMES excludes libvirt", "libvirt" not in backends.CLOUD_BACKEND_NAMES)
check("CLOUD_BACKEND_NAMES excludes harvester", "harvester" not in backends.CLOUD_BACKEND_NAMES)
for name in ("hetzner", "aws", "gcp", "alibaba", "scaleway", "upcloud", "ovhcloud", "exoscale"):
    check("CLOUD_BACKEND_NAMES includes '{}'".format(name), name in backends.CLOUD_BACKEND_NAMES)
check("CLOUD_BACKEND_NAMES has exactly the 8 cloud backends, no more/fewer",
      len(backends.CLOUD_BACKEND_NAMES) == 8)


# ── _cloud_dns_vm_user_data(): produces valid, parseable YAML ──────────────
user_data = backends._cloud_dns_vm_user_data("ssh-ed25519 AAAAtest test@example", "mydemo.lab")
check("_cloud_dns_vm_user_data() starts with the real #cloud-config header",
      user_data.startswith("#cloud-config\n"))
check("_cloud_dns_vm_user_data() embeds the real SSH pubkey", "ssh-ed25519 AAAAtest test@example" in user_data)
check("_cloud_dns_vm_user_data() installs the real bind9 package", "bind9" in user_data)

try:
    import yaml
    parsed = yaml.safe_load(user_data)
    check("the generated cloud-init is valid YAML", isinstance(parsed, dict))
    runcmd = parsed.get("runcmd", [])
    check("runcmd is a non-empty list of shell command strings",
          isinstance(runcmd, list) and len(runcmd) > 0 and all(isinstance(c, str) for c in runcmd))
    joined = "\n".join(runcmd)
    check("runcmd creates /var/lib/named before writing into it (real ordering, not write_files)",
          runcmd.index("mkdir -p /var/lib/named") < next(
              i for i, c in enumerate(runcmd) if "/var/lib/named/mydemo.lab.lan" in c))
    check("runcmd writes the real zone file path", "/var/lib/named/mydemo.lab.lan" in joined)
    check("runcmd writes the real named.conf.local zone stanza",
          any('zone "mydemo.lab"' in c and "/var/lib/named/mydemo.lab.lan" in c for c in runcmd))
    check("runcmd does NOT create a bind9.service -> named.service alias symlink, since Ubuntu's "
          "bind9 package already ships a working named.service, which the symlink would shadow",
          not any("ln -sf" in c and "named.service" in c for c in runcmd))
    check("runcmd writes an AppArmor local override for /var/lib/named, since Ubuntu's "
          "usr.sbin.named profile only allows /var/lib/bind/**",
          any("/etc/apparmor.d/local/usr.sbin.named" in c and "/var/lib/named/** rw," in c for c in runcmd))
    check("runcmd reloads the AppArmor profile after writing the override, before starting named",
          runcmd.index(next(c for c in runcmd if "/etc/apparmor.d/local/usr.sbin.named" in c))
          < runcmd.index(next(c for c in runcmd if "apparmor_parser -r" in c))
          < runcmd.index("systemctl enable --now named"))
    check("runcmd enables and starts named (no alias needed) at the end",
          any(c == "systemctl enable --now named" for c in runcmd))

    # Every runcmd entry must actually be a runnable shell command — real, not just
    # syntactically-plausible-looking text.
    all_ok = True
    for cmd in runcmd:
        result = subprocess.run(["bash", "-n", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            all_ok = False
            print("  bash -n rejected:", cmd, "->", result.stderr.decode("utf-8", "replace"))
    check("every generated runcmd entry is syntactically valid shell (bash -n)", all_ok)
except ImportError:
    print("no PyYAML for this interpreter — the YAML-parse checks run under python3.11")


# ── ensure_cloud_dns_vm(): reuse path — vm_exists() + get_ip(), no create_vm() call ──
class _FakeBackendReuse:
    def __init__(self):
        self.calls = []

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return True

    def get_ip(self, vm_name):
        self.calls.append(("get_ip", vm_name))
        return "203.0.113.100"

    def create_vm(self, *a, **kw):
        self.calls.append(("create_vm", a, kw))
        raise AssertionError("create_vm() must not be called when the DNS VM already exists")


reuse_backend = _FakeBackendReuse()
with tempfile.TemporaryDirectory() as tempfile_dir:
    ip = backends.ensure_cloud_dns_vm(
        reuse_backend, "aws", "ssh-ed25519 AAAAtest test@example", "mydemo.lab", "ami-xyz", tempfile_dir)
check("ensure_cloud_dns_vm() returns the existing DNS VM's real IP on reuse", ip == "203.0.113.100")
check("ensure_cloud_dns_vm() checks vm_exists() with the fixed 'lab-dns-<backend>' name",
      ("vm_exists", "lab-dns-aws") in reuse_backend.calls)
check("ensure_cloud_dns_vm() calls get_ip() on reuse", ("get_ip", "lab-dns-aws") in reuse_backend.calls)


# ── ensure_cloud_dns_vm(): create path — vm_exists() False, full create flow, real IP returned ──
class _FakeBackendCreate:
    def __init__(self):
        self.calls = []

    def vm_exists(self, vm_name):
        self.calls.append(("vm_exists", vm_name))
        return False

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self.calls.append(("copy_vm_image", iso_image, vm_name, vm_dsk_gb, config_method))

    def push_provisioning_files(self, vm_name, config_method=""):
        self.calls.append(("push_provisioning_files", vm_name, config_method))

    def create_vm(self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network, config_method="", iso_image="", mymac=None):
        self.calls.append(("create_vm", vm_name, vm_cpu, vm_mem, vm_dsk_gb, config_method, iso_image))
        return "203.0.113.101"


create_backend = _FakeBackendCreate()
with tempfile.TemporaryDirectory() as tempfile_dir:
    # ensure_cloud_dns_vm() itself (not the backend) polls `ssh ... systemctl is-active named`
    # after create_vm() returns, because cloud-init's package install continues after the IP is assigned.
    # Mocked here the same way every other backend test in this suite mocks subprocess.run.
    with mock.patch.object(backends.subprocess, "run", return_value=_cp(0)):
        ip = backends.ensure_cloud_dns_vm(
            create_backend, "hetzner", "ssh-ed25519 AAAAtest test@example", "mydemo.lab",
            "ubuntu-24.04", tempfile_dir)
    userdata_file = Path(tempfile_dir) / "cloud-init" / "lab-dns-hetzner_user-data"
    check("ensure_cloud_dns_vm() writes the DNS VM's own cloud-init file to the real expected path",
          userdata_file.is_file())

check("ensure_cloud_dns_vm() returns the newly created DNS VM's real IP", ip == "203.0.113.101")
# vm_name sits at a different tuple index per call shape: index 1 for vm_exists/
# push_provisioning_files/create_vm, index 2 for copy_vm_image(iso_image, vm_name, ...).
_vm_name_by_call = {
    "vm_exists": lambda c: c[1], "copy_vm_image": lambda c: c[2],
    "push_provisioning_files": lambda c: c[1], "create_vm": lambda c: c[1],
}
check("ensure_cloud_dns_vm() uses the fixed 'lab-dns-<backend>' name throughout",
      all(_vm_name_by_call[c[0]](c) == "lab-dns-hetzner" for c in create_backend.calls)
      and ("vm_exists", "lab-dns-hetzner") in create_backend.calls)
check("ensure_cloud_dns_vm() reuses the SAME ISO_IMAGE the calling lab already configured",
      any(c[0] == "create_vm" and c[6] == "ubuntu-24.04" for c in create_backend.calls))
check("ensure_cloud_dns_vm() sizes the DNS VM tiny (1 vCPU / 512 MiB) — cheap by design",
      any(c[0] == "create_vm" and c[2] == 1 and c[3] == 512 for c in create_backend.calls))
call_order = [c[0] for c in create_backend.calls]
check("ensure_cloud_dns_vm() follows the real call order: vm_exists -> copy_vm_image -> "
      "push_provisioning_files -> create_vm",
      call_order == ["vm_exists", "copy_vm_image", "push_provisioning_files", "create_vm"])


# ── ensure_cloud_dns_vm(): create path — create_vm() reports no IP dies clearly ──
class _FakeBackendNoIP(_FakeBackendCreate):
    def create_vm(self, *a, **kw):
        return None


died = []
with mock.patch.object(backends, "die",
                        side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    with tempfile.TemporaryDirectory() as tempfile_dir:
        try:
            backends.ensure_cloud_dns_vm(
                _FakeBackendNoIP(), "aws", "ssh-ed25519 AAAAtest test@example", "mydemo.lab",
                "ami-xyz", tempfile_dir)
        except SystemExit:
            pass
check("ensure_cloud_dns_vm() dies clearly when create_vm() reports no IP at all",
      any("no IP" in m for m in died))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud_dns_vm checks passed")
