#!/usr/bin/env python3
"""
Reusable machine images: install something slow once, then build every later VM from the result.

A product that takes hours to install, SUSE Multi-Linux Manager being the clear case at over a day, is worth
installing once and keeping. There are two routes to an image, and which one applies is a property of the
product, not a preference:

  capture  The default. The VM is created from an ordinary OS image in the cloud, the slow install runs once,
           and the cloud is then asked to make an image of that instance's own disk. Nothing is uploaded,
           nothing is converted, and every backend this project supports can do it.
  import   For a product that cannot be installed onto a running OS, an appliance written to a disk from its own
           installer being the case, Harvester being the example. The disk is built outside the cloud and then
           imported. Fewer backends can do this, so a backend declares it separately (image_import_supported()).

Both routes end in the same place: an image this project owns, named by name(), that create_vm() takes as its
SOURCE_IMAGE like any other. Nothing here builds or installs anything; a caller does that and then captures the
result, so this module is only the naming, the lookup and the capture.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import re

from lab_creation import die, log

# Every image this project creates is named with this prefix, so one is recognisable as ours in a cloud account
# an operator also uses for other things, and so a later lab finds it instead of building it again.
PREFIX = "lab-in-a-box"

# What a provider accepts in an image name, across all of them: lower case letters, digits and dashes. A dot is
# not safe everywhere, so a version's dots become dashes.
_UNSAFE = re.compile(r"[^a-z0-9-]+")


def name(product: str, version: str = "") -> str:
    """
    The name an image of `product` at `version` is stored under, e.g. name("smlm", "5.2") ->
    "lab-in-a-box-smlm-5-2".

    Deterministic on purpose: it is the whole mechanism by which a second lab finds the image a first lab built
    rather than spending the install time again. A version is part of it, so a new release gets its own image
    instead of silently reusing the old one.
    """
    parts = [PREFIX, str(product or "").strip().lower(), str(version or "").strip().lower()]
    joined = "-".join(p for p in parts if p)
    return _UNSAFE.sub("-", joined).strip("-")


def product_of(definition: dict, vm_name: str) -> tuple:
    """
    (product, version) for a node that keeps a reusable image, read from its keep_image and
    keep_image_version; ("", "") for a node that keeps none.

    A node's own value wins over common's, as everywhere else in a lab definition, so one lab can keep an image
    of one node and not of another. The version follows the product it belongs to: a node that names its own
    product does not inherit common's version, which would otherwise name the image after one product at another
    product's version.
    """
    node = (definition.get("nodes") or {}).get(vm_name) or {}
    common = definition.get("common") or {}
    own = str(node.get("keep_image") or "").strip()
    product = own or str(common.get("keep_image") or "").strip()
    if not product:
        return "", ""
    source = node if own else common
    return product, str(source.get("keep_image_version") or "").strip()


def existing(backend, product: str, version: str = "") -> str:
    """
    The id of an image of `product` this project already built in this backend, or "" when there is none and one
    has to be built. "" as well for a backend that cannot keep images at all, so a caller falls back to
    installing every time rather than failing.
    """
    if not backend.images_supported():
        return ""
    found = backend.find_image(name(product, version))
    if found:
        log("- Reusing the {} image already in this account: {}".format(product, found))
    return found


def capture(backend, vm_name: str, product: str, version: str = "") -> str:
    """
    Make an image of `vm_name` as it now is and return its id, so later VMs of `product` start from it.

    The VM has to be finished: whatever makes the image worth keeping must already be installed on it. Dies on a
    backend that cannot keep images, because a caller asking for this has nowhere to put the result.
    """
    if not backend.images_supported():
        die("this backend cannot keep an image of a VM, so there is nothing to capture '{}' into".format(vm_name))
    image = name(product, version)
    log("- Capturing '{}' as the reusable {} image '{}'".format(vm_name, product, image))
    return backend.image_from_vm(vm_name, image)
