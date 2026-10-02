#!/usr/bin/env python3.11
"""
vm_power.py — power state / start / stop of a lab's VMs, whatever backend
created them (libvirt KVM hosts are usually handled with virsh directly; this
is mainly for cloud backends).

Usage:
    vm_power.py <lab.json> status [vm ...]
    vm_power.py <lab.json> start  [vm ...]
    vm_power.py <lab.json> stop   [vm ...]

Prints one JSON object, {vm name: state}. A backend without power support
reports "unsupported"; any other failure reports "error: <message>" and makes
the exit code 1. Without vm names: every node of the lab.
"""
__version__ = "__LABVERSION__"

import json
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import primary  # noqa: E402

ACTIONS = ("status", "start", "stop")


def power(definition, config, action, vm_names):
    """{vm: state-or-message} for `action` on each VM."""
    result = {}
    for vm_name in vm_names:
        try:
            backend = backends.get_backend(definition, config, vm_name, for_existing=True)
            if action == "start":
                backend.start_vm(vm_name)
            elif action == "stop":
                backend.stop_vm(vm_name)
            result[vm_name] = backend.vm_state(vm_name)
        except NotImplementedError:
            result[vm_name] = "unsupported"
        except (SystemExit, RuntimeError) as e:
            result[vm_name] = "error: {}".format(e)
    return result


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)
    if len(args) < 2 or args[1] not in ACTIONS:
        print("Usage: {} <lab.json> {{{}}} [vm ...]".format(Path(sys.argv[0]).name, "|".join(ACTIONS)),
              file=sys.stderr)
        sys.exit(2)
    definition = primary.load_definition(args[0])
    config = primary.load_config()
    vm_names = args[2:] or list(definition.get("nodes", {}))
    result = power(definition, config, args[1], vm_names)
    print(json.dumps(result, indent=1, sort_keys=True))
    sys.exit(1 if any(str(v).startswith("error:") for v in result.values()) else 0)


if __name__ == "__main__":
    main()
