#!/usr/bin/env python3.11
# Part of lab-in-a-box — the cloud image catalogue checked against the provider itself, for contributors
# Author/s: Raul Mahiques
# License: GPLv3

"""
check_cloud_images.py — resolve every product name in libs/cloud_images.py against AWS itself.

The suite's own checks fake the AWS calls, so they prove the resolution logic and never the catalogue: a vendor
renaming its images, or publishing none in a region, passes every container check and fails only in a real lab.
This asks AWS, so it needs credentials and a network and is run by hand rather than by the suite.

Usage:
    check_cloud_images.py [--region <region>] [--profile <profile>] [--product <name>]
        Resolve each product for both architectures and print one line per result. Exit 1 when any known
        product resolves to nothing, 0 when they all resolve or no credentials are configured.
    check_cloud_images.py --help
        Print this help.

Options:
    --region <region>    AWS region to resolve in (default: the environment's own, else eu-west-2).
    --profile <profile>  AWS CLI profile to use (default: the environment's own).
    --product <name>     Check only this product name, instead of all of them.

An architecture a product does not publish is reported as "not published", which is a declared state and not a
failure. Only a product that publishes nothing for an architecture it claims is a failure.
"""

__version__ = "__LABVERSION__"

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "libs"))

import cloud_images  # noqa: E402

ARCHES = ("x86_64", "arm64")
DEFAULT_REGION = "eu-west-2"


def make_run_aws(default_region: str, profile: str):
    """
    An AWSBackend._aws-shaped callable: positional `aws` CLI arguments, an optional `region` overriding the default
    for an API served only from its own regions, returning the parsed JSON. Raises RuntimeError when the CLI fails.
    """
    def run_aws(*args, region=""):
        cmd = ["aws"]
        if profile:
            cmd += ["--profile", profile]
        cmd += ["--region", region or default_region, "--output", "json"] + list(args)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "aws exited {}".format(proc.returncode))
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    return run_aws


def have_credentials(run_aws) -> bool:
    """Whether the configured credentials work at all, so a missing login is reported rather than 60 failures."""
    try:
        run_aws("sts", "get-caller-identity")
        return True
    except (RuntimeError, OSError, ValueError):
        return False


def check_product(product: str, run_aws) -> list:
    """One result line per architecture of `product`, each as (architecture, state, detail)."""
    results = []
    for arch in ARCHES:
        try:
            image = cloud_images.aws_product_image(product, arch, run_aws)
        except ValueError as exc:
            message = str(exc)
            state = "not published" if "no {} image is published".format(product) in message else "FAIL"
            results.append((arch, state, message))
            continue
        except (RuntimeError, OSError) as exc:
            results.append((arch, "FAIL", str(exc)))
            continue
        results.append((arch, "ok", image) if image else (arch, "FAIL", "resolved to nothing"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or DEFAULT_REGION)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    parser.add_argument("--product", default="")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()
    if args.help:
        print(__doc__.strip())
        return 0

    products = [args.product] if args.product else sorted(cloud_images.AWS_PRODUCTS)
    unknown = [p for p in products if p not in cloud_images.AWS_PRODUCTS]
    if unknown:
        print("unknown product(s): {}".format(", ".join(unknown)))
        print("known: {}".format(", ".join(sorted(cloud_images.AWS_PRODUCTS))))
        return 1

    run_aws = make_run_aws(args.region, args.profile)
    if not have_credentials(run_aws):
        print("no working AWS credentials{} — nothing checked".format(
            " for profile '{}'".format(args.profile) if args.profile else ""))
        return 0

    print("resolving {} product(s) in {}".format(len(products), args.region))
    failures = 0
    for product in products:
        for arch, state, detail in check_product(product, run_aws):
            if state == "FAIL":
                failures += 1
            print("  {:<14} {:<8} {:<14} {}".format(product, arch, state, detail))
    print("{} failure(s)".format(failures) if failures else "every product resolves")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
