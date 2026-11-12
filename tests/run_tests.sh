#!/bin/bash
# lab-in-a-box test runner — every check runs in its OWN independent,
# disposable podman container (tests/Containerfile): one `podman run` per
# script under tests/checks/, never bundled together. A crash, hang, or
# leftover state in one check can't touch any other. Wired to run
# automatically before each commit — see .githooks/pre-commit (enable once
# per clone: git config core.hooksPath .githooks).
#
# Run manually (always the full suite):
#   tests/run_tests.sh
#
# Scoped to what changed (used by .githooks/pre-commit and CI's test-suite job): set
# TESTS_CHANGED_FILES to a newline-separated list of repo-relative paths before calling this
# script, and only the checks that declare a dependency on one of them (plus every check that
# declares none — see tests/select_checks.py) run. Unset or empty: the full suite, same as a
# manual run.
#
# Add a new check: drop a new executable script into tests/checks/ — it's
# picked up automatically, no edits needed here. Give it a "# Tests-paths: ..." comment line
# (see tests/select_checks.py) if it only ever needs to run for changes to specific files;
# leave it out if it's a cross-cutting/structural check that must run on any code change.
# -uo pipefail (no -e): a failing check must not abort the loop — it's
# recorded in _failed and the run continues, so one broken check doesn't
# hide the results of every check after it.
set -uo pipefail
# Always run from the repo root regardless of the caller's cwd, since every
# podman invocation below bind-mounts "$(pwd)" as the repo.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit

_image="lab-in-a-box-tests"

# podman is a hard requirement, not an optional speedup — there is no
# no-container fallback path (see feedback: tests only run in containers,
# never against the host).
if ! command -v podman >/dev/null 2>&1; then
    echo "podman is required to run tests (every check runs in its own container) — see tests/Containerfile" >&2
    exit 1
fi

# One shared image for every check (built once, reused per `podman run`
# below). podman's own layer cache makes this a no-op rebuild when
# tests/Containerfile and its deps haven't changed.
echo "== building test image (cached — fast if Containerfile is unchanged) =="
if ! podman build -q -t "$_image" -f tests/Containerfile . ; then
    echo "podman build failed" >&2
    exit 1
fi

_status=0
_failed=()

# Scoped selection: TESTS_CHANGED_FILES (newline-separated) picks which checks run; unset/empty
# means every check, exactly like today. select_checks.py's own default (no files given) is
# also "everything", so this is a no-op when the variable isn't set.
if [[ -n "${TESTS_CHANGED_FILES:-}" ]]; then
    mapfile -t _selected < <(printf '%s' "$TESTS_CHANGED_FILES" | python3 tests/select_checks.py -)
    echo "Scoped run: ${#_selected[@]} of $(ls tests/checks/*.sh | wc -l) checks selected for the changed files"
else
    mapfile -t _selected < <(cd tests/checks && ls *.sh)
fi

# Each tests/checks/*.sh gets its OWN fresh container (--rm), so state from
# one check (files it wrote, env it changed) never leaks into the next.
# The repo is bind-mounted read-only (:ro) — a check that needs to write
# does so inside the container's own filesystem, never back onto the host
# checkout. Discovery is filesystem globbing, not a hardcoded list: drop a
# new script into tests/checks/ and it runs on the next invocation.
for name in "${_selected[@]}"; do
    script="tests/checks/$name"
    echo
    echo "############################################"
    echo "# $name (independent container)"
    echo "############################################"
    if ! podman run --rm -v "$(pwd)":/repo:ro -w /repo "$_image" bash "$script"; then
        _status=1
        _failed+=("$name")
    fi
done

# Summarise once at the end rather than failing fast, so a single run always
# reports the full picture (which checks failed, which passed) instead of
# stopping at the first failure.
echo
if [[ $_status -eq 0 ]]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED: ${_failed[*]}"
fi
exit $_status
