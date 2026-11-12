# Part of lab-in-a-box — installing lab-in-a-box itself onto a node, shared by
# scripts/install_lab_in_a_box (the addon's own concern) and scripts/install_rodeo_cli's
# optional tie-in (a rodeo-cli node needing a nested KVM host to deploy into).
# Author/s: Raul Mahiques
# License: GPLv3
import shlex

from lab_creation import ssh_run

DEFAULT_INSTALL_URL = ("https://raw.githubusercontent.com/SUSE-Technical-Marketing/"
                        "lab-in-a-box/main/install_demo_server_scripts.sh")


def lab_in_a_box_installed(hostname: str) -> bool:
    """True if lab-in-a-box's own scripts are already installed on `hostname` (install_automation_node_scripts.sh
    installs them to /usr/local/bin/ — see CLAUDE.md's "Two-tier architecture")."""
    r = ssh_run(hostname, "test -x /usr/local/bin/setup_lab.py", check=False)
    return r.returncode == 0


def install_lab_in_a_box(hostname: str, install_url: str = DEFAULT_INSTALL_URL, args=None) -> None:
    """
    Runs install_demo_server_scripts.sh on `hostname` over SSH (root). `args` (a list of strings,
    default ["--fetch-only"]) is passed through to it verbatim, each shlex-quoted individually.

    The default, --fetch-only, installs git+python and clones the repo but stops short of
    actually building a nested KVM hypervisor/automation node on `hostname` — that is a separate,
    resource-heavy, deliberate action (pass e.g. ["-y"] to build it unattended instead), matching
    this project's own rule that mutating/live actions need an explicit go-ahead, not a default.
    """
    argv = args if args else ["--fetch-only"]
    cmdline = " ".join(shlex.quote(a) for a in argv)
    print("- Installing lab-in-a-box on '{}' from {}".format(hostname, install_url))
    ssh_run(hostname, "curl -fsSL {} | bash -s -- {}".format(shlex.quote(install_url), cmdline))
