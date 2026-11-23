#!/usr/bin/env python3.11
# desktop_common / install_xfce — see 129_desktop.sh.
import importlib.util
import io
import sys
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402
import desktop_common as desktop  # noqa: E402
import addon_common as ac  # noqa: E402

_loader = SourceFileLoader("install_xfce", str(_REPO / "scripts" / "install_xfce"))
_spec = importlib.util.spec_from_loader("install_xfce", _loader)
xfce = importlib.util.module_from_spec(_spec)
_loader.exec_module(xfce)

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Host:
    """A fake node: which package managers it has, which packages its repositories carry, and whether it is
    transactional. Records every command sent to it."""

    def __init__(self, manager="zypper", packages=(), transactional=False, os_id="opensuse-leap"):
        self.manager = manager
        self.packages = set(packages)
        self.transactional = transactional
        self.os_id = os_id
        self.cmds = []
        self.inputs = []

    def ssh(self, host, cmd, **kw):
        self.cmds.append(cmd)
        if kw.get("input_text"):
            self.inputs.append(kw["input_text"])
        rc = 0
        if cmd.startswith("command -v "):
            wanted = cmd.split()[-1]
            rc = 0 if wanted == self.manager or (self.transactional and wanted == "transactional-update") else 1
        elif "search --match-exact" in cmd or cmd.startswith("apt-cache show") or " list available " in cmd:
            rc = 0 if any(p in self.packages for p in cmd.replace("'", " ").split()) else 1
        elif "snapper --csvout list" in cmd:
            rc = 1          # nothing pending: no reboot in a test
        return mock.Mock(returncode=rc, stdout="", stderr="")

    def ran(self, needle):
        return [c for c in self.cmds if needle in c]


def run(fn, host, *args, **kwargs):
    """`fn` against a fake node, with every ssh_run faked and output captured; returns (result, printed, stderr)."""
    printed, err = [], io.StringIO()

    def fake_print(*a, **kw):
        line = " ".join(str(x) for x in a)
        # die()/warn() print to stderr; the test reads those from `err`, not from the stdout lines.
        if kw.get("file") is not None:
            kw["file"].write(line + "\n")
            return
        printed.append(line)

    with mock.patch.object(lc, "ssh_run", side_effect=host.ssh), \
            mock.patch.object(desktop, "ssh_run", side_effect=host.ssh), \
            mock.patch.object(lc, "wait_for_ssh_command", return_value=True), \
            mock.patch.object(xfce, "detect_os", return_value={"id": host.os_id, "ver_id": "16.0",
                                                               "like": "suse", "ver_major": "16"}), \
            mock.patch("builtins.print", side_effect=fake_print), \
            redirect_stderr(err):
        try:
            result = fn(host.hostname if hasattr(host, "hostname") else "node1", *args, **kwargs)
        except SystemExit:
            result = None
    return result, printed, err.getvalue()


_XFCE_SUSE = set(desktop.DESKTOPS["xfce"]["zypper"])
_XFCE_DEB = set(desktop.DESKTOPS["xfce"]["apt-get"])
_XFCE_RHEL = set(desktop.DESKTOPS["xfce"]["dnf"])

# ── the distribution's own package manager and packages ───────────────────────
h = Host(manager="zypper", packages=_XFCE_SUSE)
pkgs, printed, err = run(desktop.install_desktop, h)
check("suse: installs the XFCE pattern with zypper, in one transaction",
      pkgs == desktop.DESKTOPS["xfce"]["zypper"]
      and len(h.ran("zypper --non-interactive install -y")) == 1
      and "patterns-xfce-xfce" in h.ran("zypper --non-interactive install -y")[0])

h = Host(manager="apt-get", packages=_XFCE_DEB, os_id="ubuntu")
pkgs, _, _ = run(desktop.install_desktop, h)
check("debian: installs the xfce4 metapackage with apt-get, after refreshing the index",
      pkgs == desktop.DESKTOPS["xfce"]["apt-get"]
      and any(c.startswith("DEBIAN_FRONTEND=noninteractive apt-get install -y") and "xfce4" in c for c in h.cmds)
      and any("apt-get update -qq" in c for c in h.cmds))

h = Host(manager="dnf", packages=_XFCE_RHEL, os_id="rhel")
pkgs, _, _ = run(desktop.install_desktop, h)
check("rhel: installs the xfce4 session packages with dnf",
      pkgs == desktop.DESKTOPS["xfce"]["dnf"]
      and any(c.startswith("dnf install -y") and "xfce4-session" in c for c in h.cmds))

h = Host(manager="zypper", packages=_XFCE_SUSE, transactional=True, os_id="sl-micro")
pkgs, _, _ = run(desktop.install_desktop, h)
check("transactional: installs with transactional-update, the only way to install there",
      any(c.startswith("transactional-update --quiet pkg install -y") for c in h.cmds)
      and not any(c.startswith("zypper --non-interactive install") for c in h.cmds))
check("transactional: waits for the transactional-update lock and checks for a pending snapshot",
      any("snapper --csvout list" in c for c in h.cmds))

h = Host(manager="zypper", packages=_XFCE_SUSE | {"firefox"})
pkgs, _, _ = run(desktop.install_desktop, h, extra_packages=["firefox"])
check("extra packages: installed in the same transaction", "firefox" in pkgs
      and "firefox" in h.ran("zypper --non-interactive install -y")[0])
h = Host(manager="zypper", packages=_XFCE_SUSE)
pkgs, printed, _ = run(desktop.install_desktop, h, extra_packages=["nosuchpkg"])
check("extra packages: one the distribution does not have is skipped, not fatal",
      pkgs == desktop.DESKTOPS["xfce"]["zypper"] and any("nosuchpkg" in p for p in printed))

# ── a distribution that does not distribute XFCE ──────────────────────────────
h = Host(manager="zypper", packages={"vim"}, os_id="sl-micro")
pkgs, _, err = run(desktop.install_desktop, h, os_id="sl-micro")
check("no package: dies saying the distribution does not distribute xfce, naming the package",
      pkgs is None and "does not distribute xfce" in err and "patterns-xfce-xfce" in err)
check("no package: installs nothing", not any("install" in c for c in h.cmds))
h = Host(manager="", packages=set())
pkgs, _, err = run(desktop.install_desktop, h)
check("no package manager: dies naming the managers it looked for",
      pkgs is None and "no supported package manager" in err)

# ── access methods ────────────────────────────────────────────────────────────
h = Host(manager="zypper", packages=_XFCE_SUSE)
port, _, _ = run(desktop.enable_access, h, access=desktop.ACCESS_CONSOLE)
check("console: serves no port and only selects the graphical target",
      port == 0 and any("systemctl set-default graphical.target" in c for c in h.cmds)
      and not any("xrdp" in c or "x11vnc" in c for c in h.cmds))

h = Host(manager="zypper", packages=_XFCE_SUSE | {"xrdp"})
port, _, _ = run(desktop.enable_access, h, access=desktop.ACCESS_XRDP)
check("xrdp: installs xrdp, enables it and opens 3389",
      port == 3389 and any("install -y" in c and "xrdp" in c for c in h.cmds)
      and any("systemctl enable --now xrdp" in c for c in h.cmds)
      and any("--add-port=3389/tcp" in c for c in h.cmds))
h = Host(manager="zypper", packages=_XFCE_SUSE | {"xrdp"})
port, _, _ = run(desktop.enable_access, h, access=desktop.ACCESS_XRDP, port=13389)
check("xrdp: a port override reaches xrdp.ini and the firewall",
      port == 13389 and any("s/^port=.*/port=13389/" in c for c in h.cmds)
      and any("--add-port=13389/tcp" in c for c in h.cmds))
h = Host(manager="dnf", packages=_XFCE_RHEL, os_id="rhel")
port, _, err = run(desktop.enable_access, h, access=desktop.ACCESS_XRDP)
check("xrdp: a distribution without the package dies pointing at EPEL and the other methods",
      port is None and "does not distribute xrdp" in err and "EPEL" in err)

h = Host(manager="zypper", packages=_XFCE_SUSE | {"x11vnc"})
port, _, _ = run(desktop.enable_access, h, access=desktop.ACCESS_VNC)
check("vnc: installs x11vnc, writes its unit, enables it and opens 5900",
      port == 5900 and any("install -y" in c and "x11vnc" in c for c in h.cmds)
      and any(c.startswith("cat > /etc/systemd/system/lab-x11vnc.service") for c in h.cmds)
      and any("systemctl enable --now lab-x11vnc.service" in c for c in h.cmds)
      and any("--add-port=5900/tcp" in c for c in h.cmds))
check("vnc: the unit serves the console display on the port, and starts with the graphical target",
      any("-display :0" in u and "-rfbport 5900" in u and "WantedBy=graphical.target" in u for u in h.inputs))
h = Host(manager="zypper", packages=_XFCE_SUSE)
port, _, err = run(desktop.enable_access, h, access=desktop.ACCESS_VNC)
check("vnc: a distribution without x11vnc dies naming it", port is None and "does not distribute x11vnc" in err)
h = Host(manager="zypper", packages=_XFCE_SUSE)
port, _, err = run(desktop.enable_access, h, access="rdp")
check("an unknown access method is refused", port is None and "unknown desktop access method" in err)

# ── the add-on itself ─────────────────────────────────────────────────────────
h = Host(manager="zypper", packages=_XFCE_SUSE)
port, printed, _ = run(xfce.setup_xfce, h, {})
check("addon: console is the default, and it says where to look for the desktop",
      port == 0 and any("hypervisor" in p for p in printed)
      and not any("xrdp" in c for c in h.cmds))
h = Host(manager="zypper", packages=_XFCE_SUSE | {"xrdp"})
port, printed, _ = run(xfce.setup_xfce, h, {"xfce_access": "xrdp", "xfce_port": "13389"})
check("addon: xfce_access and xfce_port reach the access method, and the address is printed",
      port == 13389 and any("RDP at node1:13389" in p for p in printed))
h = Host(manager="zypper", packages=_XFCE_SUSE | {"x11vnc"})
port, printed, _ = run(xfce.setup_xfce, h, {"xfce_access": "vnc"})
check("addon: vnc says there is no password", port == 5900 and any("no password" in p for p in printed))

# ── validation ────────────────────────────────────────────────────────────────


def errors(cfg):
    v = ac.Validator({"xfce": cfg})
    xfce._validate(v)
    return v.errors


check("validate: every access method is accepted, and an unset one",
      errors({}) == [] and all(errors({"xfce_access": a}) == [] for a in desktop.ACCESS_METHODS))
check("validate: an unknown access method is rejected", len(errors({"xfce_access": "rdp"})) == 1)
check("validate: xfce_port must be a port number", len(errors({"xfce_port": "3389/tcp"})) == 1)
check("validate: xfce_extra_packages must be a list",
      len(errors({"xfce_extra_packages": "firefox"})) == 1 and errors({"xfce_extra_packages": ["firefox"]}) == [])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all desktop checks passed")
