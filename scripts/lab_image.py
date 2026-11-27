#!/usr/bin/env python3.11
# Part of lab-in-a-box — reusable machine images: install something slow once,
# keep it as an image, and build every later VM of that product from it. The
# two routes and the naming live in libs/images.py; this is the command that
# drives them against one lab's own backend.
# Author/s: Raul Mahiques
# License: GPLv3
__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import backends  # noqa: E402
import images  # noqa: E402
import migrations  # noqa: E402
import primary  # noqa: E402
from lab_creation import die, log  # noqa: E402

ACTIONS = ("find", "capture", "import")

_HELP_TEXT = """\
Usage: lab_image.py <action> <lab.json> <vm_name> <product> [version] [source]

Reusable machine images. A product that takes hours to install is worth installing once and keeping: the next lab
builds its VM from the image instead of installing again. <vm_name> is a VM of <lab.json>, which says which
backend and which cloud account the image belongs to.

Actions:
  find     Print the id of the image of <product> this project already built there, or nothing. Exit 0 when one
           was found, 1 when none was.
  capture  Make an image of <vm_name> as it now is. Whatever makes it worth keeping must already be installed on
           it. This is the usual route and every backend can do it.
  import   Register a disk built outside the cloud as the image, from <source> in that provider's own terms
           (an s3://bucket/key on AWS, for example). For a product that cannot be installed onto a running OS,
           such as an appliance written to a disk by its own installer. Fewer backends can do this; one that
           cannot says so instead of appearing to work.

Both routes end in an image named lab-in-a-box-<product>-<version>, which a lab uses as any node's
SOURCE_IMAGE.

Options:
  --help, -h  Print this help and exit.
"""


def _backend(lab_path, vm_name, for_existing):
    """The backend `vm_name` of this lab definition belongs to, with its cloud account resolved."""
    definition = migrations.ensure_current(Path(lab_path))
    if vm_name not in (definition.get("nodes") or {}):
        die("'{}' is not a node of {} — its nodes are: {}".format(
            vm_name, lab_path, ", ".join(sorted(definition.get("nodes") or {})) or "none"))
    return backends.get_backend(definition, primary.load_config(), vm_name, for_existing=for_existing)


def main():
    if primary.wants_help(sys.argv[1:]):
        print(_HELP_TEXT, end="")
        sys.exit(0)
    if len(sys.argv) < 5 or sys.argv[1] not in ACTIONS:
        print(_HELP_TEXT, end="")
        sys.exit(1)

    action, lab_path, vm_name, product = sys.argv[1:5]
    version = sys.argv[5] if len(sys.argv) > 5 else ""

    if action == "find":
        backend = _backend(lab_path, vm_name, for_existing=True)
        if not backend.images_supported():
            die("the {} backend cannot keep images, so there is never one to find".format(
                type(backend).__name__))
        found = images.existing(backend, product, version)
        if not found:
            log("No {} image in this account yet; it has to be built".format(
                images.name(product, version)))
            sys.exit(1)
        print(found)
        return

    if action == "capture":
        backend = _backend(lab_path, vm_name, for_existing=True)
        print(images.capture(backend, vm_name, product, version))
        return

    if len(sys.argv) < 7:
        die("import needs a version and a source: lab_image.py import <lab.json> <vm_name> <product> "
            "<version> <source>")
    source = sys.argv[6]
    backend = _backend(lab_path, vm_name, for_existing=False)
    if not backend.image_import_supported():
        die("the {} backend cannot import a disk as an image: its provider has no such operation. Build this "
            "product on a backend that can, or install it onto a running VM and capture that "
            "instead".format(type(backend).__name__))
    name = images.name(product, version)
    log("- Importing {} into this account as '{}'".format(source, name))
    print(backend.import_image(name, source))


if __name__ == "__main__":
    main()
