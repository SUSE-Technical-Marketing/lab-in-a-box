#!/bin/bash
# Tests-paths: scripts/build_man_pages.py install_automation_node_scripts.sh
# scripts/build_man_pages.py: the pages it generates for every installed
# command, that each one renders without a groff warning, and that its command
# list still matches what install_automation_node_scripts.sh installs.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_work="$(mktemp -d)"
trap 'rm -rf "${_work}"' EXIT

_fail=0
python3 tests/checks/127_man_pages_test.py "${_work}" || _fail=1

# Every generated page must render with no groff warning: a page man cannot
# format is worse than no page. Skipped where groff is not in the image.
if command -v groff >/dev/null 2>&1; then
    _pages=0
    while IFS= read -r _page; do
        _pages=$((_pages + 1))
        _out="$(groff -man -z "${_page}" 2>&1)"
        if [[ -n "${_out}" ]]; then
            echo "FAIL: groff warns on ${_page##*/}:"
            echo "${_out}" | head -5
            _fail=1
        fi
    done < <(find "${_work}" -type f -name '*.[18]')
    if (( _pages == 0 )); then
        echo "FAIL: no pages were generated to render"
        _fail=1
    else
        echo "${_pages} page(s) render with no groff warning"
    fi
else
    echo "groff not installed in this image — roff rendering not checked"
fi

exit ${_fail}
