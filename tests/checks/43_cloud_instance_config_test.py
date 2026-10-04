#!/usr/bin/env python3
# Unit tests for libs/backends.py's _parse_sku_table() (the shared config-string parser behind
# every cloud backend's *_INSTANCE_TYPES/*_SERVER_TYPES/*_PLANS override) and a real regression
# check that LibvirtBackend.create_vm() accepts the new cloud_instance_type kwarg without
# crashing. No cloud provider's sizing catalog is a hardcoded ceiling. Per-backend override behavior (the config key actually replacing the
# table, cloud_instance_type actually bypassing auto-pick) is covered in each backend's own
# 3X_*_backend_test.py instead of duplicated here. Run from
# 43_cloud_instance_config.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── _parse_sku_table(): unset/empty input ──────────────────────────────────
check("_parse_sku_table() returns None for an unset (None) value — caller keeps its own default",
      backends._parse_sku_table(None, "TEST_KEY") is None)
check("_parse_sku_table() returns None for an empty string too",
      backends._parse_sku_table("", "TEST_KEY") is None)

# ── _parse_sku_table(): a real, valid multi-entry table ────────────────────
result = backends._parse_sku_table("t3.medium:2:4,t3.large:2:8,t3.xlarge:4:16", "TEST_KEY")
check("_parse_sku_table() parses a real multi-entry table into the expected tuples",
      result == [("t3.medium", 2, 4.0), ("t3.large", 2, 8.0), ("t3.xlarge", 4, 16.0)])

# ── _parse_sku_table(): whitespace around entries/commas is tolerated ──────
result = backends._parse_sku_table(" t3.medium:2:4 , t3.large:2:8 ", "TEST_KEY")
check("_parse_sku_table() tolerates whitespace around entries",
      result == [("t3.medium", 2, 4.0), ("t3.large", 2, 8.0)])

# ── _parse_sku_table(): a fractional mem_gb is accepted (e.g. Scaleway's DEV1-S:2:2 is fine,
#    but some providers' real plans have non-integer GB values) ────────────
result = backends._parse_sku_table("small:1:0.5", "TEST_KEY")
check("_parse_sku_table() accepts a fractional mem_gb value", result == [("small", 1, 0.5)])

# ── _parse_sku_table(): malformed entries die with a clear, specific message ──
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends._parse_sku_table("t3.medium:2", "TEST_KEY")  # missing the third field
    except SystemExit:
        pass
check("_parse_sku_table() dies clearly on an entry missing a field, naming the real config key",
      any("TEST_KEY" in m and "t3.medium:2" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends._parse_sku_table("t3.medium:two:4", "TEST_KEY")  # non-numeric cores
    except SystemExit:
        pass
check("_parse_sku_table() dies clearly on a non-numeric cores field",
      any("TEST_KEY" in m and "non-numeric" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends._parse_sku_table("   ,   ", "TEST_KEY")  # only blank/whitespace entries
    except SystemExit:
        pass
check("_parse_sku_table() dies clearly when every entry is blank (no valid entries at all)",
      any("TEST_KEY" in m and "no valid entries" in m for m in died))


# ── Real regression check: LibvirtBackend.create_vm() must accept cloud_instance_type ──
# setup_vm.py now unconditionally passes cloud_instance_type=... to every backend's create_vm(),
# including libvirt (the default backend, absorbed and ignored).
# LibvirtBackend.create_vm() must accept **kwargs, or every libvirt-backed VM would raise
# "unexpected keyword argument".
import inspect  # noqa: E402
sig = inspect.signature(backends.LibvirtBackend.create_vm)
check("LibvirtBackend.create_vm() accepts cloud_instance_type as a real named parameter "
      "(not just via a missing **kwargs catch-all)",
      "cloud_instance_type" in sig.parameters)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud_instance_config checks passed")
