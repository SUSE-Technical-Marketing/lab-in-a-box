#!/usr/bin/env python3
# Part of lab-in-a-box — the shared WireGuard overlay hub's own maintenance operations
# Author/s: Raul Mahiques
# License: GPLv3
"""
overlay_ctl.py: what the overlay hub holds, and what to take out of it.

Joining an overlay and routing a node happen on their own, as part of setup_vm.py. The operations here are the ones
nobody can do automatically, because they are about a site whose lab is already gone:

    overlay_ctl.py --hub <host> list              what the hub knows: sites, nodes and the addresses it handed out
    overlay_ctl.py --hub <host> refresh           re-push every registered node's routes
    overlay_ctl.py --hub <host> retire <site>     take a site out: its peer, the hub's route for its subnet, its
                                                  lines in every registry, and the routes other sites' nodes hold
    overlay_ctl.py --hub <host> forget <node>     drop one node from the hub's node list

<host> is the hub's gateway VM, the one OVERLAY_HUB_ACCOUNT names ("lab-overlay-gw-<backend>[-<account>]"), by name
or address. It is given rather than looked up, so this never creates a hub that is not there.

The hub is shared with every lab that uses the overlay, so `retire` prints what it is about to do and asks, unless
--yes is given. Nothing here touches a VM: destroying a site's machines is destroy_lab.py's work.
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import overlay  # noqa: E402
import primary  # noqa: E402
from lab_creation import log, die  # noqa: E402

USAGE = (
    "Usage: overlay_ctl.py --hub <host> list\n"
    "       overlay_ctl.py --hub <host> refresh [<site>]\n"
    "       overlay_ctl.py --hub <host> retire <site> [--yes]\n"
    "       overlay_ctl.py --hub <host> forget <node>\n"
)


def read_answer():
    """
    The y/N answer to `retire`, read from the controlling TTY. Its own function so a test can replace it without a
    real /dev/tty, the same convention as backends.py's own prompt.
    """
    with open("/dev/tty") as tty:
        return tty.readline().strip()


def cmd_list(hub):
    """Print the hub's own registries: the sites, the nodes routed through each, and the overlay addresses."""
    sites = overlay.list_other_sites(hub)
    nodes = overlay.list_nodes(hub)
    print("Sites ({}):".format(len(sites)))
    for name, cidr in sites:
        own = [n for n in nodes if n[0] == name]
        print("  {:<28} {:<18} {} node(s)".format(name, cidr, len(own)))
    print("Nodes ({}):".format(len(nodes)))
    for site_name, node_host, gateway in nodes:
        print("  {:<40} site {:<24} via {}".format(node_host, site_name, gateway))
    return 0


def cmd_refresh(hub, site=None):
    refreshed, unreachable = overlay.refresh_routes(hub, only_site=site)
    print("Refreshed {} node(s); {} unreachable".format(refreshed, unreachable))
    return 1 if unreachable else 0


def cmd_retire(hub, site, assume_yes):
    sites = dict(overlay.list_other_sites(hub))
    if site not in sites:
        die("the overlay hub has no site '{}' — run 'list' to see what it holds".format(site))
    nodes = overlay.list_nodes(hub, site_name=site)
    print("About to retire site '{}' ({}) from the overlay hub {}:".format(site, sites[site], hub))
    print("  its gateway stops being a peer, and the hub stops routing {}".format(sites[site]))
    print("  {} of its node(s) are forgotten".format(len(nodes)))
    print("  every other site's nodes are re-routed, so the subnet goes off them")
    print("  no VM is touched")
    if not assume_yes:
        print("Retire it? [y/N] ", end="", flush=True)
        if read_answer()[:1] not in ("y", "Y"):
            log("- Left the overlay hub as it was")
            return 1
    overlay.retire_site(hub, site)
    return 0


def main():
    args = sys.argv[1:]
    if primary.wants_help(args) or not args:
        print(__doc__.strip() + "\n\n" + USAGE)
        return 0
    if "--version" in args:
        print(__version__)
        return 0

    assume_yes = False
    for flag in ("--yes", "-y"):
        if flag in args:
            assume_yes = True
            args.remove(flag)
    if "--hub" not in args:
        print(USAGE, file=sys.stderr)
        die("--hub <host> is required: the overlay hub's own gateway VM")
    hub = args.pop(args.index("--hub") + 1) if args.index("--hub") + 1 < len(args) else ""
    args.remove("--hub")
    if not hub:
        die("--hub needs the hub host")
    if not args:
        print(USAGE, file=sys.stderr)
        die("no operation given")

    action, rest = args[0], args[1:]
    if action == "list":
        return cmd_list(hub)
    if action == "refresh":
        return cmd_refresh(hub, rest[0] if rest else None)
    if action == "retire":
        if not rest:
            die("retire needs the site name")
        return cmd_retire(hub, rest[0], assume_yes)
    if action == "forget":
        if not rest:
            die("forget needs the node name")
        overlay.forget_node(hub, rest[0])
        log("- '{}' is no longer in the overlay hub's node list".format(rest[0]))
        return 0
    print(USAGE, file=sys.stderr)
    die("unknown operation '{}'".format(action))


if __name__ == "__main__":
    sys.exit(main())
