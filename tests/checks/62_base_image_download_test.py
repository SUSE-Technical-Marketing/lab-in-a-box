#!/usr/bin/env python3
# Base image download (a URL in SOURCE_IMAGE, or ISO_URL) — see 62_base_image_download.sh.
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402
import lab_creation as lc  # noqa: E402
import primary  # noqa: E402
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
      any("ISO_URL" in i for i in lc.image_source_issues("n", "img", "nfs://x", GOOD, "")))
check("ftp is a download scheme", lc.image_source_issues("n", "img", "ftp://x/img", GOOD, "") == [])
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

# ── SOURCE_IMAGE as a URL: the same download as ISO_URL ────────────────────
check("the image file name is the URL's last segment",
      lc.image_file_name("https://x/a/leap.qcow2?raw=1 https://y/other.qcow2") == "leap.qcow2")
check("a node's image name is the URL's file name",
      lc.image_name({"SOURCE_IMAGE": "https://x/leap.qcow2"}) == "leap.qcow2"
      and lc.image_name({"SOURCE_IMAGE": "", "ISO_IMAGE": "old.qcow2"}) == "old.qcow2")
check("a hex checksum is the sha256", lc.source_checksum(GOOD.upper()) == (GOOD, ""))
check("a checksum URL publishes the sha256", lc.source_checksum("https://x/SUMS") == ("", "https://x/SUMS"))
with tempfile.NamedTemporaryFile("w", suffix=".sha256", delete=False) as fh:
    fh.write(GOOD.upper() + "  leap.qcow2\n")
check("a local checksum file is read here", lc.source_checksum(fh.name) == (GOOD, ""))
Path(fh.name).unlink()
check("SOURCE_IMAGE URL with SOURCE_SHA256",
      lc.image_source({"SOURCE_IMAGE": "https://x/leap.qcow2", "SOURCE_SHA256": "https://x/leap.qcow2.sha256"})
      == ("leap.qcow2", "https://x/leap.qcow2", "", "https://x/leap.qcow2.sha256"))
check("ISO_URL downloads SOURCE_IMAGE",
      lc.image_source({"SOURCE_IMAGE": "sp7.qcow2", "ISO_URL": "https://x/sp7", "ISO_SHA256": GOOD})
      == ("sp7.qcow2", "https://x/sp7", GOOD, ""))
check("a local SOURCE_IMAGE downloads nothing", lc.image_source({"SOURCE_IMAGE": "sp7.qcow2"}) is None)
check("a URL without a checksum is an issue",
      lc.image_source_issues("n", *lc.image_source({"SOURCE_IMAGE": "https://x/leap.qcow2"})) != [])
mixed = {
    "common": {"SOURCE_IMAGE": "https://x/leap.qcow2", "SOURCE_SHA256": GOOD},
    "nodes": {
        "a.lab": {},                                               # common URL
        "b.lab": {"SOURCE_IMAGE": "own.qcow2"},                    # own local image
        "c.lab": {"SOURCE_IMAGE": "https://y/c.qcow2 https://z/c.qcow2",
                  "SOURCE_SHA256": "https://y/SUMS"},              # own URL, two mirrors
        "d.lab": {"ISO_URL": "https://x/d", "ISO_SHA256": GOOD},  # own ISO_URL for the common image
    },
}
reqs = {r[0]: r for r in lc.image_download_requests(mixed)}
check("common SOURCE_IMAGE URL applies to nodes without their own image",
      reqs.get("a.lab") == ("a.lab", "leap.qcow2", "https://x/leap.qcow2", GOOD, ""))
check("a node with its own local SOURCE_IMAGE downloads nothing", "b.lab" not in reqs)
check("a node's own SOURCE_IMAGE URL, mirrors kept",
      reqs.get("c.lab") == ("c.lab", "c.qcow2", "https://y/c.qcow2 https://z/c.qcow2", "", "https://y/SUMS"))
check("a node's own ISO_URL downloads the common image name",
      reqs.get("d.lab") == ("d.lab", "leap.qcow2", "https://x/d", GOOD, ""))


def _preflight(definition):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(definition, fh)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        lc.validate_lab_definition(primary.load_definition(fh.name), {}, "/nonexistent", "/nonexistent",
                                   host_checks=False)
    Path(fh.name).unlink()
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())


sizing = {"VM_MEM": "2048", "VM_DSK": "20", "VM_CPU": "1"}
report = _preflight({"common": dict(sizing, SOURCE_IMAGE="https://x/leap.qcow2"), "nodes": {"n.lab": {"myip": "10.0.0.5"}}})
check("the preflight refuses a SOURCE_IMAGE URL without SOURCE_SHA256",
      "common: a downloaded image needs SOURCE_SHA256" in report)
report = _preflight({"common": dict(sizing, SOURCE_IMAGE="https://x/leap.qcow2", SOURCE_SHA256=GOOD),
                     "nodes": {"n.lab": {"myip": "10.0.0.5"}}})
check("the preflight accepts a SOURCE_IMAGE URL with its sha256", "downloaded image" not in report
      and "download URL" not in report)

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
          lc.image_source_issues("n", "i", "https://a/i nfs://b/i", GOOD, "") != [])
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

# ── the libvirt backend boots the verified copy of a SOURCE_IMAGE URL ──────
fetched, ran = [], []
real_fetch = lc.fetch_source_image
lc.fetch_source_image = lambda host, iso_loc, image, url, sha, sha_url: fetched.append(
    (host, iso_loc, image, url, sha, sha_url)) or "cached"
backends.ssh_run = lambda host, cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
backend = backends.LibvirtBackend("qemu+ssh://root@kvm1/system", remote_host="kvm1", iso_loc="/srv/sources",
                                  vm_img_loc="/var/lib/libvirt/images")
backend.copy_vm_image("https://x/leap.qcow2", "n.lab", "20", source_sha256="https://x/leap.qcow2.sha256")
check("the backend fetches the URL into ISO_LOC on its KVM host",
      fetched == [("kvm1", "/srv/sources", "leap.qcow2", "https://x/leap.qcow2", "", "https://x/leap.qcow2.sha256")])
check("...and copies that verified file", ran and ran[0] == "cp /srv/sources/leap.qcow2 /var/lib/libvirt/images/n.lab.qcow2")
lc.fetch_source_image = real_fetch

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
