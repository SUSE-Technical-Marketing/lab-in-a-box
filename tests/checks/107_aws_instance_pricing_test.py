#!/usr/bin/env python3
# Unit tests for libs/aws_catalog.py's live instance-type catalog and the cheapest-sufficient sizing
# AWSBackend._pick_instance_type() builds on it: the describe-instance-types filters, the Price List
# parsing and its own endpoint region, the degraded paths that leave sizing to the built-in table, and
# the per-region cache. Every AWS call is faked, so nothing here reaches a network or an account.
# Run from 107_aws_instance_pricing.sh, in its own container — see tests/run_tests.sh.
import json
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import aws_catalog  # noqa: E402
import backends     # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _type(name, vcpus, mem_mib):
    return {"InstanceType": name, "VCpuInfo": {"DefaultVCpus": vcpus},
            "MemoryInfo": {"SizeInMiB": mem_mib}}


def _price(name, usd):
    return json.dumps({
        "product": {"attributes": {"instanceType": name}},
        "terms": {"OnDemand": {"term1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": str(usd)}}}}}},
    })


# ── region_instance_types(): filters sent, specs parsed ──────────────────────
calls = []


def _fake_specs(*args, **kwargs):
    calls.append((args, kwargs))
    return {"InstanceTypes": [
        _type("t3.micro", 2, 1024),
        _type("t3a.medium", 2, 4096),
        {"InstanceType": "broken.large"},  # no VCpuInfo/MemoryInfo at all
    ]}


specs = aws_catalog.region_instance_types(_fake_specs, "x86_64")
check("region_instance_types() returns (vcpus, memory GiB) per type",
      specs == {"t3.micro": (2, 1.0), "t3a.medium": (2, 4.0)})
check("region_instance_types() skips an entry missing its CPU or memory block",
      "broken.large" not in specs)
_args = calls[0][0]
check("region_instance_types() asks only for current-generation types",
      "Name=current-generation,Values=true" in _args)
check("region_instance_types() asks only for on-demand types",
      "Name=supported-usage-class,Values=on-demand" in _args)
check("region_instance_types() filters on the architecture it was given",
      "Name=processor-info.supported-architecture,Values=x86_64" in _args)
check("region_instance_types() sends no region of its own, so the caller's region decides",
      not calls[0][1].get("region"))

aws_catalog.region_instance_types(_fake_specs, "arm64")
check("region_instance_types() passes arm64 through to the filter",
      "Name=processor-info.supported-architecture,Values=arm64" in calls[1][0])


# ── region_prices(): Price List parsing and its own endpoint region ──────────
price_calls = []


def _fake_prices(*args, **kwargs):
    price_calls.append((args, kwargs))
    return {"PriceList": [
        _price("t3.medium", 0.0472),
        _price("t3a.medium", 0.0425),
        _price("t3a.medium", 0.9999),   # a second, dearer dimension for the same type
        _price("free.nano", 0.0),       # a zero price is a placeholder, not a free instance
        "{not json",
    ]}


prices = aws_catalog.region_prices(_fake_prices, "eu-west-2")
check("region_prices() returns USD per hour per instance type",
      prices.get("t3.medium") == 0.0472)
check("region_prices() keeps the lowest real price when a type carries several",
      prices.get("t3a.medium") == 0.0425)
check("region_prices() drops a zero price rather than reporting a free instance",
      "free.nano" not in prices)
check("region_prices() skips an unparseable Price List entry instead of failing", len(prices) == 2)
check("region_prices() selects the region's prices with the regionCode filter",
      "Type=TERM_MATCH,Field=regionCode,Value=eu-west-2" in price_calls[0][0])
check("region_prices() pins one comparable price per type (Linux, shared tenancy, no bundled software)",
      all(f in price_calls[0][0] for f in (
          "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
          "Type=TERM_MATCH,Field=tenancy,Value=Shared",
          "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
          "Type=TERM_MATCH,Field=capacitystatus,Value=Used")))
check("region_prices() calls the Price List endpoint region, not the lab's region",
      price_calls[0][1].get("region") == aws_catalog.PRICING_ENDPOINT_REGION
      and aws_catalog.PRICING_ENDPOINT_REGION != "eu-west-2")


def _denied(*args, **kwargs):
    raise RuntimeError("AccessDeniedException: pricing:GetProducts")


def _no_cli(*args, **kwargs):
    raise FileNotFoundError("aws")


check("region_prices() returns nothing when the credentials lack pricing:GetProducts",
      aws_catalog.region_prices(_denied, "eu-west-2") == {})
check("region_prices() returns nothing when the aws CLI is absent, rather than raising",
      aws_catalog.region_prices(_no_cli, "eu-west-2") == {})


# ── cheapest_sufficient(): price decides among the types that fit ────────────
_specs = {"t3.micro": (2, 1.0), "t3.medium": (2, 4.0), "t3a.medium": (2, 4.0),
          "m7i.large": (2, 8.0), "unpriced.medium": (2, 4.0)}
_prices = {"t3.micro": 0.0118, "t3.medium": 0.0472, "t3a.medium": 0.0425, "m7i.large": 0.1008}

check("cheapest_sufficient() picks the cheapest type meeting both minimums",
      aws_catalog.cheapest_sufficient(2, 4096, _specs, _prices) == "t3a.medium")
check("cheapest_sufficient() honours the memory minimum over the price",
      aws_catalog.cheapest_sufficient(2, 8192, _specs, _prices) == "m7i.large")
check("cheapest_sufficient() honours the CPU minimum",
      aws_catalog.cheapest_sufficient(1, 1024, _specs, _prices) == "t3.micro")
check("cheapest_sufficient() ignores a type with no known price",
      aws_catalog.cheapest_sufficient(2, 4096, {"unpriced.medium": (2, 4.0)}, {}) == "")
check("cheapest_sufficient() returns nothing when no type is big enough",
      aws_catalog.cheapest_sufficient(64, 999999, _specs, _prices) == "")
check("cheapest_sufficient() returns nothing when the specs lookup came back empty",
      aws_catalog.cheapest_sufficient(2, 4096, {}, _prices) == "")


# ── AWSBackend wiring: the per-region cache and the arm64 abstention ─────────
backends.AWSBackend._CATALOG_CACHE.clear()
lookups = []


def _one_region(*args, **kwargs):
    lookups.append(args[1] if len(args) > 1 else args[0])
    if "describe-instance-types" in args:
        return {"InstanceTypes": [_type("t3.medium", 2, 4096), _type("t3a.medium", 2, 4096)]}
    return {"PriceList": [_price("t3.medium", 0.0472), _price("t3a.medium", 0.0425)]}


b = backends.AWSBackend("eu-west-2", profile="lab")
with mock.patch.object(b, "_aws", side_effect=_one_region):
    first = b._pick_instance_type(2, 4096, "vm1")
    second = b._pick_instance_type(2, 4096, "vm2")
check("_pick_instance_type() buys the cheapest sufficient type from the live catalog",
      first == "t3a.medium" and second == "t3a.medium")
check("the catalog is fetched once per region, not once per VM",
      lookups.count("describe-instance-types") == 1 and lookups.count("get-products") == 1)

backends.AWSBackend._CATALOG_CACHE.clear()
with mock.patch.object(b, "_aws", side_effect=_no_cli):
    check("_pick_instance_type() falls back to the sizing table when no aws CLI is present",
          b._pick_instance_type(2, 4096, "vm1") == "t3.medium")

backends.AWSBackend._CATALOG_CACHE.clear()
check("the built-in table names only x86_64 burstable types, so arm64 is never picked by default",
      all(backends.AWSBackend.instance_arch(name) == "x86_64"
          for name, _cores, _mem in backends.AWSBackend.INSTANCE_TYPES))
check("the built-in table starts at 1 GiB rather than flooring every node at 4 GiB",
      min(mem for _name, _cores, mem in backends.AWSBackend.INSTANCE_TYPES) == 1)


# ── _aws(): the per-call region override the Price List endpoint needs ───────
ran = []


def _capture(args, **kwargs):
    ran.append(args)
    return mock.Mock(returncode=0, stdout="{}", stderr="")


b2 = backends.AWSBackend("eu-west-2", profile="lab")
with mock.patch.object(backends.subprocess, "run", side_effect=_capture):
    b2._aws("ec2", "describe-instances")
    b2._aws("pricing", "get-products", region="us-east-1")
check("_aws() uses the backend's own region by default",
      ran[0][ran[0].index("--region") + 1] == "eu-west-2")
check("_aws() uses the region override for one call when given one",
      ran[1][ran[1].index("--region") + 1] == "us-east-1")

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all aws_instance_pricing checks passed")
