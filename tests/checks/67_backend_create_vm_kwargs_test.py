#!/usr/bin/env python3
# setup_vm.py calls backend.create_vm() with one fixed set of keywords for every
# backend — see 67_backend_create_vm_kwargs.sh.
import ast
import inspect
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def create_vm_keywords():
    """Keywords of every `<x>.create_vm(...)` call in setup_vm.py."""
    tree = ast.parse((_REPO / "scripts" / "setup_vm.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_vm"):
            found.update(k.arg for k in node.keywords if k.arg)
    return found


keywords = create_vm_keywords()
check("setup_vm.py calls create_vm() with keywords", len(keywords) > 5)

subclasses = [c for c in vars(backends).values()
              if inspect.isclass(c) and issubclass(c, backends.VMBackend) and c is not backends.VMBackend]
check("backends defines VMBackend subclasses", "LibvirtBackend" in {c.__name__ for c in subclasses})

for cls in subclasses:
    params = inspect.signature(cls.create_vm).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        continue
    missing = sorted(keywords - set(params))
    check("{}.create_vm() accepts {}".format(cls.__name__, ", ".join(missing)), not missing)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all backend create_vm keyword checks passed")
