#!/usr/bin/env python3
"""
select_checks.py — which tests/checks/*.sh to run for a given set of changed files.

Each check may declare what it depends on with a comment line, anywhere in its own first 15
lines, of the form:

    # Tests-paths: libs/foo.py scripts/install_bar

Paths are repo-root-relative. A path naming a directory (or ending in "/") matches any file
under it; a bare filename matches exactly. A check with NO such line is always selected — this
is the safe, conservative default this project already documents (CLAUDE.md's "Test scope":
"until each check declares its dependencies, a code change runs the full suite for its
category"), so an un-annotated check is never silently skipped.

Usage:
    select_checks.py <file> [<file> ...]       changed files as arguments
    select_checks.py -                         changed files on stdin, one per line
    select_checks.py                           no changed files given: select everything

Prints the selected tests/checks/*.sh paths, one per line, to stdout.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKS_DIR = REPO / "tests" / "checks"
MARKER = "# Tests-paths:"
# Changes to the test harness itself, or to the harness's own CI wiring, affect every check's
# behavior — run everything rather than try to reason about which checks that touches.
HARNESS_FILES = {"tests/run_tests.sh", "tests/select_checks.py", ".githooks/pre-commit",
                  ".github/workflows/ci.yml"}


def declared_paths(check_path: Path):
    """The Tests-paths declared by `check_path`, or None if it declares none (always runs)."""
    with check_path.open() as f:
        for i, line in enumerate(f):
            if i >= 15:
                break
            line = line.strip()
            if line.startswith(MARKER):
                return line[len(MARKER):].split()
    return None


def matches(changed: str, declared: str) -> bool:
    if declared.endswith("/"):
        return changed == declared.rstrip("/") or changed.startswith(declared)
    return changed == declared or changed.startswith(declared + "/")


def select(changed_files) -> list:
    changed = list(changed_files)
    if not changed or any(c in HARNESS_FILES for c in changed):
        return sorted(p.name for p in CHECKS_DIR.glob("*.sh"))
    selected = []
    for check in sorted(CHECKS_DIR.glob("*.sh")):
        declared = declared_paths(check)
        if declared is None or any(matches(c, d) for c in changed for d in declared):
            selected.append(check.name)
    return selected


def main():
    args = sys.argv[1:]
    if args == ["-"]:
        changed = [line.strip() for line in sys.stdin if line.strip()]
    else:
        changed = args
    for name in select(changed):
        print(name)


if __name__ == "__main__":
    main()
