#!/usr/bin/env python3
# Unit tests for product-name image resolution: libs/suse_catalogue.py's own image pickers,
# libs/cloud_images.py's AWS product table, AWSBackend.instance_arch() and
# AWSBackend.resolve_image_name() tying them together, plus AWSBackend.use_dns_server()'s VPC DHCP
# option set. Every provider call is faked from a recorded image list, so nothing here reaches a
# network or a cloud account. Run from 100_image_resolution.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends          # noqa: E402
import cloud_images      # noqa: E402
import suse_catalogue    # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _img(name, image_id, published="20260101"):
    return {"name": name, "id": image_id, "publishedon": published}


# The shapes SUSE's own tracker publishes for AWS: SLES 16 with a numeric minor, SLES 15 with a
# service pack, SL Micro with a billing-entity suffix after the architecture, and Leap.
IMAGES = [
    _img("suse-sles-16-0-v20261008-hvm-ssd-x86_64", "ami-sles16", "20261008"),
    _img("suse-sles-16-0-v20260101-hvm-ssd-x86_64", "ami-sles16-old", "20260101"),
    _img("suse-sles-16-0-v20261008-hvm-ssd-arm64", "ami-sles16-arm", "20261008"),
    _img("suse-sles-16-0-v20261111-ecs-hvm-ssd-x86_64", "ami-sles16-ecs", "20261111"),
    _img("suse-sles-16-0-byos-v20261111-hvm-ssd-x86_64", "ami-sles16-byos", "20261111"),
    _img("suse-sles-15-sp7-v20261014-hvm-ssd-x86_64", "ami-sles15sp7", "20261014"),
    _img("suse-sles-15-sp6-v20261014-hvm-ssd-x86_64", "ami-sles15sp6", "20261014"),
    _img("suse-sle-micro-6-1-v20260914-hvm-ssd-x86_64-ltd", "ami-micro61", "20260914"),
    _img("suse-sle-micro-6-1-v20260914-hvm-ssd-arm64-ltd", "ami-micro61-arm", "20260914"),
    _img("suse-sle-micro-6-0-v20261111-hvm-ssd-x86_64-ltd", "ami-micro60", "20261111"),
    _img("suse-sle-micro-6-1-byos-v20260914-hvm-ssd-x86_64", "ami-micro61-byos", "20260914"),
    _img("openSUSE-Leap-16-0-v20261015-hvm-ssd-x86_64", "ami-leap16", "20261015"),
]


# ── suse_catalogue.arch_matches() ─────────────────────────────────────────
check("arch_matches() accepts a name ending in the architecture",
      suse_catalogue.arch_matches("suse-sles-16-0-v1-hvm-ssd-x86_64", "-x86_64"))
check("arch_matches() accepts an architecture followed by a billing-entity suffix",
      suse_catalogue.arch_matches("suse-sle-micro-6-1-v1-hvm-ssd-x86_64-ltd", "-x86_64"))
check("arch_matches() does not accept the other architecture",
      not suse_catalogue.arch_matches("suse-sle-micro-6-1-v1-hvm-ssd-arm64-ltd", "-x86_64"))
check("arch_matches() does not accept the architecture mid-name",
      not suse_catalogue.arch_matches("suse-x86_64-something-arm64", "-x86_64"))


# ── suse_catalogue.newest_sles(): version, service pack, variants ─────────
check("newest_sles() picks the latest build of the highest version",
      suse_catalogue.newest_sles(IMAGES, "-x86_64").get("id") == "ami-sles16")
check("newest_sles() honours major",
      suse_catalogue.newest_sles(IMAGES, "-x86_64", major=15).get("id") == "ami-sles15sp7")
check("newest_sles() honours a service pack as the minor version",
      suse_catalogue.newest_sles(IMAGES, "-x86_64", major=15, minor=6).get("id") == "ami-sles15sp6")
check("newest_sles() honours a numeric minor version",
      suse_catalogue.newest_sles(IMAGES, "-x86_64", major=16, minor=0).get("id") == "ami-sles16")
check("newest_sles() honours the architecture",
      suse_catalogue.newest_sles(IMAGES, "-arm64").get("id") == "ami-sles16-arm")
check("newest_sles() skips the ecs variant even when it is the newest build",
      suse_catalogue.newest_sles(IMAGES, "-x86_64").get("id") != "ami-sles16-ecs")
check("newest_sles() skips the byos variant even when it is the newest build",
      suse_catalogue.newest_sles(IMAGES, "-x86_64").get("id") != "ami-sles16-byos")
check("newest_sles() returns {} for a version nobody publishes",
      suse_catalogue.newest_sles(IMAGES, "-x86_64", major=99) == {})


# ── suse_catalogue.newest_micro() ─────────────────────────────────────────
check("newest_micro() picks the highest version, not the latest build of an older one",
      suse_catalogue.newest_micro(IMAGES, "-x86_64").get("id") == "ami-micro61")
check("newest_micro() honours major and minor",
      suse_catalogue.newest_micro(IMAGES, "-x86_64", major=6, minor=0).get("id") == "ami-micro60")
check("newest_micro() honours the architecture",
      suse_catalogue.newest_micro(IMAGES, "-arm64", major=6, minor=1).get("id") == "ami-micro61-arm")
check("newest_micro() skips the byos variant",
      suse_catalogue.newest_micro(IMAGES, "-x86_64", major=6, minor=1).get("id") != "ami-micro61-byos")
check("newest_micro() returns {} for an architecture nobody publishes",
      suse_catalogue.newest_micro(IMAGES, "-riscv64") == {})
check("newest_micro() is stable when two images differ only in billing entity",
      suse_catalogue.newest_micro(
          [_img("suse-sle-micro-6-1-v1-hvm-ssd-x86_64-ltd", "ami-ltd"),
           _img("suse-sle-micro-6-1-v1-hvm-ssd-x86_64-llc", "ami-llc")], "-x86_64").get("id") == "ami-ltd")


# ── cloud_images: the AWS product table ───────────────────────────────────
for product in ("ubuntu24.04", "debian13", "amazonlinux2023", "rhel9", "rhel10", "alma10", "rocky10"):
    check("cloud_images lists '{}'".format(product), product in cloud_images.AWS_PRODUCTS)
for product, entry in cloud_images.AWS_PRODUCTS.items():
    check("'{}' names either an ssm parameter or an owner, never both".format(product),
          ("ssm" in entry) != ("owner" in entry))
    check("'{}' says in words what it resolves to".format(product),
          bool(entry.get("label")) and entry["label"] == cloud_images.product_names()[product])
    arches = entry.get("ssm") or entry.get("name")
    check("'{}' covers both architectures".format(product), set(arches) == {"x86_64", "arm64"})
    if "owner" in entry:
        check("'{}' names a 12-digit AWS account as its owner".format(product),
              entry["owner"].isdigit() and len(entry["owner"]) == 12)

check("cloud_images returns '' for a product it does not know, so a caller can fall through",
      cloud_images.aws_product_image("plan9", "x86_64", None) == "")

_calls = []


def _fake_aws(*args):
    _calls.append(args)
    if args[0] == "ssm":
        return {"Parameters": [{"Value": "ami-from-ssm"}]}
    return {"Images": [{"ImageId": "ami-older", "CreationDate": "2020-01-01T00:00:00.000Z"},
                       {"ImageId": "ami-newest", "CreationDate": "2026-01-01T00:00:00.000Z"}]}


check("an ssm-published product resolves to the parameter's value",
      cloud_images.aws_product_image("ubuntu24.04", "x86_64", _fake_aws) == "ami-from-ssm")
check("the ubuntu lookup asks for the amd64 parameter on x86_64",
      any("amd64" in str(a) for a in _calls[-1]))
_calls.clear()
check("an owner-published product resolves to the newest matching image",
      cloud_images.aws_product_image("rhel9", "x86_64", _fake_aws) == "ami-newest")
check("the owner lookup restricts describe-images to the vendor's own account",
      "--owners" in _calls[-1] and cloud_images.AWS_PRODUCTS["rhel9"]["owner"] in _calls[-1])
check("the owner lookup filters on the architecture as well as the name",
      any("Name=architecture,Values=x86_64" == a for a in _calls[-1]))

try:
    cloud_images.aws_product_image("ubuntu24.04", "x86_64", lambda *a: {"Parameters": []})
    check("a product with no published value raises", False)
except ValueError:
    check("a product with no published value raises", True)

try:
    cloud_images.aws_product_image("rhel9", "riscv64", _fake_aws)
    check("a product with no image for an architecture raises", False)
except ValueError:
    check("a product with no image for an architecture raises", True)


# ── AWSBackend.instance_arch() ────────────────────────────────────────────
ARCH = {
    "t3.medium": "x86_64", "m6a.2xlarge": "x86_64", "c7i.large": "x86_64", "m5zn.large": "x86_64",
    "g4dn.xlarge": "x86_64", "": "x86_64",
    "t4g.nano": "arm64", "m7g.large": "arm64", "m6gd.large": "arm64", "x2gd.large": "arm64",
    "im4gn.large": "arm64", "g5g.xlarge": "arm64", "a1.large": "arm64",
}
for instance_type, arch in ARCH.items():
    check("instance_arch('{}') is {}".format(instance_type, arch),
          backends.AWSBackend.instance_arch(instance_type) == arch)


# ── AWSBackend.resolve_image_name() ───────────────────────────────────────
def _backend():
    b = backends.AWSBackend.__new__(backends.AWSBackend)
    b.region = "eu-central-1"
    b._aws = _fake_aws
    return b


b = _backend()
suse_catalogue.active_images = lambda provider, region="": IMAGES

RESOLVES = {
    ("sles16", "t3.medium"): "ami-sles16",
    ("sles16", "t4g.nano"): "ami-sles16-arm",
    ("sles15.7", "t3.medium"): "ami-sles15sp7",
    ("sles", "t3.medium"): "ami-sles16",
    ("slmicro6.1", "t3.medium"): "ami-micro61",
    ("slmicro6.1", "t4g.nano"): "ami-micro61-arm",
    ("micro", "t3.medium"): "ami-micro61",
    ("ubuntu24.04", "t3.medium"): "ami-from-ssm",
    ("rhel10", "t3.medium"): "ami-newest",
    ("leap", "t3.medium"): "ami-leap16",
    ("opensuse", "t3.medium"): "ami-leap16",
    ("leap16.0", "t3.medium"): "ami-leap16",
}
for (name, instance_type), expected in RESOLVES.items():
    check("resolve_image_name('{}', {}) is {}".format(name, instance_type, expected),
          b.resolve_image_name(name, instance_type=instance_type) == expected)

for literal in ("ami-0123456789abcdef0", "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2", "", "sles16-custom-build"):
    check("resolve_image_name() returns '{}' unchanged".format(literal),
          b.resolve_image_name(literal, instance_type="t3.medium") == literal)

check("opensuse_image() answers for the architecture it is asked about",
      b.opensuse_image("-x86_64") == "ami-leap16")
check("newest_leap() honours an explicit version like the other pickers do",
      suse_catalogue.newest_leap(IMAGES, "-x86_64", major=99) == {})
check("every SUSE product name maps to a picker and a label",
      all(callable(picker) and label
          for picker, label in backends.AWSBackend._SUSE_PRODUCTS.values()))

check("resolve_image_name() takes an instance_type on the base backend too, unchanged",
      backends.VMBackend.resolve_image_name(object(), "whatever", instance_type="t4g.nano") == "whatever")
check("every backend's use_dns_server() is a no-op that returns None by default",
      backends.VMBackend.use_dns_server(object(), "10.0.0.2", "mydemo.lab") is None)


# ── AWSBackend.use_dns_server(): the VPC DHCP option set ──────────────────
class _DhcpAWS:
    """Records every aws call and answers the ones use_dns_server() makes."""

    def __init__(self, current=None):
        self.calls = []
        self.current = current          # the VPC's existing DhcpConfigurations, or None for "default"

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[1]
        if verb == "describe-subnets":
            return {"Subnets": [{"VpcId": "vpc-test"}]}
        if verb == "describe-vpcs":
            return {"Vpcs": [{"DhcpOptionsId": "dopt-existing" if self.current else "default"}]}
        if verb == "describe-dhcp-options":
            return {"DhcpOptions": [{"DhcpConfigurations": self.current or []}]}
        if verb == "create-dhcp-options":
            return {"DhcpOptions": {"DhcpOptionsId": "dopt-new"}}
        return None

    def verbs(self):
        return [c[1] for c in self.calls]


def _with_dhcp(fake, subnet_id="subnet-test"):
    b = backends.AWSBackend.__new__(backends.AWSBackend)
    b.region = "eu-central-1"
    b.subnet_id = subnet_id
    b._aws = fake
    return b


fake = _DhcpAWS()
_with_dhcp(fake).use_dns_server("10.0.0.2", "mydemo.lab")
check("use_dns_server() creates a DHCP option set when the VPC has the default one",
      "create-dhcp-options" in fake.verbs())
check("use_dns_server() associates the new option set with the VPC it found",
      ("ec2", "associate-dhcp-options", "--dhcp-options-id", "dopt-new", "--vpc-id", "vpc-test") in fake.calls)
created = [c for c in fake.calls if c[1] == "create-dhcp-options"][0]
check("the option set names the DNS server it was given",
      "Key=domain-name-servers,Values=10.0.0.2" in created)
check("the option set names the lab domain as the search domain",
      "Key=domain-name,Values=mydemo.lab" in created)
check("the option set names no other DNS server, since AmazonProvidedDNS cannot be mixed in",
      not any("AmazonProvidedDNS" in str(a) for a in created))

already = [{"Key": "domain-name-servers", "Values": [{"Value": "10.0.0.2"}]},
           {"Key": "domain-name", "Values": [{"Value": "mydemo.lab"}]}]
fake = _DhcpAWS(current=already)
_with_dhcp(fake).use_dns_server("10.0.0.2", "mydemo.lab")
check("use_dns_server() is idempotent: a VPC already handing out this server is left alone",
      "create-dhcp-options" not in fake.verbs() and "associate-dhcp-options" not in fake.verbs())

fake = _DhcpAWS(current=[{"Key": "domain-name-servers", "Values": [{"Value": "10.9.9.9"}]}])
_with_dhcp(fake).use_dns_server("10.0.0.2", "mydemo.lab")
check("use_dns_server() replaces an option set naming a different server",
      "create-dhcp-options" in fake.verbs())

fake = _DhcpAWS()
_with_dhcp(fake, subnet_id=None).use_dns_server("10.0.0.2", "mydemo.lab")
check("use_dns_server() does nothing when the account has no subnet configured", not fake.calls)
fake = _DhcpAWS()
_with_dhcp(fake).use_dns_server("", "mydemo.lab")
check("use_dns_server() does nothing without a DNS server address", not fake.calls)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all image_resolution checks passed")
