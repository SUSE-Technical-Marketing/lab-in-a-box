#!/usr/bin/env python3
"""
The EC2 instance types a region actually offers, with their on-demand prices, so a lab definition states the CPU and
memory a node needs and the backend buys the cheapest instance that satisfies it.

Two AWS APIs, kept separate because they need different permissions and different endpoints:

  - ec2 describe-instance-types supplies the specifications. It is region-scoped, so it already answers "is this
    type available here", and it needs only the ec2:DescribeInstanceTypes permission every other call here uses.
  - pricing get-products supplies the prices. It is a different service, needs pricing:GetProducts, and its endpoint
    lives in a fixed region of its own rather than the lab's region.

Intersecting the two means regional availability needs no separate query: a type absent from either mapping is never
a candidate.

    region_instance_types(run_aws, arch)        -> {type name: (vcpus, memory GiB)}
    region_prices(run_aws, region)              -> {type name: USD per hour}, empty when pricing is unavailable
    cheapest_sufficient(cpu, mem_mb, specs, prices) -> type name, or "" when nothing qualifies
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
from typing import Dict, Tuple

__version__ = "__LABVERSION__"

# The region hosting the Price List API endpoint. The API is not offered in every region, and the prices it returns
# are selected by the regionCode filter rather than by the endpoint, so this is unrelated to the lab's own region.
PRICING_ENDPOINT_REGION = "us-east-1"

# Price List filters pinning a single comparable price per instance type: a plain Linux instance on shared tenancy
# with no bundled software. Without capacitystatus the same type also returns its reservation prices.
_PRICE_FILTERS = (
    "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
    "Type=TERM_MATCH,Field=tenancy,Value=Shared",
    "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
    "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
)


def region_instance_types(run_aws, arch: str = "x86_64") -> Dict[str, Tuple[int, float]]:
    """
    Every current-generation, on-demand instance type the caller's region offers for `arch` ("x86_64" or "arm64"),
    mapped to its (vCPU count, memory in GiB).

    `run_aws` is a callable taking the `aws` CLI arguments after the region and output format and returning the
    parsed JSON, i.e. AWSBackend._aws. The region is therefore the caller's, never this module's business.

    Raises RuntimeError, from `run_aws`, when the lookup itself fails.
    """
    result = run_aws(
        "ec2", "describe-instance-types",
        "--filters",
        "Name=current-generation,Values=true",
        "Name=supported-usage-class,Values=on-demand",
        "Name=processor-info.supported-architecture,Values={}".format(arch),
    )
    specs = {}
    for entry in (result or {}).get("InstanceTypes") or []:
        name = entry.get("InstanceType")
        vcpus = (entry.get("VCpuInfo") or {}).get("DefaultVCpus")
        mem_mib = (entry.get("MemoryInfo") or {}).get("SizeInMiB")
        if name and vcpus and mem_mib:
            specs[name] = (int(vcpus), int(mem_mib) / 1024.0)
    return specs


def region_prices(run_aws, region: str, endpoint_region: str = PRICING_ENDPOINT_REGION) -> Dict[str, float]:
    """
    The on-demand USD-per-hour price of every instance type priced in `region`, keyed by type name.

    `run_aws` is AWSBackend._aws, called with region=`endpoint_region` because the Price List API is not served from
    every region. Returns an empty mapping when the API is unreachable, the `aws` CLI is absent, or the credentials
    lack pricing:GetProducts, so a caller can fall back to its own sizing table instead of failing.
    """
    try:
        result = run_aws(
            "pricing", "get-products",
            "--service-code", "AmazonEC2",
            "--filters", "Type=TERM_MATCH,Field=regionCode,Value={}".format(region), *_PRICE_FILTERS,
            region=endpoint_region,
        )
    except (RuntimeError, OSError, ValueError):
        return {}
    prices = {}
    for item in (result or {}).get("PriceList") or []:
        try:
            entry = json.loads(item) if isinstance(item, str) else item
            name = ((entry.get("product") or {}).get("attributes") or {}).get("instanceType")
            if not name:
                continue
            for term in ((entry.get("terms") or {}).get("OnDemand") or {}).values():
                for dimension in (term.get("priceDimensions") or {}).values():
                    price = float(((dimension.get("pricePerUnit") or {}).get("USD")) or 0.0)
                    # A zero price is a placeholder rather than a free instance, and some types carry several
                    # dimensions, so the lowest real one is the comparable figure.
                    if price > 0 and (name not in prices or price < prices[name]):
                        prices[name] = price
        except (ValueError, AttributeError):
            continue
    return prices


def cheapest_sufficient(vm_cpu: int, vm_mem_mb: int, specs: Dict[str, Tuple[int, float]],
                        prices: Dict[str, float]) -> str:
    """
    The name of the cheapest instance type in `specs` with at least `vm_cpu` vCPUs and `vm_mem_mb` MiB of memory and
    a known price in `prices`.

    Returns "" when either mapping is empty or nothing satisfies both minimums, which is a caller's signal to size
    the VM from its own table rather than to guess at a price.
    """
    needed_mem_gib = int(vm_mem_mb) / 1024.0
    candidates = [
        (price, name) for name, price in prices.items()
        if name in specs and specs[name][0] >= int(vm_cpu) and specs[name][1] >= needed_mem_gib
    ]
    return min(candidates)[1] if candidates else ""
