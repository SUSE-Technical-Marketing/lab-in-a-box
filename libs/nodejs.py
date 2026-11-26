#!/usr/bin/env python3
# Part of lab-in-a-box — Node.js on a lab node.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/nodejs.py: install Node.js and npm packages on a node, whatever distribution it runs.

Every add-on whose tool is a Node program needs the same two things: a Node and an npm from the node's own
repositories, under whichever names that distribution uses, and a global npm install that works both as root and
as an ordinary user. install_anthropic_tools needed it first, install_hyperframes second, so it lives here.

A distribution that carries none of the usual names is reported as a missing package by install_packages(), which
is what should happen: lab-in-a-box never adds a third-party repository to a node behind the operator's back.
"""

import shlex

from lab_creation import install_packages, package_available, package_manager

# The packages Node needs, named as each package manager's own distribution names them. A tuple holds
# alternatives, tried in order against the node's repositories, for a distribution that carries none of the others.
NODE_PACKAGES = {
    "zypper": (["nodejs-default", "npm-default"], ["nodejs22", "npm22"], ["nodejs20", "npm20"]),
    "transactional-update": (["nodejs-default", "npm-default"], ["nodejs22", "npm22"], ["nodejs20", "npm20"]),
    "dnf": (["nodejs", "npm"],), "yum": (["nodejs", "npm"],), "apt-get": (["nodejs", "npm"],),
    "apk": (["nodejs", "npm"],),
}


def first_available(host: str, candidates, manager: str) -> list:
    """The first package set of `candidates` this node's repositories carry, or the last as the error to report."""
    sets = candidates or ([],)
    for packages in sets:
        if all(package_available(host, pkg, manager=manager) for pkg in packages):
            return packages
    return list(sets[-1])


def ensure_node(host: str, manager: str = "") -> str:
    """
    Install Node and npm on `host` from its own repositories, and return the package manager used. A node that
    already has both is left alone, so this is safe to call from every add-on that needs Node.
    """
    manager = manager or package_manager(host)
    install_packages(host, first_available(host, NODE_PACKAGES.get(manager), manager), manager=manager)
    return manager


def npm_command(user: str, package: str) -> str:
    """The command that installs `package` globally, under the running user's own prefix when that is not root."""
    install = "npm install -g {}".format(shlex.quote(package))
    if user == "root":
        return install
    return 'npm config set prefix "$HOME/.local" && {}'.format(install)
