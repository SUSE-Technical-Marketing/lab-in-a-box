#!/usr/bin/env python3
# Best-effort static lint for the mandatory safety rule on addon_common.die_not_ready(): the
# dependency check that decides to call it must be the FIRST thing an addon does, before any
# direct ssh_run() call in the same function — see die_not_ready()'s own docstring in
# libs/addon_common.py, and "Dependencies between addons" in docs/src/addons.html.
#
# This is a heuristic, not a proof: it can only see a DIRECT ssh_run(...) call textually earlier
# in the same function as the die_not_ready(...) call. It cannot see through a helper function the
# addon calls that itself does a mutating ssh_run — by this codebase's own convention, though, a
# genuine read-only dependency probe is usually its own small, dedicated helper (e.g.
# appco_common.rancher_is_present()), while a direct ssh_run(...) call inline in the addon's own
# function body is typically the mutating action the rule exists to protect against running twice
# on retry. Run from 119_dependency_check_first.sh, in its own container — see tests/run_tests.sh.
import ast
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
from apps import addon_files as _addon_files  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _is_named_call(node, name):
    """Whether Call node `node` invokes a function literally named `name` — bare (Name) or
    qualified (Attribute, e.g. `ac.name`/`addon_common.name`), matched by its final component."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == name
    if isinstance(func, ast.Attribute):
        return func.attr == name
    return False


def problems_in_source(source: str, label: str) -> list:
    """Every (function_name, die_not_ready_lineno) pair in `source` where a direct ssh_run(...)
    call appears, textually, at an earlier line in the SAME function. `label` is only used to
    build the returned problem strings (e.g. a file path)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        die_not_ready_lines = sorted(
            n.lineno for n in ast.walk(node) if _is_named_call(n, "die_not_ready"))
        if not die_not_ready_lines:
            continue
        first_die_not_ready = die_not_ready_lines[0]
        ssh_run_lines_before = [
            n.lineno for n in ast.walk(node)
            if _is_named_call(n, "ssh_run") and n.lineno < first_die_not_ready
        ]
        if ssh_run_lines_before:
            problems.append(
                "{}: function '{}' calls ssh_run() at line(s) {} before its own die_not_ready() "
                "at line {} — the dependency check must come first".format(
                    label, node.name, ssh_run_lines_before, first_die_not_ready))
    return problems


all_problems = []
for addon, path in _addon_files(str(_REPO / "scripts")).items():
    source = Path(path).read_text(errors="replace")
    if "die_not_ready" not in source:
        continue  # fast skip: this addon doesn't use the mechanism at all
    all_problems.extend(problems_in_source(source, "scripts/install_{}".format(addon)))

for p in all_problems:
    check(p, False)

if not all_problems:
    print("no caller of die_not_ready() has an ssh_run() call ordered before it, in {} add-on(s) "
          "using the mechanism".format(
              sum(1 for _, path in _addon_files(str(_REPO / "scripts")).items()
                  if "die_not_ready" in Path(path).read_text(errors="replace"))))


# ── Self-test: the checker itself, against synthetic good/bad snippets ──────
good = (
    "def setup_thing(hostname, cfg):\n"
    "    standalone = cfg.get('x')\n"
    "    if not standalone and not rancher_is_present(hostname):\n"
    "        ac.die_not_ready('not ready')\n"
    "    ssh_run(hostname, 'do the real work')\n"
)
check("self-test: a die_not_ready() with no ssh_run() before it is clean",
      problems_in_source(good, "synthetic") == [])

bad = (
    "def setup_thing(hostname, cfg):\n"
    "    ssh_run(hostname, 'mutating step, done too early')\n"
    "    if not rancher_is_present(hostname):\n"
    "        ac.die_not_ready('not ready')\n"
)
check("self-test: an ssh_run() call before die_not_ready() is flagged",
      len(problems_in_source(bad, "synthetic")) == 1)

unrelated = (
    "def setup_other(hostname, cfg):\n"
    "    ssh_run(hostname, 'totally unrelated function, no die_not_ready here at all')\n"
)
check("self-test: a function with no die_not_ready() call is never flagged, regardless of ssh_run()",
      problems_in_source(unrelated, "synthetic") == [])

two_functions = (
    "def a(hostname):\n"
    "    ssh_run(hostname, 'mutating, but in a DIFFERENT function from the one below')\n"
    "\n"
    "def b(hostname):\n"
    "    ac.die_not_ready('not ready')\n"
)
check("self-test: an ssh_run() in a DIFFERENT function never triggers a false positive",
      problems_in_source(two_functions, "synthetic") == [])


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All dependency-check-first checks passed")
