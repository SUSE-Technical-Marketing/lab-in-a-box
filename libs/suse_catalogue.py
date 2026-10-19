#!/usr/bin/env python3
"""
SUSE Public Cloud Information Tracker (https://susepubliccloudinfo.suse.com): the images SUSE publishes on each public
cloud, read over its public JSON API. Used to pick a provider's openSUSE Leap image by the image ID SUSE publishes, never
by a name search that could match an image someone else published under the same name.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import re
import urllib.request

API_URL = "https://susepubliccloudinfo.suse.com/v1"
TIMEOUT = 30

_LEAP_VERSION = re.compile(r"leap-(\d+)-(\d+)-", re.IGNORECASE)
_SLES_VERSION = re.compile(r"^suse-sles-(\d+)-(\d+)-", re.IGNORECASE)

# Variants that are not a plain pay-as-you-go SLES: BYOS wants a registration code before zypper
# works, chost is the container host, and sapcal and hardened are their own products.
_SLES_VARIANTS = ("byos", "chost", "sapcal", "hardened", "sap")


def active_images(provider: str, region: str = "") -> list:
    """The active images SUSE lists for `provider` ("amazon", "google", ...), limited to `region` when given."""
    path = "{}/{}/{}images/active.json".format(API_URL, provider, region + "/" if region else "")
    with urllib.request.urlopen(path, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8")).get("images", [])


def newest_leap(images: list, arch_suffix: str) -> dict:
    """The image of `images` with the highest openSUSE Leap version, then the latest publish date, whose name ends in
    `arch_suffix`; {} when there is none."""
    leap = []
    for image in images:
        name = image.get("name", "")
        match = _LEAP_VERSION.search(name)
        if match and name.lower().startswith("opensuse-leap-") and name.endswith(arch_suffix):
            leap.append(((int(match.group(1)), int(match.group(2))), image.get("publishedon", ""), image))
    return max(leap, key=lambda entry: entry[:2])[2] if leap else {}


def newest_sles(images: list, arch_suffix: str, major: int = 0) -> dict:
    """
    The newest plain SLES image of `images` whose name ends in `arch_suffix`: the highest version, then the latest
    publish date. `major` limits it to one major version (16 for SLES 16.x); 0 takes the highest there is.

    Only the pay-as-you-go images are considered. A BYOS image needs a registration code before its zypper can
    install anything, and chost, sapcal and hardened are different products, so none of them is what a lab that
    just wants "a SLES node" should be handed.
    """
    found = []
    for image in images:
        name = image.get("name", "")
        match = _SLES_VERSION.match(name)
        if not match or not name.endswith(arch_suffix):
            continue
        if any("-{}-".format(variant) in name or name.endswith("-" + variant) for variant in _SLES_VARIANTS):
            continue
        version = (int(match.group(1)), int(match.group(2)))
        if major and version[0] != major:
            continue
        found.append((version, image.get("publishedon", ""), image))
    return max(found, key=lambda entry: entry[:2])[2] if found else {}
