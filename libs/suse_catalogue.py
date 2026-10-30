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

_LEAP_VERSION = re.compile(r"^opensuse-leap-(\d+)-(\d+)-", re.IGNORECASE)
_SLES_VERSION = re.compile(r"^suse-sles-(\d+)-(?:sp)?(\d+)-", re.IGNORECASE)
_MICRO_VERSION = re.compile(r"^suse-sle-micro-(\d+)-(\d+)-", re.IGNORECASE)

# Variants that are not a plain pay-as-you-go SLES: BYOS wants a registration code before zypper
# works, chost is the container host, ecs is built for another cloud, and sapcal, hardened and
# tomcat are their own products.
_SLES_VARIANTS = ("byos", "chost", "sapcal", "hardened", "sap", "ecs", "tomcat")

# SL Micro ships only a plain and a BYOS variant; the BYOS one is excluded for the same reason.
_MICRO_VARIANTS = ("byos",)


def active_images(provider: str, region: str = "") -> list:
    """The active images SUSE lists for `provider` ("amazon", "google", ...), limited to `region` when given."""
    path = "{}/{}/{}images/active.json".format(API_URL, provider, region + "/" if region else "")
    with urllib.request.urlopen(path, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8")).get("images", [])


def arch_matches(name: str, arch_suffix: str) -> bool:
    """
    Whether image `name` is for the architecture `arch_suffix` ("-x86_64", "-arm64").

    The name either ends in the suffix, as the SLES and Leap images do, or carries it as its second-to-last segment
    followed by a billing-entity suffix, as the SL Micro images do ("...-hvm-ssd-x86_64-ltd").
    """
    if name.endswith(arch_suffix):
        return True
    head, _, tail = name.rpartition("-")
    return bool(tail) and head.endswith(arch_suffix)


def _newest(images: list, arch_suffix: str, version_re, variants: tuple,
            major: int = 0, minor: int = -1) -> dict:
    """
    The newest image of `images` whose name `version_re` matches with its major and minor version as the two groups,
    for `arch_suffix`, excluding every name carrying one of `variants`: the highest version, then the latest publish
    date, then the name, so a choice between two images differing only in a trailing suffix is stable. `major` and
    `minor` limit the result to one version; 0 and -1 take the highest there is. {} when there is none.
    """
    found = []
    for image in images:
        name = image.get("name", "")
        match = version_re.match(name)
        if not match or not arch_matches(name, arch_suffix):
            continue
        if any("-{}-".format(variant) in name or name.endswith("-" + variant) for variant in variants):
            continue
        version = (int(match.group(1)), int(match.group(2)))
        if major and version[0] != major:
            continue
        if minor >= 0 and version[1] != minor:
            continue
        found.append((version, image.get("publishedon", ""), name, image))
    return max(found, key=lambda entry: entry[:3])[3] if found else {}


def newest_leap(images: list, arch_suffix: str, major: int = 0, minor: int = -1) -> dict:
    """
    The newest openSUSE Leap image of `images` for `arch_suffix`. `major` and `minor` limit it to one version
    (16 and 0 for Leap 16.0); 0 and -1 take the highest there is.
    """
    return _newest(images, arch_suffix, _LEAP_VERSION, (), major=major, minor=minor)


def newest_sles(images: list, arch_suffix: str, major: int = 0, minor: int = -1) -> dict:
    """
    The newest plain SLES image of `images` for `arch_suffix`. `major` limits it to one major version (16 for
    SLES 16.x) and `minor` to one service pack or minor release (7 for SLES 15 SP7); 0 and -1 take the highest there
    is. A SLES 15 name spells its minor version "sp7" and a SLES 16 one "0", and both are read as the minor version
    here.

    Only the pay-as-you-go images are considered. A BYOS image needs a registration code before its zypper can
    install anything, and chost, ecs, sapcal, hardened and tomcat are different products, so none of them is what a
    lab that just wants "a SLES node" should be handed.
    """
    return _newest(images, arch_suffix, _SLES_VERSION, _SLES_VARIANTS, major=major, minor=minor)


def newest_micro(images: list, arch_suffix: str, major: int = 0, minor: int = -1) -> dict:
    """
    The newest pay-as-you-go SUSE Linux Micro (SLE Micro) image of `images` for `arch_suffix`. `major` and `minor`
    limit it to one version (6 and 1 for SL Micro 6.1); 0 and -1 take the highest there is.

    The BYOS images are excluded: their zypper needs a registration code before it can install anything, which is the
    same reason newest_sles() excludes them.
    """
    return _newest(images, arch_suffix, _MICRO_VERSION, _MICRO_VARIANTS, major=major, minor=minor)
