#!/usr/bin/env python3
# Unreferenced library functions — see 83_dead_functions.sh.
#
# A function counts as referenced when code outside tests/ names it: `module.func` after `import module [as alias]`,
# `from module import func`, or a bare `func` inside its own module. Code that reaches a function another way names it
# in DYNAMIC with the reason. A function that exists for tests only names it in TEST_HOOKS with its purpose.
import ast
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
SKIP = ("tests", "legacy_bash", "python_migration", ".git")
EXTRA = ("scripts/lab_schema",)
DYNAMIC = {
    ("primary", "validate_definition"): "webui/lib/discovery.py calls it on the module _primary_lib() loads",
}
TEST_HOOKS = {
    ("primary", "clear_passphrase_cache"): "tests reset the cached credential passphrase between cases",
}


def sources() -> list:
    """Every Python source of the repository outside SKIP."""
    files = [p for p in _REPO.rglob("*.py") if p.relative_to(_REPO).parts[0] not in SKIP]
    return files + [_REPO / e for e in EXTRA]


def library_functions(libs: dict) -> set:
    """(module, name) of every module-level function in `libs` (module -> path)."""
    return {(mod, n.name) for mod, path in libs.items() for n in ast.parse(path.read_text()).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def references(path: Path, libs: dict, funcs: set) -> set:
    """The (module, name) pairs of `funcs` that the source at `path` references."""
    tree = ast.parse(path.read_text())
    own = path.stem if path.parent == _REPO / "libs" else None
    alias, found = {}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            alias.update({a.asname or a.name: a.name for a in n.names if a.name in libs})
        elif isinstance(n, ast.ImportFrom) and n.module in libs:
            found |= {(n.module, a.name) for a in n.names}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
            found.add((alias[n.value.id], n.attr))
        elif isinstance(n, ast.Name) and own:
            found.add((own, n.id))
    return found & funcs


libs = {p.stem: p for p in (_REPO / "libs").glob("*.py")}
funcs = library_functions(libs)
used = set(DYNAMIC) | set(TEST_HOOKS)
for src in sources():
    used |= references(src, libs, funcs)
dead = sorted(f for f in funcs - used if not f[1].startswith("__"))
stale = sorted((set(DYNAMIC) | set(TEST_HOOKS)) - funcs)
for mod, name in dead:
    print("FAIL: libs/{}.py {}() is referenced by no code outside tests/".format(mod, name))
for mod, name in stale:
    print("FAIL: DYNAMIC or TEST_HOOKS names libs/{}.py {}(), which does not exist".format(mod, name))
if dead or stale:
    print("{} check(s) failed".format(len(dead) + len(stale)))
    sys.exit(1)
print("all dead-function checks passed")
