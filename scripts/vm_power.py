#!/usr/bin/env python3.11
"""
vm_power.py — power state / start / stop of a lab's VMs, whatever backend
created them (libvirt KVM hosts are usually handled with virsh directly; this
is mainly for cloud backends, where a stopped lab costs less than a running one
and far less than one left running by accident).

Usage:
    vm_power.py <lab-file> status [vm ...]
    vm_power.py <lab-file> start  [vm ...]
    vm_power.py <lab-file> stop   [vm ...]
    vm_power.py --example <name> {status|start|stop} [vm ...]

Options:
    --cloud-account <name>  use cloud account <name> for every cloud node of this run,
                            taking precedence over "cloud_account" in the definition.
    --example <name>        act on the lab built by setup_lab.py --example <name>, for
                            which no lab file of your own exists.
    --no-wait               do not wait for each VM to reach the state asked for, and do
                            not order the lab's own VMs (below). One call per VM, as this
                            command behaved before ordering existed.
    --no-dns                on start, do not register the lab's DNS again. Only useful when
                            every node has a fixed address.
    --with-infra            on stop, also stop the supporting VMs the lab brought up for itself
                            but does not declare (each site's cloud DNS VM, each site's overlay
                            gateway). They are left running by default, because they are shared
                            by every lab on that cloud account and stopping them takes the other
                            labs' DNS down with this one. Ignored by start and status.

Prints one JSON object, {vm name: state}. A backend without power support
reports "unsupported"; any other failure reports "error: <message>" and makes
the exit code 1. Without vm names: every node of the lab, plus the VMs the lab
brought up for itself, and for start and stop in an order that suits the lab
rather than the file:

  stop   Kubernetes agents, then servers. The infrastructure VMs the lab brought
         up for itself (each site's cloud DNS VM and overlay gateway) are left
         running, since they are shared by every lab on that cloud account;
         --with-infra stops them too, after the lab's own nodes.
  start  the reverse, so the DNS and the overlay are up before the nodes that
         resolve and reach each other through them, and each cloud node's DNS
         records are written again afterwards, because a provider that releases
         a public address on stop hands back a different one on start.

With explicit vm names, those VMs are acted on in the order given and nothing
else is touched.

Stopping is not a saving everywhere: each backend says what it does to the bill
(backends.VMBackend.stop_billing_note()), printed on stderr with every stop.
"""
__version__ = "__LABVERSION__"

import atexit
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import k8s  # noqa: E402
import lab_creation as lc  # noqa: E402
import lab_examples  # noqa: E402
import overlay  # noqa: E402
import primary  # noqa: E402

ACTIONS = ("status", "start", "stop")

# How long a VM is given to reach the state asked for before the next group of the lab is acted on.
WAIT_SECONDS = backends.POWER_WAIT_SECONDS

# The states that count as having arrived, per action. The vocabulary itself is backends', since the same
# spellings decide whether a VM that exists is merely powered off (backends._ensure_cloud_dns_vm()).
_ARRIVED = {
    "start": backends.RUNNING_STATES,
    "stop": backends.POWERED_OFF_STATES,
}


def _arrived(action: str, state: str) -> bool:
    """True when `state` is one this backend reports for a VM that has finished `action`."""
    if (state or "").strip().lower() in ("not found", ""):
        # Nothing to wait for: a VM the provider no longer lists is as stopped as it gets, and a start
        # that cannot find it has already failed with its own message.
        return True
    return backends.state_matches(state, _ARRIVED.get(action, ()))


def lab_groups(definition: dict, action: str, infra=None) -> list:
    """
    The lab's own VMs as the groups to act on in order, for `action`. `infra` is infra_backends()' own answer,
    passed in so the provider is asked once per run rather than once per caller.

    Stopping goes from the outside in — Kubernetes agents, then servers, then the VMs the lab brought up for itself
    (each site's cloud DNS VM, each site's overlay gateway) — so a cluster loses its workers before its control
    plane, and the DNS and the overlay stay up while the nodes using them go down. Starting is the same list
    reversed, for the same reason.

    `infra` empty leaves that last group out altogether, which is what a stop without --with-infra passes: those
    VMs serve every lab on the cloud account, so this lab stopping them would take the others' DNS with it.
    """
    nodes = definition.get("nodes") or {}
    agents = [n for n, cfg in nodes.items() if k8s.node_role(cfg) != "server"]
    servers = [n for n in nodes if n not in agents]
    groups = [agents, servers, list(infra if infra is not None else infra_backends(definition))]
    groups = [g for g in groups if g]
    return groups if action == "stop" else [list(reversed(g)) for g in reversed(groups)]


def infra_to_act_on(action: str, infra: dict, with_infra: bool) -> dict:
    """
    The subset of `infra` (infra_backends()' answer) this run may power, as {vm name: backend}.

    A stop leaves them alone unless `with_infra`: the VMs a lab brings up for itself are shared by every lab on the
    same cloud account, so one lab stopping its site's DNS VM takes the other labs' name resolution down with it.
    A start always includes them, since bringing them up again is what the lab's own nodes need and costs no other
    lab anything, and status reads every one of them.
    """
    return infra if (action != "stop" or with_infra) else {}


def infra_backends(definition: dict) -> dict:
    """
    {vm name: backend} for the VMs this lab brought up for itself rather than declaring: each cloud site's DNS VM
    (backends.cloud_dns_vm_name()) and, for a lab with the overlay on, that site's gateway VM
    (overlay.hub_vm_name()). Named per account, since each account is its own cloud network, and listed once each
    however many nodes share them.

    The backend comes with the name because it cannot be found from the name: these VMs are not in the definition,
    so resolving one the way a node is resolved lands on whatever the lab's default backend is (libvirt), which
    would report every one of them as "not found" while the provider is still charging for it.
    """
    found = {}
    uses_overlay = bool((definition.get("common") or {}).get("overlay"))
    for _vm_name, backend, backend_name in _cloud_backends(definition):
        for name in ([backends.cloud_dns_vm_name(backend, backend_name)]
                     + ([overlay.hub_vm_name(backend_name, getattr(backend, "account", ""))]
                        if uses_overlay else [])):
            if name in found:
                continue
            try:
                if backend.vm_exists(name):
                    found[name] = backend
            except (RuntimeError, SystemExit) as e:
                # Asking the provider failed (expired credentials, a region that is unreachable). That is the
                # lab's own nodes' problem to report, one line each, rather than this list's to die on.
                print("- cannot tell whether '{}' exists: {}".format(name, e), file=sys.stderr)
    return found


def _cloud_backends(definition: dict) -> list:
    """
    [(node, backend, backend_name)] with one entry per distinct cloud account of the lab, so a lab's infrastructure
    VMs are looked for once per cloud network rather than once per node. A node on libvirt or Harvester has no such
    VMs and is left out.
    """
    out, seen = [], set()
    config = primary.load_config()
    for vm_name in definition.get("nodes") or {}:
        backend_name = backends.effective_backend_name(definition, config, vm_name)
        if backend_name not in backends.CLOUD_BACKEND_NAMES:
            continue
        try:
            backend = backends.get_backend(definition, config, vm_name, for_existing=True)
        except (RuntimeError, SystemExit):
            continue
        key = (backend_name, getattr(backend, "account", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append((vm_name, backend, backend_name))
    return out


def _wait(backend, vm_name: str, action: str) -> str:
    """`vm_name`'s state once it has finished `action`, or its last state after WAIT_SECONDS."""
    deadline = time.monotonic() + WAIT_SECONDS
    state = backend.vm_state(vm_name)
    while not _arrived(action, state) and time.monotonic() < deadline:
        time.sleep(backends.POWER_POLL_SECONDS)
        state = backend.vm_state(vm_name)
    return state


def power(definition, config, action, vm_names, wait=True, refresh_dns=True, infra=None):
    """
    {vm: state-or-message} for `action` on each VM of `vm_names`, which is a flat list of names or, from
    lab_groups(), a list of groups acted on one after another. `wait` holds each group until its VMs have reached
    the state asked for; `refresh_dns` writes a started node's DNS records again, since a cloud provider that
    released its public address on stop hands back a different one. `infra` is infra_backends()' answer: a VM the
    lab brought up for itself is acted on through the backend that owns it, since its name resolves to nothing.
    """
    infra = infra or {}
    groups = vm_names if vm_names and isinstance(vm_names[0], list) else [list(vm_names)]
    result = {}
    noted = set()
    started = []
    for group in groups:
        for vm_name in group:
            try:
                backend = infra.get(vm_name) or backends.get_backend(definition, config, vm_name,
                                                                      for_existing=True)
                if action == "start":
                    backend.start_vm(vm_name)
                elif action == "stop":
                    note = backend.stop_billing_note()
                    if note and note not in noted:
                        noted.add(note)
                        print("- {}".format(note), file=sys.stderr)
                    backend.stop_vm(vm_name)
                result[vm_name] = _wait(backend, vm_name, action) if (wait and action != "status") \
                    else backend.vm_state(vm_name)
                if action == "start" and _arrived(action, result[vm_name]):
                    started.append(vm_name)
            except NotImplementedError:
                result[vm_name] = "unsupported"
            except (SystemExit, RuntimeError) as e:
                result[vm_name] = "error: {}".format(e)
    if action == "start" and refresh_dns:
        _refresh_dns(definition, config, started, result)
    return result


def _refresh_dns(definition, config, started: list, result: dict) -> None:
    """
    Register each started node of the lab in the lab's DNS again, so the records point at the address the provider
    has just given it. Only the lab's own nodes: an infrastructure VM is registered by the step that uses it. A
    failure is reported against that node rather than ending the run, since the VM is up either way.
    """
    nodes = definition.get("nodes") or {}
    to_refresh = [vm for vm in started if vm in nodes]
    if not to_refresh:
        return
    from setup_vm import refresh_kept_node_dns     # imported here: it pulls in the whole provisioning path
    defaults = primary.load_defaults()
    for vm_name in to_refresh:
        try:
            refresh_kept_node_dns(definition, config, defaults, vm_name)
        except (SystemExit, RuntimeError, OSError) as e:
            result[vm_name] = "{} (DNS not updated: {})".format(result[vm_name], e)


def main():
    args = sys.argv[1:]
    if primary.wants_help(args):
        print(__doc__.strip())
        sys.exit(0)
    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    args = backends.take_cloud_account_option(args)
    try:
        args, example = lab_examples.take_example_option(args)
    except ValueError as e:
        lc.die(str(e))
    wait = "--no-wait" not in args
    refresh_dns = "--no-dns" not in args
    with_infra = "--with-infra" in args
    args = [a for a in args if a not in ("--no-wait", "--no-dns", "--with-infra")]

    if example:
        # The same definition setup_lab.py --example built from, in a temporary file this process removes.
        example_dir = tempfile.mkdtemp(prefix="lab-example-")
        atexit.register(shutil.rmtree, example_dir, True)
        try:
            lab_file = lab_examples.materialise(example, directory=example_dir)
        except (ValueError, FileNotFoundError) as e:
            lc.die("{} — setup_lab.py --list-examples lists the example labs".format(e))
        action, rest = (args[0] if args else ""), args[1:]
    else:
        lab_file, action, rest = (args[0] if args else ""), (args[1] if len(args) > 1 else ""), args[2:]

    if action not in ACTIONS:
        print("Usage: {} {{<lab-file>|--example <name>}} {{{}}} [vm ...]".format(
            Path(sys.argv[0]).name, "|".join(ACTIONS)), file=sys.stderr)
        sys.exit(2)

    definition = primary.load_definition(lab_file)
    config = primary.load_config()
    infra = infra_backends(definition)
    # `infra` itself is still what power() resolves backends through, so naming one of these VMs explicitly acts on
    # it through the backend that owns it; acted_infra only decides which of them this run picks up on its own.
    acted_infra = infra_to_act_on(action, infra, with_infra)
    if infra and not acted_infra:
        print("- leaving the supporting VMs this lab shares with every other lab on the same account running: {}"
              " (--with-infra stops them too)".format(", ".join(sorted(infra))), file=sys.stderr)
    if rest:
        vm_names = rest
    elif action == "status":
        # The lab's own VMs too: a DNS VM left running is exactly the cost this command exists to find.
        vm_names = list(definition.get("nodes", {})) + list(infra)
    elif wait:
        vm_names = lab_groups(definition, action, acted_infra)
    else:
        vm_names = list(definition.get("nodes", {})) + list(acted_infra)
    result = power(definition, config, action, vm_names, wait=wait, refresh_dns=refresh_dns, infra=infra)
    print(json.dumps(result, indent=1, sort_keys=True))
    sys.exit(1 if any(str(v).startswith("error:") for v in result.values()) else 0)


if __name__ == "__main__":
    main()
