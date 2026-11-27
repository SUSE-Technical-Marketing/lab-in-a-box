#!/usr/bin/env python3
"""
Which releases of a Kubernetes distribution a lab can install: the versions a dropdown offers and the one a
cluster naming no version is built with. Covers every distribution clu_type accepts, and takes one more by
adding an entry to SOURCES.

The list comes from the distribution's own git repository, read the way `git ls-remote --tags` reads it: git's
smart HTTP protocol, version 2, asking only for refs under refs/tags/v. No REST API, so no rate limit to run
into (GitHub allows an unauthenticated API caller 60 requests an hour per address, which a node serving a web UI
exhausts), no token, and no `git` binary needed on the node.

Nothing here constrains what a lab may name. A version typed by hand is accepted on its shape alone
(is_version), so a release newer than this node's cached list, or one a distribution publishes somewhere else
entirely, still installs. The list is a convenience, never an allowlist.

A channel is a filter over that list, not a separate source: "stable" lists the releases with no prerelease
marker, "testing" lists the ones with it, "all" lists both. A distribution's own installer may also accept a
channel name where this project passes clu_rel straight through (rke2 and k3s do), which is unaffected by any
of this.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import http.client
import json
import os
import re
import time
import urllib.request

TIMEOUT = 25

# How long a read list is used before the repository is asked again. One day: a distribution publishes a release
# every few weeks, so a day-old list is never meaningfully behind.
CACHE_TTL = 24 * 3600

# Named like git itself, because this speaks git's protocol rather than any REST API. It names this project,
# never whoever happens to be running it.
USER_AGENT = "git/2.0 (lab-in-a-box)"

# The channels a distribution's releases can be filtered by.
CHANNELS = ("all", "stable", "testing")

# Every distribution clu_type accepts, by its clu_type name.
#   repo     : the GitHub repository whose tags are its releases.
#   build    : the per-distribution build suffix in a tag ("v1.37.2+rke2r1"), or None for a plain tag.
#   channels : the base URL of the distribution's own channel service, which is the only authority on what a
#              channel name means, or None where it publishes none. rke2's "stable" is a whole minor behind its
#              newest tag, so resolving a channel from the tag list instead would silently install a different
#              release than the distribution itself calls stable.
#   fallback : what versions() answers on a node that has never managed to read the repository, newest first.
#              Harvester's list is complete from the v1.3 branch on, because without it there is no version to
#              install at all; the other three resolve a channel name through their own installer, so their
#              list only populates a dropdown and carries the newest release of the three newest branches.
SOURCES = {
    "harvester": {
        "repo": "harvester/harvester",
        "build": None,
        "channels": None,
        "fallback": (
            "v1.9.1", "v1.9.0",
            "v1.8.2", "v1.8.1", "v1.8.0",
            "v1.7.3", "v1.7.2", "v1.7.1", "v1.7.0",
            "v1.6.1", "v1.6.0",
            "v1.5.2", "v1.5.1", "v1.5.0",
            "v1.4.3", "v1.4.2", "v1.4.1", "v1.4.0",
            "v1.3.2", "v1.3.1", "v1.3.0",
        ),
    },
    "rke2": {
        "repo": "rancher/rke2",
        "build": "rke2r",
        "channels": "https://update.rke2.io/v1-release/channels",
        "fallback": ("v1.37.2+rke2r1", "v1.36.6+rke2r2", "v1.35.10+rke2r1"),
    },
    "k3s": {
        "repo": "k3s-io/k3s",
        "build": "k3s",
        "channels": "https://update.k3s.io/v1-release/channels",
        "fallback": ("v1.37.2+k3s1", "v1.36.6+k3s1", "v1.35.10+k3s1"),
    },
    "vanilla": {
        "repo": "kubernetes/kubernetes",
        "build": None,
        "channels": None,
        "fallback": ("v1.37.3", "v1.36.7", "v1.35.11"),
    },
}

_KEY_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?(?:\+[A-Za-z0-9]*?(\d+))?$")
_PRERELEASE_RE = re.compile(r"-(rc|alpha|beta|dev|pre)", re.IGNORECASE)
_TAG_REF_RE = re.compile(rb"refs/tags/(\S+)")
# What a channel name may look like: "stable", "latest", "testing", "v1.31", "v1.31-latest". A lab definition's
# clu_rel is free text, and only this shape is ever put in a URL.
_CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")


def source(distribution: str) -> dict:
    """SOURCES[`distribution`]. Raises KeyError for a distribution with no release list, which is not an error:
    a caller offers no choices for it."""
    return SOURCES[str(distribution or "").strip().lower()]


def version_re(distribution: str) -> str:
    """
    The shape of a release tag of `distribution`: "v1.9.1" for Harvester and upstream Kubernetes, "v1.37.2+rke2r1"
    for rke2, "v1.37.2+k3s1" for k3s.

    A prerelease marker is part of the shape ("v1.10.0-rc1"), because a distribution publishes its release
    candidates as installable releases and an operator may legitimately pin one. Which of them a caller offers is
    the channel's job, not the shape's: channel_of() sends them to "testing" and newest() defaults to "stable".
    """
    build = source(distribution).get("build")
    pre = r"(?:-[0-9A-Za-z.]+)?"
    tail = r"$" if not build else r"\+{}\d+$".format(build)
    return r"^v\d+\.\d+\.\d+" + pre + tail


def is_version(distribution: str, text: str) -> bool:
    """
    Whether `text` is a release of `distribution` by its shape. This is the whole test a hand-typed version has
    to pass, so a release no list here mentions is still installable.
    """
    try:
        return bool(re.match(version_re(distribution), str(text or "").strip()))
    except KeyError:
        return False


def version_key(text: str) -> tuple:
    """
    `text` as a comparable (major, minor, patch, released, prerelease number, build number), or (-1,) for
    anything that is not a release tag, so sorted() orders by number rather than by the order a repository
    happens to list its tags in: v1.7.1 is newer than v1.6.2 however they were published, v1.36.6+rke2r2 is
    newer than v1.36.6+rke2r1, v1.10.0-rc3 is newer than v1.10.0-rc1, and both are older than v1.10.0.
    """
    found = _KEY_RE.match(str(text or "").strip())
    if not found:
        return (-1,)
    major, minor, patch, pre, build = found.groups()
    # A prerelease sorts below the release it precedes, and prereleases of one release sort by their own number:
    # rc1 before rc3, alpha.3 before beta.1 only by number, which is as far as a dropdown needs to order them.
    digits = re.findall(r"\d+", pre or "")
    return (int(major), int(minor), int(patch), 0 if pre else 1,
            int(digits[-1]) if digits else 0, int(build or 0))


def channel_of(tag: str) -> str:
    """Which channel `tag` belongs to: "testing" when it carries a prerelease marker, else "stable"."""
    return "testing" if _PRERELEASE_RE.search(str(tag or "")) else "stable"


def tags(repo: str, timeout: int = TIMEOUT) -> list:
    """
    Every tag of `repo` beginning with "v", read over git's smart HTTP protocol version 2 (one POST of an
    ls-refs command, the same exchange `git ls-remote --tags` makes).

    Raises OSError (urllib's own) when the repository cannot be read, so a caller that must not fail uses
    versions() instead.
    """
    def pkt(payload):
        raw = payload.encode("utf-8")
        return "{:04x}".format(len(raw) + 4).encode("ascii") + raw

    body = (pkt("command=ls-refs\n") + pkt("agent={}\n".format(USER_AGENT))
            + b"0001" + pkt("ref-prefix refs/tags/v\n") + b"0000")
    request = urllib.request.Request(
        "https://github.com/{}.git/git-upload-pack".format(repo), data=body,
        headers={"User-Agent": USER_AGENT, "Git-Protocol": "version=2",
                 "Content-Type": "application/x-git-upload-pack-request",
                 "Accept": "application/x-git-upload-pack-result"})
    handle = urllib.request.urlopen(request, timeout=timeout)
    try:
        raw = handle.read()
    finally:
        handle.close()
    found = set()
    for match in _TAG_REF_RE.finditer(raw):
        name = match.group(1).decode("utf-8", "replace")
        # A peeled annotated tag is listed a second time as "<tag>^{}"; it names the same release.
        if not name.endswith("^{}"):
            found.add(name)
    return sorted(found)


def filter_channel(distribution: str, listed, channel: str = "all") -> list:
    """The release tags among `listed` that belong to `channel`, newest first. Raises ValueError for an unknown
    channel, so a typo narrows nothing silently."""
    channel = str(channel or "all").strip().lower()
    if channel not in CHANNELS:
        raise ValueError("unknown channel '{}', expected one of {}".format(channel, ", ".join(CHANNELS)))
    shape = re.compile(version_re(distribution))
    found = {t for t in (listed or []) if shape.match(t) and (channel == "all" or channel_of(t) == channel)}
    return sorted(found, key=version_key, reverse=True)


def newer(listed, floor: str) -> list:
    """
    The entries of `listed` newer than `floor`, newest first; all of them with no `floor`. This is what keeps a
    refresh to the one question worth asking: everything at or below the newest version already held is already
    held.
    """
    if not floor:
        return sorted(set(listed or []), key=version_key, reverse=True)
    bar = version_key(floor)
    return sorted({t for t in (listed or []) if version_key(t) > bar}, key=version_key, reverse=True)


def channel_url(distribution: str, channel: str) -> str:
    """
    Where `distribution` publishes what `channel` currently means, "" where it publishes nothing, and "" for a
    name that is not a channel name.

    A channel name comes from a lab definition, so it is free text. Only the shape a channel can actually have
    reaches a URL; anything else is not asked about at all, which is also what stops a value with a space or a
    control character in it from reaching the HTTP client.
    """
    base = source(distribution).get("channels")
    name = str(channel or "").strip()
    if not base or not _CHANNEL_NAME_RE.match(name):
        return ""
    return "{}/{}".format(base, name)


def _channel_tag(distribution: str, channel: str, timeout: int = TIMEOUT) -> str:
    """
    The release tag `channel` names right now, read from the distribution's own channel service, which answers by
    redirecting to that release. "" when it answers something that is not a release of this distribution.

    Raises OSError (urllib's own) or http.client.HTTPException when the service cannot be reached, which
    includes it answering 404 for a channel it does not publish.
    """
    url = channel_url(distribution, channel)
    if not url:
        return ""
    handle = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
    try:
        final = handle.geturl()
    finally:
        handle.close()
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    return tag if is_version(distribution, tag) else ""


def resolve(distribution: str, channel: str, timeout: int = TIMEOUT) -> str:
    """
    The release `channel` names for `distribution`, as a version tag, or "" when it cannot be resolved.

    A `channel` that is already a release tag is returned unchanged. Otherwise the distribution's own channel
    service decides, because it is the only authority on what its channel names mean. When that service cannot be
    reached, the answer is the last one it gave on this node, and failing that the newest release of the matching
    channel among the tags; a caller that gets "" passes the channel name on untouched, exactly as before.
    """
    named = str(channel or "").strip()
    if is_version(distribution, named):
        return named
    try:
        source(distribution)
    except KeyError:
        return ""

    if channel_url(distribution, named):
        try:
            tag = _channel_tag(distribution, named, timeout=timeout)
        except (OSError, ValueError, http.client.HTTPException):
            tag = ""
        if tag:
            _remember_channel(distribution, named, tag)
            return tag
        remembered = _remembered_channel(distribution, named)
        if remembered:
            return remembered

    # No channel service, or it has never answered on this node: the tags are all there is to go on. "latest"
    # means the newest release of any kind, which is this module's "all".
    fallback = {"stable": "stable", "testing": "testing", "latest": "all"}.get(named)
    if not fallback:
        return ""
    found = versions(distribution, channel=fallback)
    return found[0] if found else ""


def newest_per_branch(listed, limit: int = 0) -> list:
    """
    One release per <major>.<minor> branch, the newest of each, newest first, at most `limit` of them (0 for all
    of them).

    What a choice of version actually wants: a distribution publishes hundreds of patch releases, and upstream
    Kubernetes alone has over 580, which is a list nobody reads. The newest patch of each branch is the real
    decision. Anything left out is still installable by naming it, since a version is accepted on its shape.
    """
    out, seen = [], set()
    for tag in sorted(set(listed or []), key=version_key, reverse=True):
        branch = version_key(tag)[:2]
        if branch in seen or branch == (-1,)[:2]:
            continue
        seen.add(branch)
        out.append(tag)
        if limit and len(out) >= limit:
            break
    return out


def cache_file(distribution: str) -> str:
    """Where `distribution`'s read list is kept: under $LAB_RELEASES_CACHE_DIR, else lab_creation/releases in
    the user's cache directory."""
    directory = os.environ.get("LAB_RELEASES_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "lab_creation", "releases")
    return os.path.join(directory, "{}.json".format(str(distribution).strip().lower()))


def _cached(distribution: str) -> dict:
    """`distribution`'s whole cache file as a mapping, {} when there is nothing readable. A cache that cannot be
    read is not an error, it is an empty one."""
    try:
        with open(cache_file(distribution)) as handle:
            cached = json.load(handle)
        return cached if isinstance(cached, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _store(distribution: str, **fields) -> None:
    """Merge `fields` into `distribution`'s cache file, keeping what it already holds. A cache that cannot be
    written is not an error: the answer is still returned, the next caller just asks again."""
    path = cache_file(distribution)
    cached = _cached(distribution)
    cached.update(fields)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w") as handle:
            json.dump(cached, handle)
    except OSError:
        pass


def _read_cache(distribution: str) -> tuple:
    """(tags, read_at) from `distribution`'s cache file, or ([], 0) when there is nothing usable."""
    cached = _cached(distribution)
    if cached.get("tags"):
        try:
            return [str(t) for t in cached["tags"]], float(cached.get("read_at") or 0)
        except (TypeError, ValueError):
            pass
    return [], 0.0


def _write_cache(distribution: str, found) -> None:
    """Store `found` as `distribution`'s list, as of now."""
    _store(distribution, read_at=time.time(), tags=list(found))


def _remember_channel(distribution: str, channel: str, tag: str) -> None:
    """Keep what `distribution`'s channel service said `channel` means, as the answer to fall back on when it
    cannot be reached."""
    known = _cached(distribution).get("channels")
    known = dict(known) if isinstance(known, dict) else {}
    known[str(channel)] = str(tag)
    _store(distribution, channels=known)


def _remembered_channel(distribution: str, channel: str) -> str:
    """The last tag `distribution`'s channel service gave for `channel` on this node, "" when it never has."""
    known = _cached(distribution).get("channels")
    tag = (known or {}).get(str(channel)) if isinstance(known, dict) else ""
    return str(tag) if tag and is_version(distribution, tag) else ""


def cached(distribution: str, channel: str = "all") -> list:
    """
    The releases of `distribution` in `channel` from the cache alone, newest first, and the baked fallback list
    when nothing has been read on this node. Never touches the network.

    This is what a process that must not make an outbound request uses: the lab-builder's CGI runs as the web
    server's user and does no slow or privileged work, so the list it offers is whatever the root-side refresh
    (scripts/refresh_hypervisor_status.py) last put in the snapshot it reads.
    """
    try:
        known, _read_at = _read_cache(distribution)
        return filter_channel(distribution, known or source(distribution)["fallback"], channel)
    except (KeyError, ValueError):
        return []


def versions(distribution: str, channel: str = "all", ttl: int = CACHE_TTL, refresh: bool = False,
             timeout: int = TIMEOUT) -> list:
    """
    The releases of `distribution` in `channel`, newest first, read from the cache and topped up from the
    repository at most once every `ttl` seconds (`refresh=True` asks now regardless).

    A refresh adds the tags newer than the newest one cached and keeps the rest, so the list only ever grows and
    an older branch's releases stay offered. A failure to read leaves the cache as it is, stale timestamp
    included, so the next caller tries again rather than waiting out the TTL on a failed read; the answer is then
    the list last read, and the distribution's `fallback` only on a node that has never read one.
    """
    known, read_at = _read_cache(distribution)
    if known and not refresh and (time.time() - read_at) < ttl:
        return filter_channel(distribution, known, channel)

    try:
        found = tags(source(distribution)["repo"], timeout=timeout)
    except (OSError, ValueError, http.client.HTTPException):
        return filter_channel(distribution, known or source(distribution)["fallback"], channel)

    merged = sorted(set(known) | set(newer(found, _newest_of(distribution, known))))
    merged = merged or list(source(distribution)["fallback"])
    _write_cache(distribution, merged)
    return filter_channel(distribution, merged, channel)


def _newest_of(distribution: str, listed) -> str:
    """The newest release tag among `listed`, "" when there is none. The floor a refresh asks above."""
    found = filter_channel(distribution, listed, "all")
    return found[0] if found else ""


def newest(distribution: str, channel: str = "stable", ttl: int = CACHE_TTL) -> str:
    """
    The newest release of `distribution` in `channel`, which is what a cluster naming no version is built with.
    Cached like versions(). "" when the distribution has no release list at all.
    """
    try:
        found = versions(distribution, channel=channel, ttl=ttl)
    except KeyError:
        return ""
    if found:
        return found[0]
    fallback = filter_channel(distribution, source(distribution)["fallback"], channel)
    return fallback[0] if fallback else ""
