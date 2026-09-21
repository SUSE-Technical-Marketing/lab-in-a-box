#!/usr/bin/env python3
# Unit tests for libs/backends.py's get_backend()/resolve() dispatch — the
# refactor that moved host/cluster resolution out of get_backend() itself
# and into each backend's own resolve() classmethod. resolve_kvm_host()/
# locate_kvm_host() are mocked (no real virsh); HARVESTER_KUBECONFIG comes
# from a plain dict, no real kubectl. Run from 26_backends_resolve.sh, in
# its own container — see tests/run_tests.sh.
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402
import primary  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


definition = {"nodes": {"vm1": {"myip": "192.168.1.50"}}, "common": {}}

# ── libvirt (default): behavior-identical to the pre-refactor inline body ───
with mock.patch.object(backends, "resolve_kvm_host", return_value=("hv1", "qemu+ssh://root@hv1/system")) as m:
    backend = backends.get_backend(definition, {}, "vm1", for_existing=False, vm_img_loc="/var/lib/libvirt/images")
    check("get_backend() with no backend configured defaults to LibvirtBackend",
          isinstance(backend, backends.LibvirtBackend))
    check("get_backend() (for_existing=False) calls resolve_kvm_host(), not locate_kvm_host()",
          m.call_count == 1)
    check("LibvirtBackend.resolve() sets virt_srv/remote_host from resolve_kvm_host()'s return",
          backend.virt_srv == "qemu+ssh://root@hv1/system" and backend.remote_host == "hv1")

with mock.patch.object(backends, "locate_kvm_host", return_value=("hv1", "qemu+ssh://root@hv1/system")) as m:
    backend = backends.get_backend(definition, {}, "vm1", for_existing=True)
    check("get_backend() (for_existing=True) calls locate_kvm_host(), not resolve_kvm_host()",
          m.call_count == 1)
    check("libvirt resolve() for an existing VM still returns a usable LibvirtBackend",
          isinstance(backend, backends.LibvirtBackend) and backend.virt_srv == "qemu+ssh://root@hv1/system")

# ── explicit per-node / common backend selection ─────────────────────────────
per_node_def = {"nodes": {"vm1": {"backend": "libvirt"}}, "common": {"backend": "harvester"}}
with mock.patch.object(backends, "resolve_kvm_host", return_value=("hv1", "qemu+ssh://root@hv1/system")):
    backend = backends.get_backend(per_node_def, {}, "vm1", for_existing=False)
    check("a per-node backend override wins over common.backend",
          isinstance(backend, backends.LibvirtBackend))

common_def = {"nodes": {"vm1": {}}, "common": {"backend": "harvester"}}
backend = backends.get_backend(common_def, {"HARVESTER_KUBECONFIG": "/etc/lab-harvester/kubeconfig"},
                                "vm1", for_existing=False)
check("common.backend selects HarvesterBackend when no per-node override is set",
      isinstance(backend, backends.HarvesterBackend))

# ── unknown backend name dies, listing the known backends ────────────────────
bad_def = {"nodes": {"vm1": {"backend": "not-a-backend"}}, "common": {}}
died = False
try:
    backends.get_backend(bad_def, {}, "vm1", for_existing=False)
except SystemExit:
    died = True
check("an unknown backend name dies rather than silently defaulting", died)


# ── HarvesterBackend.resolve(): needs HARVESTER_KUBECONFIG, defaults namespace ─
harv_def = {"nodes": {"vm1": {"backend": "harvester"}}, "common": {}}
died = False
try:
    backends.get_backend(harv_def, {}, "vm1", for_existing=False)  # no HARVESTER_KUBECONFIG
except SystemExit:
    died = True
check("HarvesterBackend.resolve() dies clearly when HARVESTER_KUBECONFIG is not configured", died)

backend = backends.get_backend(harv_def, {"HARVESTER_KUBECONFIG": "/etc/lab-harvester/kubeconfig"},
                                "vm1", for_existing=False)
check("HarvesterBackend.resolve() picks up HARVESTER_KUBECONFIG from config",
      backend.kubeconfig == "/etc/lab-harvester/kubeconfig")
check("HarvesterBackend.resolve() defaults namespace to 'default' when HARVESTER_NAMESPACE is unset",
      backend.namespace == "default")

backend = backends.get_backend(
    harv_def, {"HARVESTER_KUBECONFIG": "/etc/lab-harvester/kubeconfig", "HARVESTER_NAMESPACE": "my-lab"},
    "vm1", for_existing=False)
check("HarvesterBackend.resolve() uses HARVESTER_NAMESPACE when set", backend.namespace == "my-lab")


# ── _check_or_generate_mac(): reads the lab definition exactly once ─────────
# Regression coverage for two real bugs, fixed in sequence:
#   1. This used to re-read/re-parse the lab file from disk on a MAC
#      conflict, discarding the caller's own already-loaded `definition`
#      entirely, then write the patched copy back as JSON unconditionally
#      (breaking YAML lab files).
#   2. The first fix threaded an input_file/fmt pair through this function
#      as separate arguments to know where/how to write — but this function
#      has no business knowing either; that's not its job. Now `definition`
#      is a primary.LabDefinition (see primary.py), which already knows its
#      own source_path/fmt, so this function takes ONLY `definition` for
#      that purpose and calls primary.save_definition(definition) — no
#      input_file, no fmt, no re-read, ever.
# Proven decisively below by making the ON-DISK file's content genuinely
# diverge from `definition`: if the fix ever regressed back to re-reading
# the file, these assertions would see the stale disk content instead of
# the in-memory definition's own fields. Also proven that a resolved
# conflict never overwrites the original file: it lands in a sibling
# "<path>.system_modified.json" instead.

def _lab_def(data, path):
    """A LabDefinition backed by a real temp file at `path`, for tests that
    need .source_path to resolve to something real (the conflict-resolution
    path writes to "<source_path>.system_modified.json")."""
    Path(path).write_text(json.dumps(data))
    return primary.LabDefinition(data, path, "json")


def _tmp_path(suffix):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.close()
    return f.name


# No conflict: mymac empty -> generated. No file I/O at all — the definition
# below points at a path that's never even created, proving nothing was
# read from (or written to) it.
with mock.patch.object(backends._lc, "_generate_unused_mac", return_value="52:54:00:aa:bb:01"):
    definition = primary.LabDefinition({"nodes": {"vm1": {}}}, "/nonexistent/never-touched.json", "json")
    mymac, network = backends._check_or_generate_mac({}, "vm1", "", definition, "br0", "virtio")
    check("_check_or_generate_mac: empty mymac generates one, no file access needed",
          mymac == "52:54:00:aa:bb:01" and "mac.address=52:54:00:aa:bb:01" in network)

# No conflict: mymac set, not in use by anyone else -> used as-is.
definition = primary.LabDefinition({"nodes": {"vm1": {}}}, "/nonexistent/never-touched.json", "json")
mymac, network = backends._check_or_generate_mac(
    {"vm2": "aa:bb:cc:dd:ee:02"}, "vm1", "aa:bb:cc:dd:ee:01", definition, "br0", "virtio")
check("_check_or_generate_mac: available mymac is used as-is", mymac == "aa:bb:cc:dd:ee:01")

# No conflict: mymac already belongs to THIS SAME vm_name (re-run/--keep case).
definition = primary.LabDefinition({"nodes": {"vm1": {}}}, "/nonexistent/never-touched.json", "json")
mymac, network = backends._check_or_generate_mac(
    {"vm1": "aa:bb:cc:dd:ee:01"}, "vm1", "aa:bb:cc:dd:ee:01", definition, "br0", "virtio")
check("_check_or_generate_mac: MAC already owned by this same VM is not a conflict",
      mymac == "aa:bb:cc:dd:ee:01")

# Conflict, user confirms ('y'): definition is mutated in place AND saved —
# using the in-memory definition, never re-reading the disk file, which is
# deliberately left holding DIFFERENT content throughout. The ORIGINAL file
# must be left untouched; the update lands in a sibling ".system_modified.json".
path = _tmp_path(".json")
definition = _lab_def(
    {"nodes": {"vm1": {"mymac": "aa:bb:cc:dd:ee:01"}}, "_marker": "in-memory-definition"}, path)
Path(path).write_text(json.dumps({"nodes": {"vm1": {"mymac": "STALE-ON-DISK-VALUE"}},
                                   "_marker": "this-is-stale-disk-content"}))
original_on_disk = Path(path).read_text()
with mock.patch.object(backends, "_read_conflict_confirmation", return_value="y"), \
     mock.patch.object(backends._lc, "_generate_unused_mac", return_value="52:54:00:aa:bb:02"):
    mymac, network = backends._check_or_generate_mac(
        {"vm2": "aa:bb:cc:dd:ee:01"}, "vm1", "aa:bb:cc:dd:ee:01", definition, "br0", "virtio")
check("_check_or_generate_mac: conflict + 'y' regenerates the MAC", mymac == "52:54:00:aa:bb:02")
check("_check_or_generate_mac: the in-memory definition dict is mutated directly",
      definition["nodes"]["vm1"]["mymac"] == "52:54:00:aa:bb:02")
check("_check_or_generate_mac: the ORIGINAL file is left completely untouched",
      Path(path).read_text() == original_on_disk)
saved_path = path + ".system_modified.json"
check("_check_or_generate_mac: writes the update to '<path>.system_modified.json' instead",
      Path(saved_path).is_file())
saved = json.loads(Path(saved_path).read_text())
check("_check_or_generate_mac: saves the IN-MEMORY definition (its own '_marker'), "
      "not a re-read of the stale on-disk copy",
      saved.get("_marker") == "in-memory-definition")
check("_check_or_generate_mac: the saved file reflects the new MAC",
      saved["nodes"]["vm1"]["mymac"] == "52:54:00:aa:bb:02")

# Conflict, user declines (anything but 'y'/'Y') -> dies, nothing written.
path = _tmp_path(".json")
definition = _lab_def({"nodes": {"vm1": {"mymac": "aa:bb:cc:dd:ee:01"}}}, path)
original_content = Path(path).read_text()
died = False
with mock.patch.object(backends, "_read_conflict_confirmation", return_value="n"):
    try:
        backends._check_or_generate_mac(
            {"vm2": "aa:bb:cc:dd:ee:01"}, "vm1", "aa:bb:cc:dd:ee:01", definition, "br0", "virtio")
    except SystemExit:
        died = True
check("_check_or_generate_mac: conflict + decline dies", died)
check("_check_or_generate_mac: declining a conflict never touches the original file",
      Path(path).read_text() == original_content)
check("_check_or_generate_mac: declining a conflict never writes a .system_modified copy either",
      not Path(path + ".system_modified.json").exists())


# ── LibvirtBackend.check_or_generate_mac: real concurrency stress test for
# the 2026-09-21 _mac_lock ────────────────────────────────────────────────
# setup_lab.py's new --parallel VM-creation mode means multiple real
# threads can now call check_or_generate_mac() concurrently for different
# VMs. The real hazard: list_used_macs() (a live query) and
# _check_or_generate_mac()'s own generate-a-new-one decision is a genuine
# TOCTOU window if not held together as one critical section — and a real
# conflict-resolution path mutates the SHARED `definition` object in
# place. This directly verifies mutual exclusion (not an indirect side
# effect): list_used_macs() is monkeypatched to sleep briefly and record
# its own [enter, exit] interval; if _mac_lock genuinely serializes the
# whole call, no two threads' intervals can ever overlap.
import threading  # noqa: E402
import time as _mac_time  # noqa: E402

intervals = []
intervals_lock = threading.Lock()


def _slow_list_used_macs(self):
    start = _mac_time.monotonic()
    _mac_time.sleep(0.01)
    end = _mac_time.monotonic()
    with intervals_lock:
        intervals.append((start, end))
    return [], {}


mac_backend = backends.LibvirtBackend("qemu:///system")
with mock.patch.object(backends.LibvirtBackend, "list_used_macs", _slow_list_used_macs), \
     mock.patch.object(backends._lc, "_generate_unused_mac", return_value="52:54:00:aa:bb:99"):
    defs = [primary.LabDefinition({"nodes": {"vm{}".format(i): {}}},
                                   "/nonexistent/never-touched-{}.json".format(i), "json")
            for i in range(10)]
    mac_threads = [
        threading.Thread(target=mac_backend.check_or_generate_mac, args=("vm{}".format(i), "", defs[i]))
        for i in range(10)
    ]
    for t in mac_threads:
        t.start()
    for t in mac_threads:
        t.join()

check("check_or_generate_mac: list_used_macs() was actually called by every thread",
      len(intervals) == 10)
overlapping = any(
    a_start < b_end and b_start < a_end
    for i, (a_start, a_end) in enumerate(intervals)
    for j, (b_start, b_end) in enumerate(intervals)
    if i != j
)
check("check_or_generate_mac: _mac_lock genuinely serializes concurrent calls — "
      "no two threads' critical sections ever overlap in time",
      not overlapping)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all backends_resolve checks passed")
