#!/usr/bin/env python3.11
# Which releases of a Kubernetes distribution a lab can install — see 152_releases.sh. No network: every answer
# from a repository here is a stub, and a test that really reached github.com would fail on an offline node.
import json
import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import releases as rl  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def pkt(payload):
    raw = payload.encode("utf-8")
    return "{:04x}".format(len(raw) + 4).encode("ascii") + raw


class _Answer(object):
    """What urlopen() returns for an ls-refs POST: git's own pkt-line ref advertisement."""

    def __init__(self, tags):
        body = b""
        for i, tag in enumerate(tags):
            body += pkt("{:040x} refs/tags/{}\n".format(i + 1, tag))
        self.body = body + b"0000"

    def read(self):
        return self.body

    def close(self):
        pass


def api(*answers):
    """A urlopen stub answering each call in turn, so a test can assert how many times it was asked."""
    return mock.Mock(side_effect=[a if isinstance(a, Exception) else _Answer(a) for a in answers])


def cached(distribution):
    with open(rl.cache_file(distribution)) as handle:
        return json.load(handle)


def write_cache(distribution, tags, age=0):
    path = rl.cache_file(distribution)
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w") as handle:
        json.dump({"read_at": time.time() - age, "tags": tags}, handle)


# ── every distribution clu_type accepts has a release list ────────────────────
check("the four distributions are covered", sorted(rl.SOURCES) == ["harvester", "k3s", "rke2", "vanilla"])
check("each one names the repository its releases are tagged in",
      all(rl.source(d)["repo"].count("/") == 1 for d in rl.SOURCES))
check("a distribution with no list is a KeyError, not a wrong answer",
      not rl.is_version("openshift", "v1.0.0") and rl.newest("openshift") == "")

# ── a version is a shape, per distribution ────────────────────────────────────
check("a Harvester release is recognised", rl.is_version("harvester", "v1.9.1"))
check("rke2 carries its build suffix", rl.is_version("rke2", "v1.37.2+rke2r1")
      and not rl.is_version("rke2", "v1.37.2"))
check("k3s carries its own", rl.is_version("k3s", "v1.37.2+k3s1") and not rl.is_version("k3s", "v1.37.2+rke2r1"))
check("upstream Kubernetes carries none", rl.is_version("vanilla", "v1.37.3")
      and not rl.is_version("vanilla", "v1.37.3+k3s1"))
check("a tag typed without the v is not a version", not rl.is_version("harvester", "1.9.1"))
check("a channel name is not a version", not rl.is_version("harvester", "stable"))
check("a release this project has never listed is still a version", rl.is_version("harvester", "v9.42.0"))
check("a prerelease is a version, because a distribution publishes it as one",
      rl.is_version("harvester", "v1.10.0-rc3") and rl.is_version("k3s", "v1.37.3-rc1+k3s1"))

# ── ordering is by number, across every shape ─────────────────────────────────
check("releases sort by number, not by how a repository lists them",
      sorted(["v1.6.2", "v1.10.0", "v1.7.1"], key=rl.version_key, reverse=True) == ["v1.10.0", "v1.7.1", "v1.6.2"])
check("a later build of one release is newer",
      rl.version_key("v1.36.6+rke2r2") > rl.version_key("v1.36.6+rke2r1"))
check("a prerelease is older than the release it precedes",
      rl.version_key("v1.10.0-rc3") < rl.version_key("v1.10.0"))
check("prereleases of one release sort by their own number",
      rl.version_key("v1.10.0-rc3") > rl.version_key("v1.10.0-rc1"))
check("anything that is not a release tag sorts below everything", rl.version_key("latest") == (-1,))

# ── a channel is a filter over the list, not another source ───────────────────
check("the channels are all, stable and testing", rl.CHANNELS == ("all", "stable", "testing"))
check("a prerelease belongs to testing", rl.channel_of("v1.10.0-rc1") == "testing")
check("a release belongs to stable", rl.channel_of("v1.9.1") == "stable")
listed = ["v1.9.1", "v1.9.0", "v1.10.0-rc1", "v1.10.0-rc2", "not-a-tag", "v1.37.2+k3s1"]
check("stable lists only the releases",
      rl.filter_channel("harvester", listed, "stable") == ["v1.9.1", "v1.9.0"])
check("testing lists only the prereleases",
      rl.filter_channel("harvester", listed, "testing") == ["v1.10.0-rc2", "v1.10.0-rc1"])
check("all lists both, newest first",
      rl.filter_channel("harvester", listed, "all")
      == ["v1.10.0-rc2", "v1.10.0-rc1", "v1.9.1", "v1.9.0"])
check("another distribution's tag is not this one's release",
      "v1.37.2+k3s1" not in rl.filter_channel("harvester", listed, "all"))
try:
    rl.filter_channel("harvester", listed, "lastest")
    check("a misspelled channel is refused rather than narrowing silently", False)
except ValueError as exc:
    check("a misspelled channel is refused rather than narrowing silently", "lastest" in str(exc))

# ── only the releases above what is already held are of interest ──────────────
check("a refresh asks only about newer tags", rl.newer(["v1.10.0", "v1.9.1"], "v1.9.1") == ["v1.10.0"])
check("the newest held release is not new", rl.newer(["v1.9.1"], "v1.9.1") == [])
check("with nothing held, every tag is new", rl.newer(["v1.9.1", "v1.9.0"], "") == ["v1.9.1", "v1.9.0"])

workdir = tempfile.mkdtemp(prefix="releases-")
os.environ["LAB_RELEASES_CACHE_DIR"] = os.path.join(workdir, "cache")

# ── the repository is read once a day and the answer kept ─────────────────────
with mock.patch.object(rl.urllib.request, "urlopen", api(["v1.9.1", "v1.9.0"])) as opened:
    first = rl.versions("harvester")
check("a first read answers what the repository tags", first == ["v1.9.1", "v1.9.0"])
asked = opened.call_args[0][0]
check("it asked over git's protocol, not an API",
      "api.github.com" not in asked.full_url and asked.full_url.endswith("/git-upload-pack"))
check("it asked the distribution's own repository",
      "/harvester/harvester.git/" in asked.full_url)
check("the request asks only for tags, not every ref", b"ref-prefix refs/tags/v" in asked.data)
check("it announces git protocol version 2", asked.headers.get("Git-protocol") == "version=2")
check("and stores what it read, with the time", cached("harvester")["tags"] and cached("harvester")["read_at"] > 0)
check("each distribution has its own cache file",
      rl.cache_file("harvester") != rl.cache_file("rke2"))

with mock.patch.object(rl.urllib.request, "urlopen", api()) as opened:
    again = rl.versions("harvester")
check("a second read inside the day asks nothing", again == first and opened.call_count == 0)
check("the day is the default", rl.CACHE_TTL == 24 * 3600)

with mock.patch.object(rl.urllib.request, "urlopen", api(["v1.10.0", "v1.9.1", "v1.9.0"])) as opened:
    forced = rl.versions("harvester", refresh=True)
check("asking now overrides the cache", forced[0] == "v1.10.0" and opened.call_count == 1)

# ── a day-old cache is topped up, not replaced ────────────────────────────────
write_cache("harvester", ["v1.9.1", "v1.9.0", "v1.8.2"], age=rl.CACHE_TTL + 60)
with mock.patch.object(rl.urllib.request, "urlopen", api(["v1.10.0", "v1.9.1"])) as opened:
    topped = rl.versions("harvester")
check("a stale cache is topped up with what is newer", topped[0] == "v1.10.0")
check("and keeps releases the repository's answer no longer carried", "v1.8.2" in topped)
check("one request tops it up", opened.call_count == 1)

write_cache("harvester", ["v1.9.1"], age=rl.CACHE_TTL + 60)
with mock.patch.object(rl.urllib.request, "urlopen", api(["v1.9.1"])):
    unchanged = rl.versions("harvester")
check("a refresh that finds nothing new changes nothing", unchanged == ["v1.9.1"])
check("but counts as a read, so the next caller waits the full day",
      (time.time() - cached("harvester")["read_at"]) < 60)

# ── a repository that cannot be read is not an error ──────────────────────────
refusal = urllib.error.HTTPError("https://github.com", 503, "unavailable", {}, None)
write_cache("harvester", ["v1.9.1", "v1.9.0"], age=rl.CACHE_TTL + 60)
with mock.patch.object(rl.urllib.request, "urlopen", api(refusal)):
    refused = rl.versions("harvester")
check("a failed refresh answers the list last read", refused == ["v1.9.1", "v1.9.0"])
check("and does not pretend it read one, so the next caller tries again",
      (time.time() - cached("harvester")["read_at"]) > rl.CACHE_TTL)

os.unlink(rl.cache_file("harvester"))
with mock.patch.object(rl.urllib.request, "urlopen", api(refusal)):
    nothing_ever = rl.versions("harvester")
check("a node that has never read the repository falls back to the baked list",
      nothing_ever == list(rl.source("harvester")["fallback"]))
with mock.patch.object(rl.urllib.request, "urlopen", api(OSError("no route to host"))):
    check("an offline node answers too", rl.versions("harvester") == list(rl.source("harvester")["fallback"]))

# ── the baked lists ───────────────────────────────────────────────────────────
for distribution in sorted(rl.SOURCES):
    fallback = rl.source(distribution)["fallback"]
    check("every baked {} version is a usable tag".format(distribution),
          all(rl.is_version(distribution, v) for v in fallback))
    check("the baked {} list is newest first".format(distribution),
          list(fallback) == sorted(fallback, key=rl.version_key, reverse=True))
    check("the baked {} list names no version twice".format(distribution),
          len(set(fallback)) == len(fallback))
    check("every baked {} version is a stable one".format(distribution),
          all(rl.channel_of(v) == "stable" for v in fallback))
check("Harvester's baked list reaches back to the v1.3 branch, since without it nothing installs",
      "v1.3.0" in rl.source("harvester")["fallback"] and len(rl.source("harvester")["fallback"]) >= 20)
for distribution in ("rke2", "k3s", "vanilla"):
    fallback = rl.source(distribution)["fallback"]
    check("{} carries one release per branch, since its installer resolves a channel itself".format(distribution),
          len(fallback) == 3 and len({rl.version_key(v)[:2] for v in fallback}) == 3)

# ── the default version of a cluster that names none ──────────────────────────
write_cache("harvester", ["v1.10.0-rc3", "v1.9.1", "v1.9.0"])
check("the default is the newest stable release, not a release candidate", rl.newest("harvester") == "v1.9.1")
check("a caller can ask for the newest prerelease on purpose",
      rl.newest("harvester", "testing") == "v1.10.0-rc3")

# ── a channel name is resolved by the distribution's own service ──────────────
# rke2's own "stable" is a whole minor behind its newest tag, so resolving a channel from the tag list instead
# would silently install a different release than the distribution calls stable. The service decides.
class _Redirected(object):
    """What urlopen() returns for a channel lookup: the service redirects to the release it names."""

    def __init__(self, final):
        self.final = final

    def geturl(self):
        return self.final

    def close(self):
        pass


def service(*finals):
    return mock.Mock(side_effect=[f if isinstance(f, Exception) else _Redirected(f) for f in finals])


check("a distribution with a channel service has its URL",
      rl.channel_url("rke2", "stable") == "https://update.rke2.io/v1-release/channels/stable")
check("one without a channel service has none", rl.channel_url("harvester", "stable") == "")

with mock.patch.object(rl.urllib.request, "urlopen",
                       service("https://github.com/rancher/rke2/releases/tag/v1.36.6+rke2r1")) as opened:
    check("a channel resolves to the release the service names", rl.resolve("rke2", "stable") == "v1.36.6+rke2r1")
    check("it asked that service, not the tag list",
          "update.rke2.io" in opened.call_args[0][0].full_url)
check("and the answer is remembered",
      json.load(open(rl.cache_file("rke2")))["channels"]["stable"] == "v1.36.6+rke2r1")

with mock.patch.object(rl.urllib.request, "urlopen", service(OSError("service down"))) as opened:
    check("a service that is down answers with what it last said", rl.resolve("rke2", "stable") == "v1.36.6+rke2r1")

write_cache("rke2", ["v1.37.2+rke2r1", "v1.36.6+rke2r1"])
with mock.patch.object(rl.urllib.request, "urlopen", service(OSError("service down"))):
    check("a service that has never answered falls back to the newest release tagged",
          rl.resolve("rke2", "stable") == "v1.37.2+rke2r1")

with mock.patch.object(rl.urllib.request, "urlopen", service()) as opened:
    check("a version is already an answer and asks nothing",
          rl.resolve("rke2", "v1.31.5+rke2r1") == "v1.31.5+rke2r1" and opened.call_count == 0)
    check("a distribution with no channel service and no list entry answers nothing",
          rl.resolve("openshift", "stable") == "")

# clu_rel is free text, so a channel name that is not one must never reach the HTTP client: it raises its own
# exception types, and an unresolvable channel has to leave the install exactly as it was.
with mock.patch.object(rl.urllib.request, "urlopen", service()) as opened:
    for hostile in ("stable; rm -rf /", "sta ble", "../../etc", "", "a" * 60):
        check("{!r} is not asked about at all".format(hostile), rl.channel_url("rke2", hostile) == "")
    check("and resolving one answers nothing, so the caller passes it on untouched",
          rl.resolve("rke2", "stable; rm -rf /") == "" and opened.call_count == 0)

with mock.patch.object(rl.urllib.request, "urlopen",
                       service("https://github.com/rancher/rke2/releases/tag/nonsense")):
    check("a service answering something that is not a release of this distribution is not trusted",
          rl.resolve("rke2", "testing") in ("v1.37.2+rke2r1", ""))

# ── a cache that cannot be written is not an error ────────────────────────────
os.environ["LAB_RELEASES_CACHE_DIR"] = "/proc/version/nope"
with mock.patch.object(rl.urllib.request, "urlopen", api(["v1.9.1"])):
    check("an unwritable cache still answers", rl.versions("harvester") == ["v1.9.1"])
os.environ["LAB_RELEASES_CACHE_DIR"] = os.path.join(workdir, "cache")

# ── Harvester's own asset addressing, and the version a kcluster installs ─────
sys.path.insert(0, str(_REPO / "scripts"))
import harvester  # noqa: E402

check("an asset URL names the version twice, as the publisher does",
      harvester.asset_url("v1.9.1", "amd64.iso")
      == "https://releases.rancher.com/harvester/v1.9.1/harvester-v1.9.1-amd64.iso")
check("every file an install needs is addressed", len(harvester.ASSETS) == 4 and "amd64.iso" in harvester.ASSETS)
check("no asset URL points at GitHub, which publishes none of them",
      not any("github" in harvester.asset_url("v1.9.1", s) for s in harvester.ASSETS))
# clu_rel carries the Harvester version, the same field every other distribution takes its version from.
# Validation checks only what a definition names, and by shape alone: it runs in the public Validate endpoint,
# which must make no outbound request of its own.
def harvester_problems(clu_rel):
    definition = {"nodes": {"h1.example.lab": {"kcluster": "h", "SOURCE_IMAGE": "ami-1",
                                               "aws_nested_virtualization": "true"}},
                  "kclusters": {"h": {"clu_type": "harvester", "mydomain": "example.lab",
                                      "harvester_token": "t", "clu_rel": clu_rel}}}
    return harvester.problems(definition, lambda vm: "aws")


check("a release tag in clu_rel is accepted", not any("clu_rel" in p for p in harvester_problems("v1.8.2")))
check("a version newer than any list is accepted too",
      not any("clu_rel" in p for p in harvester_problems("v9.42.0")))
check("naming no version is accepted, since it means the newest stable release",
      not any("clu_rel" in p for p in harvester_problems("")))
check("a channel name in clu_rel is refused, naming the value",
      any("clu_rel 'stable'" in p for p in harvester_problems("stable")))
check("so is an rke2-shaped version", any("clu_rel" in p for p in harvester_problems("v1.37.2+rke2r1")))

with mock.patch.object(rl.urllib.request, "urlopen", api()) as opened:
    harvester_problems("")
    check("validating a lab makes no outbound request", opened.call_count == 0)

# ── the web UI reads the list and never asks for it ───────────────────────────
# The CGI runs as the web server's user and does no slow or privileged work, so the list it offers comes out of
# the snapshot the root-side refresh writes. A request from that process would be one per page load.
write_cache("harvester", ["v1.10.0-rc1", "v1.9.1", "v1.9.0"])
with mock.patch.object(rl.urllib.request, "urlopen", api()) as opened:
    offered = rl.cached("harvester", "stable")
    check("the cached list is answered without asking anyone", offered == ["v1.9.1", "v1.9.0"]
          and opened.call_count == 0)
    check("a channel still filters it", rl.cached("harvester", "testing") == ["v1.10.0-rc1"])

os.unlink(rl.cache_file("harvester"))
with mock.patch.object(rl.urllib.request, "urlopen", api()) as opened:
    check("a node that has read nothing yet answers the baked list, still without asking",
          rl.cached("harvester", "stable") == list(rl.source("harvester")["fallback"])
          and opened.call_count == 0)
    check("a distribution with no list at all answers nothing rather than failing",
          rl.cached("openshift") == [])

# ── a dropdown offers one release per branch, not every patch ─────────────────
every = ["v1.37.3", "v1.37.2", "v1.37.1", "v1.36.7", "v1.36.6", "v1.35.11", "v1.3.0"]
check("one release per branch, the newest of each",
      rl.newest_per_branch(every) == ["v1.37.3", "v1.36.7", "v1.35.11", "v1.3.0"])
check("and at most as many as asked for", rl.newest_per_branch(every, limit=2) == ["v1.37.3", "v1.36.7"])
check("a limit of zero means all of them", len(rl.newest_per_branch(every, limit=0)) == 4)
check("anything that is not a release tag is left out", rl.newest_per_branch(["latest", "v1.9.1"]) == ["v1.9.1"])
check("upstream Kubernetes' 580-odd patch releases become a list someone can read",
      len(rl.newest_per_branch(rl.cached("vanilla", "stable"), limit=8)) <= 8)

# ── the library states its own contract ───────────────────────────────────────
source_text = (_REPO / "libs" / "releases.py").read_text()
check("it says why it does not use the REST API", "60 requests an hour" in source_text)
check("it says a hand-typed version is accepted", "typed by hand" in source_text)
check("it says a channel is a filter", "A channel is a filter" in source_text)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all release-list checks passed")
