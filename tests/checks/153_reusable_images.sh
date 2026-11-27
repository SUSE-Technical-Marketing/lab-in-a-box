#!/bin/bash
# Reusable machine images: that a product installed once can be kept as an image and found again by a later lab,
# that a backend which cannot keep images degrades to installing every time rather than failing, and that the
# two routes to an image (capture a running VM, import a disk built outside) are declared per backend rather
# than assumed. No provider is contacted.
# Tests-paths: libs/images.py libs/backends.py libs/lab_schema_base.py scripts/setup_vm.py scripts/setup_lab.py scripts/lab_image.py
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/153_reusable_images_test.py
