#!/usr/bin/env python3
"""
Graphical desktops on a lab node: which packages each distribution family ships for a desktop environment, and how
the session is reached (the node's own console, RDP or VNC).

A desktop environment is one entry in DESKTOPS, keyed by the package manager that installs it, so adding GNOME or KDE
is a table entry rather than new code. The access methods are independent of which desktop is installed: install_xrdp()
and install_vnc() work for any of them.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import shlex

from lab_creation import (die, install_packages, log, open_guest_port, package_available, package_manager,
                          ssh_run)

# Packages per desktop environment and package manager. The first name of each list is the one whose absence means
# "this distribution does not ship this desktop": a metapackage or pattern that pulls the rest of the session in.
#
# transactional-update installs from the same repositories as zypper, so it shares zypper's names.
DESKTOPS = {
    "xfce": {
        "zypper": ["patterns-xfce-xfce", "xorg-x11-server", "xdg-utils"],
        "transactional-update": ["patterns-xfce-xfce", "xorg-x11-server", "xdg-utils"],
        "dnf": ["xfce4-session", "xfdesktop", "xfwm4", "xfce4-panel", "xfce4-terminal", "thunar",
                "xorg-x11-server-Xorg", "xdg-utils"],
        "yum": ["xfce4-session", "xfdesktop", "xfwm4", "xfce4-panel", "xfce4-terminal", "thunar",
                "xorg-x11-server-Xorg", "xdg-utils"],
        "apt-get": ["xfce4", "xfce4-terminal", "xdg-utils"],
    },
}

# How the session is reached. "console" is the node's own screen (the hypervisor's SPICE/VNC display or a physical
# one) and needs no server of its own; "xrdp" and "vnc" serve it over the network.
ACCESS_CONSOLE = "console"
ACCESS_XRDP = "xrdp"
ACCESS_VNC = "vnc"
ACCESS_METHODS = (ACCESS_CONSOLE, ACCESS_XRDP, ACCESS_VNC)

XRDP_PORT = 3389
VNC_PORT = 5900

# x11vnc serves the display the console is already showing, so a VNC session shows the same desktop as the console and
# needs no user, session or password file of its own.
_VNC_PACKAGE = "x11vnc"
_VNC_UNIT = "/etc/systemd/system/lab-x11vnc.service"
_VNC_SERVICE = "lab-x11vnc.service"


def desktop_packages(desktop: str, manager: str) -> list:
    """The packages `manager` installs for `desktop`. Dies on a desktop this library does not know."""
    known = DESKTOPS.get(desktop)
    if not known:
        die("unknown desktop environment '{}' — this lab installs: {}".format(desktop, ", ".join(sorted(DESKTOPS))))
    pkgs = known.get(manager)
    if not pkgs:
        die("desktop '{}' has no package list for package manager '{}'".format(desktop, manager))
    return list(pkgs)


def install_desktop(host: str, desktop: str = "xfce", extra_packages=None, os_id: str = "") -> list:
    """
    Install `desktop` on `host` with the host's own package manager, plus `extra_packages`.

    Dies with "does not distribute" when the distribution has no package for the desktop, telling the two apart: a
    distribution that does not ship it at all is not the same as a package manager error. Returns the packages
    installed.
    """
    manager = package_manager(host)
    pkgs = desktop_packages(desktop, manager) + [p for p in (extra_packages or []) if p]
    metapackage = pkgs[0]
    if not package_available(host, metapackage, manager=manager):
        die("'{}'{} does not distribute {}: its repositories have no '{}' package. Use an image of a distribution "
            "that ships {} (openSUSE Leap or Tumbleweed, SLES with the Desktop Applications module, Debian, Ubuntu, "
            "or RHEL/CentOS/Fedora) — or add the repository that carries it to this node first".format(
                host, " ({})".format(os_id) if os_id else "", desktop, metapackage, desktop))
    # Every package of the set in one transaction: on a transactional-update host each call is a snapshot of its own,
    # and a second one started before the first is booted into discards it.
    available = [p for p in pkgs if p == metapackage or package_available(host, p, manager=manager)]
    skipped = [p for p in pkgs if p not in available]
    if skipped:
        log("- '{}' has no package for: {} — installing the rest of {}".format(host, ", ".join(skipped), desktop))
    install_packages(host, available, manager=manager)
    log("- {} installed on '{}'".format(desktop, host))
    return available


def enable_console(host: str) -> None:
    """
    Make `host` boot into its graphical session, so its own console shows the desktop. The display manager comes in as
    a dependency of the desktop's packages, so this only selects the target.
    """
    ssh_run(host, "systemctl set-default graphical.target", check=False, capture=True)
    r = ssh_run(host, "systemctl isolate graphical.target", check=False, capture=True)
    if r.returncode != 0:
        log("- '{}' will show the desktop on its console after the next boot".format(host))


def install_xrdp(host: str, port: int = XRDP_PORT) -> int:
    """
    Serve the desktop over RDP from `host`. Dies with "does not distribute" when the distribution has no xrdp package
    (RHEL and CentOS carry it in EPEL, not in the base repositories). Returns the port it listens on.
    """
    manager = package_manager(host)
    if not package_available(host, "xrdp", manager=manager):
        die("'{}' does not distribute xrdp: its repositories have no 'xrdp' package. On RHEL/CentOS it comes from "
            "EPEL — add that repository to this node first, or use the console or vnc access method".format(host))
    install_packages(host, ["xrdp"], manager=manager)
    if port != XRDP_PORT:
        ssh_run(host, "sed -i 's/^port=.*/port={}/' /etc/xrdp/xrdp.ini".format(int(port)), check=False, capture=True)
    ssh_run(host, "systemctl enable --now xrdp", check=False, capture=True)
    open_guest_port(host, int(port))
    log("- xrdp serves '{}' on port {}".format(host, port))
    return int(port)


def install_vnc(host: str, port: int = VNC_PORT) -> int:
    """
    Serve `host`'s console display over VNC with x11vnc, as a systemd service of its own. Dies with "does not
    distribute" when the distribution has no x11vnc package. Returns the port it listens on.

    No authentication: the session is as reachable as the port is. A lab network only.
    """
    manager = package_manager(host)
    if not package_available(host, _VNC_PACKAGE, manager=manager):
        die("'{}' does not distribute {}: its repositories have no '{}' package. Use the console or xrdp access "
            "method, or add the repository that carries it to this node first".format(host, _VNC_PACKAGE,
                                                                                     _VNC_PACKAGE))
    install_packages(host, [_VNC_PACKAGE], manager=manager)
    unit = (
        "[Unit]\n"
        "Description=x11vnc server for the lab desktop\n"
        "After=graphical.target\n"
        "\n"
        "[Service]\n"
        "ExecStart=/usr/bin/x11vnc -display :0 -auth guess -forever -shared -nopw -rfbport {port}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical.target\n"
    ).format(port=int(port))
    ssh_run(host, "cat > {}".format(shlex.quote(_VNC_UNIT)), input_text=unit)
    ssh_run(host, "systemctl daemon-reload", check=False, capture=True)
    ssh_run(host, "systemctl enable --now {}".format(_VNC_SERVICE), check=False, capture=True)
    open_guest_port(host, int(port))
    log("- x11vnc serves '{}'s console display on port {}, without a password".format(host, port))
    return int(port)


def enable_access(host: str, access: str = ACCESS_CONSOLE, port: int = 0) -> int:
    """
    Make the installed desktop reachable the way `access` says. Returns the TCP port it listens on, or 0 for the
    console, which serves nothing over the network. Dies on an unknown access method.
    """
    enable_console(host)
    if access == ACCESS_CONSOLE:
        return 0
    if access == ACCESS_XRDP:
        return install_xrdp(host, port=port or XRDP_PORT)
    if access == ACCESS_VNC:
        return install_vnc(host, port=port or VNC_PORT)
    die("unknown desktop access method '{}' — use one of: {}".format(access, ", ".join(ACCESS_METHODS)))
