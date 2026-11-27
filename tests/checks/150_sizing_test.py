#!/usr/bin/env python3
# Unit tests for libs/sizing.py. The figures themselves are a record of what each product's documentation says,
# so they are asserted here one by one: that is what makes a silent edit to the table visible, and every entry
# carries the URL to check it against. Run from 150_sizing.sh, in its own container — see tests/run_tests.sh.
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import sizing  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── every entry is complete, and says where it came from ──────────────────
KEYS = ("title", "cpu", "memory_mb", "disk_gb", "rec_cpu", "rec_memory_mb", "rec_disk_gb",
        "extra_disk_gb", "nodes", "bare_metal", "source", "note")
for name, entry in sorted(sizing.PRODUCTS.items()):
    check("{}: has every key, so no caller has to guess a shape".format(name), set(entry) == set(KEYS))
    check("{}: states a minimum CPU and memory, which every one of these documents".format(name),
          entry["cpu"] > 0 and entry["memory_mb"] > 0)
    check("{}: names the page its figures came from".format(name),
          re.match(r"^https://", entry["source"] or ""))
    check("{}: carries a note, since a bare number is not checkable".format(name), len(entry["note"]) > 40)
    check("{}: needs at least one node".format(name), entry["nodes"] >= 1)
    check("{}: a recommended figure is never below the minimum it recommends over".format(name),
          (entry["rec_cpu"] or entry["cpu"]) >= entry["cpu"]
          and (entry["rec_memory_mb"] or entry["memory_mb"]) >= entry["memory_mb"]
          and (entry["rec_disk_gb"] or entry["disk_gb"]) >= entry["disk_gb"])

check("the products this project's own workflows stand on are all here",
      set(sizing.PRODUCTS) >= {"rke2", "k3s", "k3s-agent", "rancher", "harvester", "smlm", "uyuni", "longhorn"})


# ── the figures, as each product's documentation states them ──────────────
check("RKE2: 2 CPU and 4 GB minimum, 4 and 8 recommended",
      (sizing.PRODUCTS["rke2"]["cpu"], sizing.PRODUCTS["rke2"]["memory_mb"],
       sizing.PRODUCTS["rke2"]["rec_cpu"], sizing.PRODUCTS["rke2"]["rec_memory_mb"]) == (2, 4096, 4, 8192))
check("K3s server: 2 cores and 2 GB, which is the smallest cluster this project can build",
      (sizing.PRODUCTS["k3s"]["cpu"], sizing.PRODUCTS["k3s"]["memory_mb"]) == (2, 2048))
check("K3s agent: 1 core and 512 MB, the only role either distribution documents separately",
      (sizing.PRODUCTS["k3s-agent"]["cpu"], sizing.PRODUCTS["k3s-agent"]["memory_mb"]) == (1, 512))
check("Rancher: 4 vCPU and 16 GB per node of the cluster it runs on, three nodes in production",
      (sizing.PRODUCTS["rancher"]["cpu"], sizing.PRODUCTS["rancher"]["memory_mb"],
       sizing.PRODUCTS["rancher"]["nodes"]) == (4, 16384, 3))
check("Harvester: 8 cores, 32 GB and 250 GB for testing; 16, 64 GB and 500 GB in production",
      (sizing.PRODUCTS["harvester"]["cpu"], sizing.PRODUCTS["harvester"]["memory_mb"],
       sizing.PRODUCTS["harvester"]["disk_gb"], sizing.PRODUCTS["harvester"]["rec_cpu"],
       sizing.PRODUCTS["harvester"]["rec_memory_mb"], sizing.PRODUCTS["harvester"]["rec_disk_gb"])
      == (8, 32768, 250, 16, 65536, 500))
check("Harvester is the one product here its own documentation keeps off a virtual machine",
      [n for n, e in sizing.PRODUCTS.items() if e["bare_metal"]] == ["harvester"])
check("Uyuni and SUSE Multi-Linux Manager: 4 cores, 16 GB, 32 recommended, 40 GB root and 200 GB of volumes",
      all((sizing.PRODUCTS[n]["cpu"], sizing.PRODUCTS[n]["memory_mb"], sizing.PRODUCTS[n]["rec_memory_mb"],
           sizing.PRODUCTS[n]["disk_gb"], sizing.PRODUCTS[n]["extra_disk_gb"]) == (4, 16384, 32768, 40, 200)
          for n in ("uyuni", "smlm")))
check("the Multi-Linux Manager entry says its figures came from Uyuni, which is what makes them checkable",
      "Uyuni" in sizing.PRODUCTS["smlm"]["note"])
check("Longhorn: 4 vCPU, 4 GiB, three nodes and a disk of its own",
      (sizing.PRODUCTS["longhorn"]["cpu"], sizing.PRODUCTS["longhorn"]["memory_mb"],
       sizing.PRODUCTS["longhorn"]["nodes"]) == (4, 4096, 3)
      and sizing.PRODUCTS["longhorn"]["extra_disk_gb"] > 0)


# ── known() ──────────────────────────────────────────────────────────────
check("a product in the table is known", sizing.known("rancher"))
check("an add-on with no documented requirement is not, which is most of them",
      not sizing.known("nextcloud") and not sizing.known("") and not sizing.known(None))


# ── combined(): the rule the module documents ────────────────────────────
one = sizing.combined(["rke2"])
check("one product is its own figures, with this project's floor for the disk nothing documents",
      one == {"cpu": 2, "memory_mb": 4096, "disk_gb": sizing.BASE_DISK_GB})

several = sizing.combined(["rke2", "rancher"])
check("CPU and memory take the largest, never the sum: each figure already covers the OS beneath it",
      several["cpu"] == 4 and several["memory_mb"] == 16384)

with_storage = sizing.combined(["rke2", "rancher", "longhorn"])
check("a product that wants storage of its own adds it, because nothing else may use that space",
      with_storage["disk_gb"] == sizing.BASE_DISK_GB + sizing.PRODUCTS["longhorn"]["extra_disk_gb"])
check("...and the rest is unchanged by it",
      (with_storage["cpu"], with_storage["memory_mb"]) == (several["cpu"], several["memory_mb"]))

check("a disk a product does state wins over the floor",
      sizing.combined(["harvester"])["disk_gb"] == 250)
check("the recommended level is asked for separately, and is never smaller",
      sizing.combined(["harvester"], level="recommended") == {"cpu": 16, "memory_mb": 65536, "disk_gb": 500})
check("a product with no recommended figures falls back to its minimum, which is all it documents",
      sizing.combined(["rancher"], level="recommended") == sizing.combined(["rancher"]))
check("an unknown name contributes nothing, so a whole addons[] list can be passed",
      sizing.combined(["rke2", "nextcloud", "wordpress"]) == one)
check("nothing at all is no requirement, not a crash",
      sizing.combined([]) == {"cpu": 0, "memory_mb": 0, "disk_gb": sizing.BASE_DISK_GB})


# ── binding(): which product set the figure ──────────────────────────────
check("the product with the largest requirement is the one named",
      sizing.binding(["rke2", "rancher"], "memory_mb")[0] == "Rancher")
check("for the disk it is whoever asked for storage of its own, not whoever has the biggest root",
      sizing.binding(["rke2", "rancher", "longhorn"], "disk_gb")[0] == "Longhorn")
check("when nothing documents a disk at all, this project's own floor answers for it",
      sizing.binding(["rke2"], "disk_gb") == ("lab-in-a-box's own floor for a node image", ""))
check("the source comes back with the title, so a warning can point at the page",
      sizing.binding(["harvester"], "cpu")[1] == sizing.PRODUCTS["harvester"]["source"])


# ── notes() ──────────────────────────────────────────────────────────────
check("notes() keeps the order it was given, for a message that lists what a node runs",
      [t for t, _n, _s in sizing.notes(["rancher", "rke2"])] == ["Rancher", "RKE2"])
check("...and leaves out what it has no figures for",
      sizing.notes(["rke2", "nextcloud"]) == sizing.notes(["rke2"]))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all sizing checks passed")
