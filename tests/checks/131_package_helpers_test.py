#!/usr/bin/env python3.11
# lab_creation's package helpers — see 131_package_helpers.sh.
import io
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Host:
    """A fake node: which managers it has, which packages its repositories carry, whether a snapshot is pending,
    which firewall runs, and which installs fail."""

    def __init__(self, managers=("zypper",), packages=(), pending=False, firewall="", failing=()):
        self.managers = set(managers)
        self.packages = set(packages)
        self.pending = pending
        self.firewall = firewall
        self.failing = set(failing)
        self.cmds = []

    def ssh(self, host, cmd, **kw):
        self.cmds.append(cmd)
        rc = 0
        if cmd.startswith("command -v "):
            rc = 0 if cmd.split()[2] in self.managers else 1
        elif "search --match-exact" in cmd or cmd.startswith("apt-cache show") or " list available " in cmd:
            rc = 0 if any(p in self.packages for p in cmd.replace("'", " ").split()) else 1
        elif "snapper --csvout list" in cmd:
            rc = 0 if self.pending else 1
        elif cmd.startswith("if command -v firewall-cmd"):
            # The real script's if/elif/fi exits 0 whether or not a firewall is running.
            rc = 0
        elif any(f in cmd for f in self.failing):
            rc = 1
        return mock.Mock(returncode=rc, stdout="", stderr="boom" if rc else "")

    def installs(self):
        return [c for c in self.cmds if " install " in c or "pkg install" in c]


def run(fn, host, *args, **kwargs):
    """`fn` with lab_creation's ssh faked; returns (result, stderr)."""
    err = io.StringIO()

    def fake_print(*a, **kw):
        # die()/warn() print to stderr; the test reads those from `err`.
        if kw.get("file") is not None:
            kw["file"].write(" ".join(str(x) for x in a) + "\n")

    with mock.patch.object(lc, "ssh_run", side_effect=host.ssh), \
            mock.patch.object(lc, "wait_for_ssh_command", return_value=True), \
            mock.patch.object(lc, "boot_id", return_value="old"), \
            mock.patch.object(lc, "wait_for_reboot", return_value=True), \
            mock.patch("builtins.print", side_effect=fake_print), redirect_stderr(err):
        try:
            result = fn(*args, **kwargs)
        except SystemExit:
            result = None
    return result, err.getvalue()

# ── which manager is picked ───────────────────────────────────────────────────
check("zypper on a SUSE host", run(lc.package_manager, Host(managers=("zypper",)), "n1")[0] == "zypper")
check("apt-get on a Debian host", run(lc.package_manager, Host(managers=("apt-get",)), "n1")[0] == "apt-get")
check("dnf wins over yum where both exist",
      run(lc.package_manager, Host(managers=("dnf", "yum")), "n1")[0] == "dnf")
check("transactional-update wins over zypper, because there it is the only way to install",
      run(lc.package_manager, Host(managers=("transactional-update", "zypper")), "n1")[0] == "transactional-update")
result, err = run(lc.package_manager, Host(managers=()), "n1")
check("a host with no package manager is refused, naming the ones looked for",
      result is None and "no supported package manager" in err and "apt-get" in err)

# ── availability ──────────────────────────────────────────────────────────────
h = Host(managers=("zypper",), packages={"vim"})
check("an available package is reported available", run(lc.package_available, h, "n1", "vim")[0] is True)
check("a missing one is not", run(lc.package_available, h, "n1", "emacs")[0] is False)
h = Host(managers=("apt-get",), packages={"vim"})
run(lc.package_available, h, "n1", "vim")
check("apt refreshes its index before answering", any("apt-get update -qq" in c for c in h.cmds))

# ── installing ────────────────────────────────────────────────────────────────
h = Host(managers=("zypper",))
run(lc.install_packages, h, "n1", ["vim", "git"])
check("zypper installs every package in one transaction",
      h.installs() == ["zypper --non-interactive install -y vim git"])
h = Host(managers=("apt-get",))
run(lc.install_packages, h, "n1", ["vim"])
check("apt-get installs non-interactively, after refreshing",
      h.installs() == ["DEBIAN_FRONTEND=noninteractive apt-get install -y vim"]
      and any("apt-get update -qq" in c for c in h.cmds))
h = Host(managers=("zypper",))
run(lc.install_packages, h, "n1", "vim")
check("a single package name is accepted as a string", h.installs() == ["zypper --non-interactive install -y vim"])
h = Host(managers=("zypper",))
run(lc.install_packages, h, "n1", [])
check("nothing to install runs no command", h.installs() == [])
h = Host(managers=("zypper",))
run(lc.install_packages, h, "n1", ["a b", "c;d"])
check("package names are shell-quoted", any("'a b'" in c and "'c;d'" in c for c in h.installs()))
h = Host(managers=("zypper",))
run(lc.install_packages, h, "n1", ["python311"], extra_args="--gpg-auto-import-keys")
check("extra_args reaches the manager's own command line",
      h.installs() == ["zypper --non-interactive install -y --gpg-auto-import-keys python311"])

h = Host(managers=("zypper",), failing=("install",))
result, err = run(lc.install_packages, h, "n1", ["vim"])
check("a failed install dies by default, with the manager's own error",
      result is None and "could not install vim" in err and "boom" in err)
h = Host(managers=("zypper",), failing=("install",))
result, err = run(lc.install_packages, h, "n1", ["vim"], required=False)
check("required=False warns and returns empty instead of dying",
      result == "" and "could not install vim" in err)

# ── transactional hosts ───────────────────────────────────────────────────────
h = Host(managers=("transactional-update", "zypper"), pending=True)
run(lc.install_packages, h, "n1", ["vim"])
check("a transactional host installs into a snapshot and reboots into it",
      h.installs() == ["transactional-update --quiet pkg install -y vim"]
      and any("systemctl reboot" in c for c in h.cmds))
h = Host(managers=("transactional-update",), pending=True)
run(lc.install_packages, h, "n1", ["vim"], reboot=False)
check("reboot=False leaves the reboot to the caller", not any("systemctl reboot" in c for c in h.cmds))
h = Host(managers=("transactional-update",), pending=False)
run(lc.install_packages, h, "n1", ["vim"])
check("no reboot when no snapshot is pending", not any("systemctl reboot" in c for c in h.cmds))

# ── podman ────────────────────────────────────────────────────────────────────
h = Host(managers=("zypper", "podman"))
check("podman already there: nothing is installed, the socket is enabled",
      run(lc.ensure_podman, h, "n1")[0] is True and h.installs() == []
      and any("systemctl enable --now podman.socket" in c for c in h.cmds))
h = Host(managers=("zypper",))
run(lc.ensure_podman, h, "n1")
check("podman missing: it is installed and its socket enabled",
      h.installs() == ["zypper --non-interactive install -y podman"]
      and any("systemctl enable --now podman.socket" in c for c in h.cmds))
h = Host(managers=("zypper",), failing=("install",))
result, err = run(lc.ensure_podman, h, "n1")
check("a distribution with no podman package warns rather than dying",
      result is False and "could not install podman" in err)

# ── the guest's own firewall ──────────────────────────────────────────────────
h = Host(managers=("zypper",), firewall="firewalld")
run(lc.open_guest_port, h, "n1", 3389)
check("the port is opened for both firewalls the one command knows",
      any("--add-port=3389/tcp" in c and "ufw allow 3389/tcp" in c for c in h.cmds))
h = Host(managers=("zypper",), firewall="")
_, err = run(lc.open_guest_port, h, "n1", 5900, proto="udp")
check("a host with neither firewall is not an error", "could not open" not in err)
check("the protocol is honoured", any("5900/udp" in c for c in h.cmds))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all package-helper checks passed")
