#!/usr/bin/env python3
# Part of lab-in-a-box, installs the lab-in-a-box automation system
# Author/s: Raul Mahiques
# License: GPLv3
#
# The automation-system entrypoint; install_demo_server_scripts.sh installs Python and runs
# this by default. Reuses setup_kvm_node.py's own building blocks (Asker, build_values,
# prepare_kvm_host, deploy_automation_node, run_remote) as a sibling module — see that file for
# what each one actually does; this script is about priority and sequencing, not new mechanism.

"""
setup_automation.py — install the lab-in-a-box automation system.

Usage:
    setup_automation.py [options] [TARGET]       (see --help for every option)

Building the automation node (vm/container/kubernetes) is the one mandatory step here — unlike
setup_kvm_node.py, where KVM-host setup is the mandatory step and the automation node is the
optional add-on. Without setup_demo_server/automation.cfg, it is written first from the options
and this host's own settings; with --interactive each value is asked for, with those as
defaults. An existing automation.cfg is used as is; options that map to its keys update them in
place.

A VM-mode automation node needs a KVM host under it: if this host isn't already one,
setup_kvm_host.py's prepare_kvm_host() runs first, automatically — it's not a separate question
for vm mode, since there is no automation node without it. For container/kubernetes mode, a KVM
host is a genuinely optional add-on, asked about (default: no) rather than assumed. In
interactive mode, adding cloud-provider credentials (scripts/setup_credentials.py) is offered
too — optional, independently re-runnable later.

TARGET installs onto that remote host over SSH instead: setup_demo_server/ and libs/ are copied
there and install_demo_server_scripts.sh runs with the same options (install_demo_server_scripts.sh
calls this script by default, same as a local run).
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
for _candidate in ("/usr/local/lib/lab_creation", str(_SCRIPT_DIR.parent / "libs"),
                   str(_SCRIPT_DIR.parent / "scripts")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import kvm_host_profiles  # noqa: E402
import setup_credentials  # noqa: E402
import setup_kvm_node as skn  # noqa: E402


def parse_args(argv):
    """setup_kvm_node.py's shared option set (build_arg_parser()), plus this script's one extra
    flag: whether to also prepare this host as a KVM hypervisor (mandatory for vm mode either
    way; optional, default no, for container/kubernetes)."""
    p = skn.build_arg_parser(
        "setup_automation.py",
        "Install the lab-in-a-box automation system on this host (or TARGET over SSH): build "
        "the automation node (vm/container/kubernetes), write automation.cfg and, when needed "
        "or asked for, kvm_host.cfg too. Without automation.cfg, it is written from the options "
        "and the host's own settings; --interactive asks for each value.")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--kvm-host", dest="kvm_host", action="store_true", default=None,
                       help="also prepare this host as a KVM hypervisor (default: yes if "
                            "--automation-node is vm, no otherwise)")
    group.add_argument("--no-kvm-host", dest="kvm_host", action="store_false",
                       help="never prepare this host as a KVM hypervisor, even for vm mode "
                            "(fails if vm mode actually needs one and it isn't ready already)")
    return skn._finish_parse(p, argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if args.target:
        skn.run_remote(args.target, argv)
        return

    profile = kvm_host_profiles.detect_profile()
    automation_path = skn._find("automation.cfg")
    kvm_path = skn._find("kvm_host.cfg")
    if not args.reconfigure and not automation_path.is_file():
        skn._auto_split_legacy_lab_cfg(automation_path, kvm_path)
    generated_password = ""
    ask = skn.Asker(interactive=args.interactive)
    fresh = args.reconfigure or not automation_path.is_file()
    if fresh:
        automation_values, kvm_values, generated_password = skn.build_values(args, ask, profile.name if profile else "")
    else:
        automation_values, kvm_values = {}, {}
    skn._write_cfg(automation_path, "automation.cfg.template", fresh, automation_values,
                   skn._automation_flag_values(args))
    automation_cfg = primary.load_shell_vars(automation_path)
    node_mode = automation_cfg.get("_automation_node", "") or "vm"

    # A VM-mode automation node needs a KVM host under it, full stop — not a question. For
    # container/kubernetes, it's a genuinely optional add-on (default no): most automation nodes
    # of that shape don't also need this host to become a hypervisor.
    want_kvm_host = args.kvm_host
    if want_kvm_host is None:
        want_kvm_host = True if node_mode == "vm" else ask(
            "Also set up this host as a KVM hypervisor?", "no", ("yes", "no")) == "yes"
    if want_kvm_host:
        skn._write_cfg(kvm_path, "kvm_host.cfg.template", fresh, kvm_values, skn._kvm_flag_values(args))
        kvm_cfg = primary.load_shell_vars(kvm_path)
    else:
        kvm_cfg = {}
    cfg = dict(kvm_cfg, **automation_cfg)

    print("\nautomation node {} ({}) at {}, gateway {}, DNS {}{}".format(
        cfg.get("AUTOMATION_HOSTNAME", ""), node_mode, cfg.get("_myip", ""), cfg.get("_mygw", ""),
        cfg.get("_mydns", ""), " — also setting up this host as a KVM hypervisor" if want_kvm_host else ""))
    if args.interactive:
        skn.review_cfg([automation_path, kvm_path] if want_kvm_host else [automation_path])
    if not args.yes and input("Install with these settings? (yes/no): ").strip() != "yes":
        print("Nothing changed. Edit {}{} or run again with different options.".format(
            automation_path, " / {}".format(kvm_path) if want_kvm_host else ""))
        return
    # Reload in case the operator edited a file during the pause above.
    automation_cfg = primary.load_shell_vars(automation_path)
    kvm_cfg = primary.load_shell_vars(kvm_path) if want_kvm_host else {}
    cfg = dict(kvm_cfg, **automation_cfg)
    skn.write_lab_cfg(cfg)

    if want_kvm_host:
        skn.prepare_kvm_host(cfg, _SCRIPT_DIR)
    elif node_mode == "vm":
        die_msg = ("_automation_node is \"vm\" but this host was not asked to become a KVM hypervisor — "
                   "pass --kvm-host, or choose a different --automation-node.")
        skn.die(die_msg)
    skn.deploy_automation_node(cfg, _SCRIPT_DIR)
    if generated_password:
        print("\nGenerated root password for the automation node: {}  (also in {})".format(
            generated_password, automation_path))

    if args.interactive:
        if ask("Add cloud credentials now?", "no", ("yes", "no")) == "yes":
            setup_credentials.interactive_build(primary.credentials_dirs()[0])


if __name__ == "__main__":
    main()
