#!/usr/bin/env python3.11
# install_complianceascode — see 88_complianceascode.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
_loader = SourceFileLoader("install_complianceascode", str(_REPO / "scripts" / "install_complianceascode"))
ca = importlib.util.module_from_spec(importlib.util.spec_from_loader("install_complianceascode", _loader))
_loader.exec_module(ca)

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


check("in_range: a K3s version inside the range", ca.in_range("v1.36.5+k3s1", ">= 1.34.0-0 < 1.37.0-0"))
check("in_range: the upper bound is exclusive", not ca.in_range("v1.37.0", ">= 1.34.0-0 < 1.37.0-0"))
check("in_range: below the lower bound", not ca.in_range("v1.33.9+rke2r1", ">= 1.34.0-0 < 1.37.0-0"))

VERSIONS = {
    "rancher-compliance": [("110.2.0+up1.5.3", ">= 1.34.0-0 < 1.37.0-0"), ("109.6.0+up1.4.8", ">= 1.33.0-0 < 1.36.0-0")],
    "rancher-cis-benchmark": [("106.8.0+up8.10.0", ">= 1.30.0-0 < 1.33.0-0")],
}
check("pick_chart: the newest rancher-compliance supporting the cluster",
      ca.pick_chart(VERSIONS, "v1.36.5+k3s1") == ("rancher-compliance", "compliance-operator-system",
                                                   "compliance.cattle.io/v1", "110.2.0+up1.5.3"))
check("pick_chart: an older rancher-compliance for an older cluster",
      ca.pick_chart(VERSIONS, "v1.33.4+rke2r1")[3] == "109.6.0+up1.4.8")
check("pick_chart: rancher-cis-benchmark when no rancher-compliance fits",
      ca.pick_chart(VERSIONS, "v1.31.2+k3s1") == ("rancher-cis-benchmark", "cis-operator-system", "cis.cattle.io/v1",
                                                   "106.8.0+up8.10.0"))
check("pick_chart: an explicit version wins over the range", ca.pick_chart(VERSIONS, "v1.36.5", "109.6.0+up1.4.8")[3]
      == "109.6.0+up1.4.8")
check("pick_chart: None when nothing supports the cluster", ca.pick_chart(VERSIONS, "v1.28.0") is None)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all complianceascode checks passed")
