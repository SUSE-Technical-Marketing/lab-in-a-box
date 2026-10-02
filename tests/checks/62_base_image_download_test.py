#!/usr/bin/env python3
# Base image download (ISO_URL) — see 62_base_image_download.sh.
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_creation as lc  # noqa: E402
import setup_lab  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Died(Exception):
    pass


def _die(msg):
    raise Died(msg)


lc.die = _die
GOOD = "a" * 64

# ── image_source_issues ────────────────────────────────────────────────────
check("valid url + sha256 has no issues",
      lc.image_source_issues("n", "img.qcow2", "https://x/img.qcow2", GOOD, "") == [])
check("valid url + sha256_url has no issues",
      lc.image_source_issues("n", "img.qcow2", "file:///srv/img.qcow2", "", "https://x/SUMS") == [])
check("missing checksum is an issue",
      any("ISO_SHA256" in i for i in lc.image_source_issues("n", "img", "https://x", "", "")))
check("bad scheme is an issue",
      any("ISO_URL" in i for i in lc.image_source_issues("n", "img", "ftp://x", GOOD, "")))
check("image with a path is an issue",
      any("plain file name" in i for i in lc.image_source_issues("n", "../img", "https://x", GOOD, "")))
check("malformed sha256 is an issue",
      any("64-character" in i for i in lc.image_source_issues("n", "img", "https://x", "abc", "")))

# ── image_download_requests: which URL belongs to which image ──────────────
definition = {
    "common": {"ISO_IMAGE": "base.qcow2", "ISO_URL": "https://x/base.qcow2", "ISO_SHA256": GOOD},
    "nodes": {
        "a.lab": {},                                               # common image + common URL
        "b.lab": {"ISO_IMAGE": "own.qcow2"},                       # own image, no URL
        "c.lab": {"ISO_IMAGE": "c.img", "ISO_URL": "https://x/c.img",
                  "ISO_SHA256_URL": "https://x/SUMS"},             # own image + own URL
    },
}
reqs = {r[0]: r for r in lc.image_download_requests(definition)}
check("common URL applies to nodes using common.ISO_IMAGE",
      reqs.get("a.lab") == ("a.lab", "base.qcow2", "https://x/base.qcow2", GOOD, ""))
check("a node with its own ISO_IMAGE and no ISO_URL downloads nothing", "b.lab" not in reqs)
check("node-level URL applies to that node's image",
      reqs.get("c.lab") == ("c.lab", "c.img", "https://x/c.img", "", "https://x/SUMS"))

cloud = {"common": {"ISO_IMAGE": "ami-1", "ISO_URL": "https://x/i", "ISO_SHA256": GOOD},
         "nodes": {"a.lab": {"cloud_account": "aws-lab"}, "b.lab": {"backend": "hetzner"},
                   "c.lab": {"backend": "libvirt"}}}
check("cloud/Harvester nodes download nothing",
      [r[0] for r in lc.image_download_requests(cloud)] == ["c.lab"])

# ── image_source_hosts ─────────────────────────────────────────────────────
lc._configured_hosts = lambda config: (["kvm1", "kvm2"], "kvm1")
check("all configured hosts are candidates", lc.image_source_hosts(definition, "a.lab", {}) == ["kvm1", "kvm2"])
pinned = {"nodes": {"p.lab": {"kvm_host": "kvm9"}}}
check("an explicit kvm_host is the only candidate", lc.image_source_hosts(pinned, "p.lab", {}) == ["kvm9"])


# ── fetch_source_image: run the real script locally ───────────────────────
def local_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    assert cmd == "bash -s"
    return subprocess.run(["bash", "-s"], input=input_text, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)


lc.ssh_run = local_ssh_run
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    src = tmp / "src.qcow2"
    src.write_bytes(b"qcow2 bytes")
    sha = hashlib.sha256(b"qcow2 bytes").hexdigest()
    iso_loc = tmp / "sources"

    state = lc.fetch_source_image("kvm1", str(iso_loc), "img.qcow2", "file://{}".format(src), sha)
    check("first fetch downloads", state == "downloaded")
    check("image lands in ISO_LOC", (iso_loc / "img.qcow2").read_bytes() == b"qcow2 bytes")
    check("no .part left behind", not (iso_loc / "img.qcow2.part").exists())
    check("second fetch is cached",
          lc.fetch_source_image("kvm1", str(iso_loc), "img.qcow2", "file://{}".format(src), sha) == "cached")

    mirrors = "file://{}/missing.qcow2 file://{}".format(tmp, src)
    check("second mirror used when the first fails",
          lc.fetch_source_image("kvm1", str(iso_loc), "m.qcow2", mirrors, sha) == "downloaded"
          and (iso_loc / "m.qcow2").read_bytes() == b"qcow2 bytes")
    try:
        lc.fetch_source_image("kvm1", str(iso_loc), "none.qcow2",
                              "file://{}/a file://{}/b".format(tmp, tmp), sha)
        check("all mirrors failing dies", False)
    except Died as e:
        check("all-mirrors-failed message names the image", "none.qcow2" in str(e))
    check("multiple URLs validate",
          lc.image_source_issues("n", "i", "https://a/i file:///b/i", GOOD, "") == [])
    check("one bad URL among several is an issue",
          lc.image_source_issues("n", "i", "https://a/i ftp://b/i", GOOD, "") != [])
    try:
        lc.fetch_source_image("kvm1", str(iso_loc), "bad.qcow2", "file://{}".format(src), "b" * 64)
        check("checksum mismatch dies", False)
    except Died as e:
        check("mismatch message names the image", "bad.qcow2" in str(e))
    check("mismatching download moved aside, never left as the image",
          not (iso_loc / "bad.qcow2").exists() and (iso_loc / "bad.qcow2.corrupt").exists())

    try:
        lc.fetch_source_image("kvm1", str(iso_loc), "x.qcow2", "file://{}".format(src), "")
        check("fetch without any checksum dies before running anything", False)
    except Died:
        pass

    # Quoting: a URL with shell metacharacters must not be executed.
    evil = tmp / "pwned"
    try:
        lc.fetch_source_image("kvm1", str(iso_loc), "q.qcow2",
                              "file:///nonexistent;touch {}".format(evil), sha)
    except Died:
        pass
    check("URL is shell-quoted in the remote script", not evil.exists())

# ── setup_lab.phase_fetch_images: one fetch per (host, image) ──────────────
calls = []
lc.fetch_source_image = lambda host, iso_loc, image, url, sha, sha_url: calls.append((host, image)) or "cached"
two_nodes = {"common": {"ISO_IMAGE": "base.qcow2", "ISO_URL": "https://x/b", "ISO_SHA256": GOOD},
             "nodes": {"n1.lab": {}, "n2.lab": {}}}
setup_lab.phase_fetch_images(two_nodes, {}, "/srv/sources")
check("each image fetched once per candidate host",
      sorted(calls) == [("kvm1", "base.qcow2"), ("kvm2", "base.qcow2")])
calls.clear()
setup_lab.phase_fetch_images({"common": {}, "nodes": {"n.lab": {}}}, {}, "/srv/sources")
check("nothing fetched without ISO_URL", calls == [])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all base image download checks passed")
