"""
reconcile.py — bring what a lab has written down about its nodes' addresses back in line with what their providers
report now.

Why it exists: a cloud provider that releases an instance's auto-assigned public address when the instance stops
hands back a different one when it starts. Everything derived from that address is then wrong — the lab's A records
on every master of its zone, the LAB_HOSTS_FILE line, and the resolver the guest itself uses. Starting a lab with
scripts/vm_power.py already refreshes all of that; a restart from the provider's own console, or an instance that
crashed and came back by itself, does not.

So every entrypoint that acts on an existing lab brings its nodes current, rather than leaving it to an operator
to remember a separate command. scripts/setup_lab.py and scripts/vm_power.py's start already register every node
they touch, through the same refresh this module calls; scripts/check_lab.py and scripts/reconcile_lab.py come
here, which compares first and registers only what moved.

What this does not cover: the WireGuard Endpoint in each site gateway's config is the overlay hub's public
address, and the hub reserves its own address on every backend that can reserve one
(backends.ensure_infra_address()), so that Endpoint cannot go stale there. Where a backend reserves nothing and
also releases the address on stop it can, and nothing here can repair it: no record says which address the spokes
were given, and a gateway whose tunnel is down cannot be reached through that tunnel to be rewritten. Running the
lab again rebuilds them, through overlay.ensure_overlay_site_gateway().

The DNS refresh itself is scripts/setup_vm.py's refresh_kept_node_dns(), which registers a node in every master of
the zone, rewrites its hosts-file line and re-points the guest's own resolver. It lives there because it needs the
whole provisioning path, and scripts/ is not importable from libs/, so callers pass it in as `refresh_node`.
"""

from typing import Callable, Dict, Iterator, Tuple

import backends
import dns_zone
import primary
from lab_creation import load_vm_vars, log, warn


def recorded_address(mydomain: str, vm_name: str) -> str:
    """
    The address this host's own copy of the lab's forward zone gives `vm_name`, or "" when it has none there (a
    node never registered, or a lab whose zone this host does not serve).

    This is the record that matters: it is what every name in the lab resolves to, and what a moved address breaks.
    The lab definition is not the record, because a cloud node's address is never in the file on disk; it is read
    from the provider on each run.
    """
    if not mydomain:
        return ""
    try:
        with open(dns_zone.zone_path(dns_zone.forward_file(mydomain)), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    return dns_zone.node_address(text, vm_name.split(".")[0])


def ensure_node_address(backend, backend_name: str, vm_name: str, env: dict) -> str:
    """
    Reserve `vm_name`'s public address when its lab definition asks for one (static_ip, see
    backends.static_ip_requested()), and return the address in effect afterwards: "" when nothing was reserved, so
    the caller keeps the address it already had.

    Called from the two places a node's address is established: scripts/setup_vm.py's provision_vm() when the node
    is created, and its refresh_kept_node_dns() for a node that already exists. So a node that predates the flag,
    or whose reservation was removed by hand, gets one the next time a run brings it current (reconcile(),
    scripts/vm_power.py's start, scripts/setup_lab.py's --keep) rather than only when it is next rebuilt.
    """
    if not backends.static_ip_requested(env):
        return ""
    address = backend.ensure_stable_address(vm_name)
    if not address:
        warn("- static_ip is \"true\" for '{}' but backend '{}' reserves no address: it either holds an "
             "instance's address for that instance's whole lifetime, or offers only an additional address the "
             "guest has to configure itself (see backends.VMBackend.ensure_stable_address())".format(
                 vm_name, backend_name))
    return address


def _node_context(definition, config, vm_names=None) -> Iterator[Tuple[str, dict, object]]:
    """
    Yield (vm name, merged variables, backend) for each cloud node of the lab, in definition order, narrowed to
    `vm_names` when given. Only cloud nodes: a libvirt or Harvester VM is at the `myip` the definition gives it,
    which no restart changes.
    """
    defaults = primary.load_defaults()
    nodes = list((definition.get("nodes") or {}).keys())
    if vm_names:
        wanted = set(vm_names)
        nodes = [vm for vm in nodes if vm in wanted]
    for vm_name in nodes:
        if backends.effective_backend_name(definition, config, vm_name) not in backends.CLOUD_BACKEND_NAMES:
            continue
        env = {}
        env.update(defaults)
        env.update(config)
        env.update(load_vm_vars(definition, vm_name))
        yield vm_name, env, backends.get_backend(
            definition, config, vm_name, for_existing=True,
            vm_img_loc=defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/"),
            iso_loc=defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources"),
            lab_setup_path=defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation"))


def _addresses(backend, vm_name: str, env: dict) -> Tuple[str, str]:
    """
    (address the zone records for `vm_name`, address its provider reports now). The second is "" when the provider
    reports none, which is a node that is destroyed or still starting rather than one whose address moved, and
    there is then no address to register it at either.
    """
    return recorded_address(env.get("mydomain", ""), vm_name), (backend.get_ip(vm_name) or "")


def drift(definition, config, vm_names=None) -> Dict[str, Tuple[str, str]]:
    """
    {vm name: (address in the zone, address the provider reports)} for every cloud node of the lab whose two
    disagree, read-only. A node with no record yet is included with "" as its recorded address, since registering
    it is the same work as correcting it.
    """
    out = {}
    for vm_name, env, backend in _node_context(definition, config, vm_names):
        recorded, actual = _addresses(backend, vm_name, env)
        if actual and recorded != actual:
            out[vm_name] = (recorded, actual)
    return out


def reconcile(definition, config, refresh_node: Callable, vm_names=None,
              always: bool = False) -> Dict[str, str]:
    """
    Bring every cloud node whose record no longer matches what its provider reports back in line, by calling
    `refresh_node(definition, config, defaults, vm_name)` for it — in practice scripts/setup_vm.py's
    refresh_kept_node_dns(), which rewrites that node's records on every master of the zone, its hosts-file line,
    the resolver inside the guest, and reserves its address when the lab asked for a fixed one.

    `always` refreshes every node rather than only the ones that moved (reconcile_lab.py's --all), for a lab whose
    guests lost their own resolver configuration rather than their addresses, and to apply a static_ip added to a
    definition after the nodes were built.

    Returns {vm name: "<recorded> -> <current>"} for the nodes whose address had moved, plus an entry for any node
    that could not be brought current. Empty when nothing moved and nothing failed. To see what would change
    without changing it, call drift().

    One node that cannot be refreshed is reported and the rest are still done.
    """
    defaults = primary.load_defaults()
    report = {}
    for vm_name, env, backend in _node_context(definition, config, vm_names):
        recorded, actual = _addresses(backend, vm_name, env)
        if not actual:
            continue   # no address to register it at: destroyed, or not up yet
        moved = recorded != actual
        if moved:
            report[vm_name] = "{} -> {}".format(recorded or "no record", actual)
        if not (moved or always):
            continue
        if moved:
            log("- Address of \"{}\" is {}, its records say {}: registering it again".format(
                vm_name, actual, recorded or "nothing"))
        try:
            refresh_node(definition, config, defaults, vm_name)
        except (SystemExit, RuntimeError, OSError) as e:
            warn("- could not bring '{}' current: {}".format(vm_name, e))
            report[vm_name] = "{} (not applied: {})".format(report.get(vm_name, "no change needed"), e)
    return report
