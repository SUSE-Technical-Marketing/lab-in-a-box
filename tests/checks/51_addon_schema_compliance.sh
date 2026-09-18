#!/bin/bash
# Every scripts/install_<addon>*.py must correctly handle --schema/--version
# (addon_common.handle_common_args, called as the first line of main()) —
# confirmed live 2026-09-17 that install_prometheus.py/install_grafana.py
# had never wired this up at all: `install_prometheus --schema` crashed with
# "ERROR: Lab definition file '--schema' not found" instead of printing the
# schema, since main() went straight to treating argv[1] as a lab.json path.
# Nothing caught this before a real user did — 22_addon_common_schema_test.py
# only exercises addon_common.print_schema() against a synthetic fixture, not
# any real install_<addon> script's own main(). This test closes that gap for
# every current AND future addon script, not just the two that broke.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

# print_schema() resolves lab_schema via shutil.which() (PATH lookup) — the
# real, installed automation VM always has it there (install_automation_node_
# scripts.sh puts every scripts/* on PATH), but this container only mounts
# the raw repo, so it isn't found unless added here too.
export PATH="$(pwd)/scripts:$PATH"

_pass=0
_fail=0

for f in scripts/install_*.py; do
    name=$(basename "$f")

    out=$(python3 "$f" --schema 2>&1)
    rc=$?
    first_line=$(printf '%s\n' "$out" | head -1)
    if [[ $rc -ne 0 || "$first_line" != "{" ]]; then
        _fail=$((_fail + 1))
        echo "FAIL: $name --schema did not print JSON (rc=$rc): $first_line"
        continue
    fi

    out=$(python3 "$f" --version 2>&1)
    rc=$?
    if [[ $rc -ne 0 || "$out" != "$name "* ]]; then
        _fail=$((_fail + 1))
        echo "FAIL: $name --version did not print '$name <version>' (rc=$rc): $out"
        continue
    fi

    _pass=$((_pass + 1))
done

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
