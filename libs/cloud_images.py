#!/usr/bin/env python3
"""
The images a public cloud provider publishes for the operating systems SUSE does not publish, looked up by product
name so a lab definition never carries an AMI ID that goes stale.

SUSE's own products (SLES, SL Micro, openSUSE Leap) come from libs/suse_catalogue.py instead, which reads SUSE's
public image tracker. This module covers the rest, through two mechanisms:

  - An AWS Systems Manager public parameter, where the vendor publishes one. The parameter always names that
    vendor's current AMI in the caller's region, so there is nothing to search and nothing to match by name.
  - ec2 describe-images restricted to the vendor's own AWS account ID, where no such parameter exists. The owner
    restriction is what makes the name pattern safe: without it, any account could publish an image whose name
    matches and be picked up.

    aws_product_image(product, arch, run_aws)  -> AMI ID, or "" when the product is not one this module knows
    product_names()                            -> every product name a lab definition may use, with what it means
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

__version__ = "__LABVERSION__"

# Each product carries a "label", what it resolves to in words, and then either an "ssm" parameter path per
# architecture, or an "owner" AWS account ID with a "name" pattern per architecture. Written out per product and
# architecture rather than composed from templates: the vendors disagree on how they spell an architecture
# ("amd64", "x86_64") and on where it sits in the string, and a literal is what can be checked against the
# vendor's own documentation.
AWS_PRODUCTS = {
    "ubuntu24.04": {
        "label": "Ubuntu 24.04 LTS Server, published by Canonical",
        "ssm": {
            "x86_64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
            "arm64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id",
        },
    },
    "debian13": {
        "label": "Debian 13, published by the Debian project",
        "ssm": {
            "x86_64": "/aws/service/debian/release/13/latest/amd64",
            "arm64": "/aws/service/debian/release/13/latest/arm64",
        },
    },
    "amazonlinux2023": {
        "label": "Amazon Linux 2023, published by AWS",
        "ssm": {
            "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
            "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
        },
    },
    "rhel9": {
        "label": "the newest Red Hat Enterprise Linux 9.x, published by Red Hat",
        "owner": "309956199498",
        "name": {
            "x86_64": "RHEL-9.*_HVM-*-x86_64-*-Hourly2-GP*",
            "arm64": "RHEL-9.*_HVM-*-arm64-*-Hourly2-GP*",
        },
    },
    "rhel10": {
        "label": "the newest Red Hat Enterprise Linux 10.x, published by Red Hat",
        "owner": "309956199498",
        "name": {
            "x86_64": "RHEL-10.*_HVM-*-x86_64-*-Hourly2-GP*",
            "arm64": "RHEL-10.*_HVM-*-arm64-*-Hourly2-GP*",
        },
    },
    "alma10": {
        "label": "the newest AlmaLinux OS 10.x, published by the AlmaLinux project",
        "owner": "764336703387",
        "name": {
            "x86_64": "AlmaLinux OS 10*x86_64*",
            "arm64": "AlmaLinux OS 10*aarch64*",
        },
    },
    "rocky10": {
        "label": "the newest Rocky Linux 10, published by the Rocky project",
        "owner": "792107900819",
        "name": {
            "x86_64": "Rocky-10-EC2-Base-*.x86_64",
            "arm64": "Rocky-10-EC2-Base-*.aarch64",
        },
    },
}


def aws_product_image(product: str, arch: str, run_aws) -> str:
    """
    The current AMI ID for `product` ("ubuntu24.04", "rhel9", ...) on `arch` ("x86_64" or "arm64").

    `run_aws` is a callable taking the `aws` CLI arguments after the region and output format and returning the
    parsed JSON, i.e. AWSBackend._aws. The region is therefore the caller's, never this module's business.

    Returns "" when `product` is not a known product name, so a caller can fall through to its other resolvers and
    finally to treating the value as a literal image ID. Raises RuntimeError, from `run_aws`, when the lookup itself
    fails, and ValueError when a known product has no image for `arch`.
    """
    entry = AWS_PRODUCTS.get(str(product or "").strip().lower())
    if not entry:
        return ""
    if "ssm" in entry:
        path = entry["ssm"].get(arch)
        if not path:
            raise ValueError("no {} image is published for architecture '{}'".format(product, arch))
        result = run_aws("ssm", "get-parameters", "--names", path)
        parameters = (result or {}).get("Parameters") or []
        if not parameters:
            raise ValueError("AWS published no value for the {} image parameter '{}' in this "
                             "region".format(product, path))
        return parameters[0].get("Value", "")

    pattern = entry["name"].get(arch)
    if not pattern:
        raise ValueError("no {} image is published for architecture '{}'".format(product, arch))
    result = run_aws("ec2", "describe-images", "--owners", entry["owner"],
                     "--filters", "Name=name,Values={}".format(pattern),
                     "Name=architecture,Values={}".format(arch),
                     "Name=state,Values=available")
    images = (result or {}).get("Images") or []
    if not images:
        raise ValueError("account {} publishes no available {} image matching '{}' in this "
                         "region".format(entry["owner"], product, pattern))
    return max(images, key=lambda image: image.get("CreationDate", "")).get("ImageId", "")


# The product names SUSE's own image tracker answers, which libs/suse_catalogue.py resolves rather than this module.
# A version is optional and may be a service pack: "sles15.7" is SLES 15 SP7, "sles" is the newest SLES there is.
SUSE_PRODUCTS = {
    "sles": "the newest SUSE Linux Enterprise Server, any version",
    "sles16": "the newest SUSE Linux Enterprise Server 16.x",
    "sles15.7": "SUSE Linux Enterprise Server 15 SP7",
    "slmicro": "the newest SUSE Linux Micro, any version",
    "slmicro6.1": "SUSE Linux Micro 6.1",
    "leap": "the newest openSUSE Leap",
}


def product_names() -> dict:
    """
    Every product name a lab definition's SOURCE_IMAGE may use instead of a provider image ID, mapped to what it
    resolves to. The one source of truth for the web builder's own list of them and for the documentation.
    """
    names = dict(SUSE_PRODUCTS)
    names.update({product: entry["label"] for product, entry in AWS_PRODUCTS.items()})
    return names
