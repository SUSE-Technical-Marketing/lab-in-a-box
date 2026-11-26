#!/usr/bin/env python3
# Part of lab-in-a-box — what each product documents as its own hardware minimum.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/sizing.py: the documented hardware requirements of the products this project installs, and what a node
running several of them therefore needs.

Anything that chooses VM_MEM, VM_CPU or VM_DSK for a node — the step-by-step lab builder above all — reads them
from here rather than guessing, and every entry carries the URL its numbers came from so that a figure can be
checked and corrected against its own documentation rather than against somebody's memory.

What each entry means:

    cpu, memory_mb, disk_gb         the product's documented MINIMUM for one node that runs it. 0 means the
                                    documentation states none, which is common for disk.
    rec_cpu, rec_memory_mb,
    rec_disk_gb                     the RECOMMENDED figures, where the documentation distinguishes them. 0 means
                                    it gives only one set, and then the minimum is all there is.
    extra_disk_gb                   storage this product wants of its own, on top of the node's own disk: a
                                    volume for a database, or a disk dedicated to a storage system. Added rather
                                    than compared, because it is space nothing else gets to use.
    nodes                           the smallest node count the documentation calls for.
    bare_metal                      True for a product whose own documentation rules out running it in a VM.
    source, note                    where the figures came from, and anything about them a reader needs.

combined() is the rule for a node that runs more than one: CPU and memory take the largest requirement, because
each product's figure already includes the operating system under it, and disk takes the largest plus every
product's own extra storage. BASE_DISK_GB is this project's floor for a node image, since several products
document no disk figure at all and a lab still has to write an image somewhere.
"""

# A node image with an operating system, a container runtime and room for what a lab pulls onto it. No product
# documents this: it is what this project's own example labs have used for years (examples/*.json.template).
BASE_DISK_GB = 60

_SOURCES = {
    "rke2": "https://docs.rke2.io/install/requirements",
    "k3s": "https://docs.k3s.io/installation/requirements",
    "rancher": "https://ranchermanager.docs.rancher.com/getting-started/installation-and-upgrade/"
               "installation-requirements",
    "harvester": "https://docs.harvesterhci.io/v1.4/install/requirements",
    "uyuni": "https://www.uyuni-project.org/uyuni-docs/en/uyuni/installation-and-upgrade/"
             "hardware-requirements.html",
    "longhorn": "https://longhorn.io/docs/archives/1.7.2/best-practices/",
}

PRODUCTS = {
    "rke2": {
        "title": "RKE2",
        "cpu": 2, "memory_mb": 4096, "disk_gb": 0,
        "rec_cpu": 4, "rec_memory_mb": 8192, "rec_disk_gb": 0,
        "extra_disk_gb": 0, "nodes": 1, "bare_metal": False,
        "source": _SOURCES["rke2"],
        "note": "One set of figures for servers and agents alike. The page recommends an SSD and gives no disk "
                "size; its own sizing tests used 20 GiB volumes.",
    },
    "k3s": {
        "title": "K3s server",
        "cpu": 2, "memory_mb": 2048, "disk_gb": 0,
        "rec_cpu": 2, "rec_memory_mb": 4096, "rec_disk_gb": 0,
        "extra_disk_gb": 0, "nodes": 1, "bare_metal": False,
        "source": _SOURCES["k3s"],
        "note": "The minimum table gives 2 cores and 2 GB for a server; the sizing guide starts a server at 2 "
                "CPU and 4 GB for up to 350 agents, which is what the recommended figures are.",
    },
    "k3s-agent": {
        "title": "K3s agent",
        "cpu": 1, "memory_mb": 512, "disk_gb": 0,
        "rec_cpu": 0, "rec_memory_mb": 0, "rec_disk_gb": 0,
        "extra_disk_gb": 0, "nodes": 1, "bare_metal": False,
        "source": _SOURCES["k3s"],
        "note": "The only role either Kubernetes distribution documents separately, and the smallest figure in "
                "this table.",
    },
    "rancher": {
        "title": "Rancher",
        "cpu": 4, "memory_mb": 16384, "disk_gb": 0,
        "rec_cpu": 0, "rec_memory_mb": 0, "rec_disk_gb": 0,
        "extra_disk_gb": 0, "nodes": 3, "bare_metal": False,
        "source": _SOURCES["rancher"],
        "note": "Per node of the cluster Rancher itself runs on, for its smallest documented size: up to 150 "
                "managed clusters and 1500 nodes. Three nodes are what it calls for in production; one works "
                "for a lab and loses high availability. Its next sizes are 8 vCPU with 32 GB and 16 with 64.",
    },
    "harvester": {
        "title": "Harvester",
        "cpu": 8, "memory_mb": 32768, "disk_gb": 250,
        "rec_cpu": 16, "rec_memory_mb": 65536, "rec_disk_gb": 500,
        "extra_disk_gb": 0, "nodes": 3, "bare_metal": True,
        "source": _SOURCES["harvester"],
        "note": "The minimum is its own development and testing figure; the recommended one is what it asks for "
                "in production. It needs hardware-assisted virtualization, and its documentation rules out "
                "laptops and nested virtualization. Three nodes are needed for its multi-node features; one "
                "works and has no high availability, no replicas and no live migration.",
    },
    "smlm": {
        "title": "SUSE Multi-Linux Manager",
        "cpu": 4, "memory_mb": 16384, "disk_gb": 40,
        "rec_cpu": 4, "rec_memory_mb": 32768, "rec_disk_gb": 0,
        "extra_disk_gb": 200, "nodes": 1, "bare_metal": False,
        "source": _SOURCES["uyuni"],
        "note": "From Uyuni's own hardware requirements, which is the upstream this product is built from: "
                "4 dedicated cores, 16 GB (32 recommended), 40 GB for the root filesystem, and volumes of at "
                "least 150 GB plus 50 GB for PostgreSQL, which is the extra storage here. SUSE's own page for "
                "it could not be read when this table was written, so correct these against it rather than "
                "trusting the lineage.",
    },
    "uyuni": {
        "title": "Uyuni",
        "cpu": 4, "memory_mb": 16384, "disk_gb": 40,
        "rec_cpu": 4, "rec_memory_mb": 32768, "rec_disk_gb": 0,
        "extra_disk_gb": 200, "nodes": 1, "bare_metal": False,
        "source": _SOURCES["uyuni"],
        "note": "4 dedicated cores, 16 GB (32 recommended), 40 GB for the root filesystem, volumes of at least "
                "150 GB and 50 GB for PostgreSQL. Each further product it mirrors adds 50 GB, or 360 GB for a "
                "Red Hat one, so a lab mirroring several needs more than the extra storage here.",
    },
    "longhorn": {
        "title": "Longhorn",
        "cpu": 4, "memory_mb": 4096, "disk_gb": 0,
        "rec_cpu": 0, "rec_memory_mb": 0, "rec_disk_gb": 0,
        "extra_disk_gb": 100, "nodes": 3, "bare_metal": False,
        "source": _SOURCES["longhorn"],
        "note": "Its best practices ask for three nodes, 4 vCPU and 4 GiB each. It states no disk size; the "
                "extra storage here is a disk of its own, which it recommends over the root disk, and which "
                "lab-in-a-box gives a node through extra_dsk.",
    },
}

# Which lab-definition field each figure is compared against, and read from. A caller iterates these rather
# than spelling the three names itself.
FIELDS = (("cpu", "VM_CPU"), ("memory_mb", "VM_MEM"), ("disk_gb", "VM_DSK"))


def known(name):
    """Whether `name` is a product this table holds."""
    return name in PRODUCTS


def _value(entry, key, level):
    """One figure at `level`: the recommended one when it exists, otherwise the minimum, which is all there is."""
    if level == "recommended":
        return entry.get("rec_{}".format(key)) or entry.get(key) or 0
    return entry.get(key) or 0


def combined(names, level="minimum"):
    """
    What one node running every product in `names` needs, as {"cpu", "memory_mb", "disk_gb"}.

    CPU and memory take the largest of them, because each product's own figure already covers the operating
    system beneath it; adding them would size a node for several copies of that. Disk takes the largest, never
    below BASE_DISK_GB, plus every product's own extra storage, which is space nothing else gets to use.

    An unknown name contributes nothing, so a caller may pass a node's whole add-on list and let this answer for
    the ones that document a requirement.
    """
    entries = [PRODUCTS[name] for name in names if name in PRODUCTS]
    cpu = max([_value(e, "cpu", level) for e in entries] or [0])
    memory = max([_value(e, "memory_mb", level) for e in entries] or [0])
    disk = max([_value(e, "disk_gb", level) for e in entries] + [BASE_DISK_GB])
    disk += sum(e.get("extra_disk_gb") or 0 for e in entries)
    return {"cpu": cpu, "memory_mb": memory, "disk_gb": disk}


def binding(names, key, level="minimum"):
    """
    Which product's figure sets the requirement for `key` ("cpu", "memory_mb" or "disk_gb"), as
    (title, source URL). For disk, where every entry may state nothing and BASE_DISK_GB is the floor, that floor
    answers for this project itself.
    """
    entries = [PRODUCTS[name] for name in names if name in PRODUCTS]
    if key == "disk_gb":
        # The figure is the largest stated disk, or this project's floor, plus every product's own extra
        # storage, so what to point at is whichever product asks for the most of that extra storage.
        extra = sorted(((e.get("extra_disk_gb") or 0, e["title"], e["source"]) for e in entries), reverse=True)
        if extra and extra[0][0] > 0:
            return (extra[0][1], extra[0][2])
    best, title, source = 0, "", ""
    for entry in entries:
        value = _value(entry, key, level)
        if value > best:
            best, title, source = value, entry["title"], entry["source"]
    if key == "disk_gb" and best < BASE_DISK_GB:
        return ("lab-in-a-box's own floor for a node image", "")
    return (title, source)


def notes(names):
    """[(title, note, source)] for each product named, in the order given, for a caller that explains its sizing."""
    return [(PRODUCTS[name]["title"], PRODUCTS[name]["note"], PRODUCTS[name]["source"])
            for name in names if name in PRODUCTS]
