#!/usr/bin/env python3
"""
SUSE Application Collection (https://apps.rancher.io) and other authenticated OCI registries: image pull secrets, helm
registry logins and the --set flags that hand pull secrets to a chart. Used by the appcollection and suse_ai add-ons.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import shlex
import sys

from lab_creation import ssh_run

DEFAULT_REGISTRY = "dp.apps.rancher.io"
SECRET_NAME = "application-collection"


# Where an add-on takes its chart from. "upstream" is the project's own Helm repository, which needs no account;
# "appcollection" is the SUSE Application Collection's OCI chart, which needs an Application Collection user and
# token (or a credential file of kind "appcollection"). An add-on declares the field <prefix>_source with these
# values and defaults to UPSTREAM, so a lab without an account keeps working exactly as before.
SOURCE_UPSTREAM = "upstream"
SOURCE_APPCOLLECTION = "appcollection"
SOURCES = (SOURCE_UPSTREAM, SOURCE_APPCOLLECTION)


def chart_source(cfg: dict, prefix: str, default: str = SOURCE_UPSTREAM) -> str:
    """
    <prefix>_source, `default` when the lab does not set it. An unknown value is refused rather than guessed at.

    An add-on whose product the Collection carries, and whose Collection chart has been checked against the --set
    flags the add-on passes, declares default=SOURCE_APPCOLLECTION. An add-on that has not been checked keeps
    SOURCE_UPSTREAM, so a lab cannot land on an unverified chart by doing nothing.
    """
    value = str((cfg or {}).get("{}_source".format(prefix)) or default).strip().lower()
    if value not in SOURCES:
        print("ERROR: {}_source '{}' is invalid — use one of: {}".format(prefix, value, ", ".join(SOURCES)),
              file=sys.stderr)
        sys.exit(1)
    return value


def from_appcollection(cfg: dict, prefix: str, default: str = SOURCE_UPSTREAM) -> bool:
    """Whether this add-on should install from the SUSE Application Collection rather than its upstream chart."""
    return chart_source(cfg, prefix, default) == SOURCE_APPCOLLECTION


def appcollection_chart(slug: str, registry: str = "") -> str:
    """The OCI reference of the Application Collection's chart `slug` (its slug_name at https://apps.rancher.io)."""
    return "oci://{}/charts/{}".format((registry or DEFAULT_REGISTRY).rstrip("/"), slug)


def oci_charts(registry: str = "") -> str:
    """
    The Collection's chart repository as an OCI reference prefix, without a chart name. An add-on that builds its
    helm argument as "<repo>/<chart>" installs the Collection's chart by passing this as its <prefix>_rel, as long
    as the chart it asks for is named the same as the Collection's slug for it. An add-on whose chart name differs
    (traefik, whose slug is traefik-proxy) passes the full reference from prepare_appcollection_chart() instead.
    """
    return "oci://{}/charts".format((registry or DEFAULT_REGISTRY).rstrip("/"))


def prepare_appcollection_repo(hostname: str, ns: str, user: str, password: str, registry: str = "",
                               secret: str = "") -> tuple:
    """
    Make the Collection's charts installable on `hostname` in namespace `ns`: create the image pull secret and log
    Helm in. Returns (oci_charts_prefix, set_flags_string) for an add-on that builds "<repo>/<chart>" itself.
    """
    registry = (registry or DEFAULT_REGISTRY).rstrip("/")
    secret = secret or SECRET_NAME
    pull_secret(hostname, secret, registry, user, password, ns)
    helm_login(hostname, registry, user, password)
    return oci_charts(registry), set_flags(pull_secret_pairs([secret]))


def prepare_appcollection_chart(hostname: str, slug: str, ns: str, user: str, password: str,
                                registry: str = "", secret: str = "") -> tuple:
    """
    Make the Application Collection's chart `slug` installable on `hostname` in namespace `ns`: create the image
    pull secret and log Helm in to the registry.

    Returns (chart_reference, set_flags_string) for the helm command: the OCI chart to install and the
    --set global.imagePullSecrets[0]= flag that lets the deployed pods pull their images.
    """
    registry = (registry or DEFAULT_REGISTRY).rstrip("/")
    secret = secret or SECRET_NAME
    pull_secret(hostname, secret, registry, user, password, ns)
    helm_login(hostname, registry, user, password)
    return appcollection_chart(slug, registry), set_flags(pull_secret_pairs([secret]))


def pull_secret(hostname: str, name: str, server: str, user: str, password: str, ns: str) -> None:
    """Create or update the docker-registry Secret `name` in namespace `ns`. The password travels in an environment variable."""
    ssh_run(hostname, "kubectl create namespace {} --dry-run=client -o yaml | kubectl apply -f -".format(shlex.quote(ns)))
    cmd = ("kubectl create secret docker-registry {} --docker-server={} --docker-username={} "
           "--docker-password=\"$REGISTRY_PASSWORD\" --namespace {} --dry-run=client -o yaml | kubectl apply -f -").format(
        shlex.quote(name), shlex.quote(server), shlex.quote(user), shlex.quote(ns))
    ssh_run(hostname, "REGISTRY_PASSWORD={} bash -c {}".format(shlex.quote(password), shlex.quote(cmd)))


def helm_login(hostname: str, target: str, user: str, password: str) -> None:
    """helm registry login to `target`, password on stdin; exits with an actionable error when it fails."""
    login = ssh_run(hostname, "helm registry login {} --username {} --password-stdin".format(
        shlex.quote(target), shlex.quote(user)), input_text=password, check=False)
    if login.returncode != 0:
        print("ERROR: helm registry login to '{}' failed (see helm's own error above). For dp.apps.rancher.io use a SUSE "
              "Application Collection user and access token (https://apps.rancher.io); for registry.suse.com the SCC "
              "registration code of a subscription.".format(target), file=sys.stderr)
        sys.exit(1)


def set_flags(pairs: list) -> str:
    """' --set k=v' for each (key, value) pair, shell-quoted."""
    return "".join(" --set {}={}".format(shlex.quote(str(k)), shlex.quote(str(v))) for k, v in pairs)


def pull_secret_pairs(secrets: list, named: bool = False) -> list:
    """global.imagePullSecrets entries: plain names, or {name: ...} objects when `named` (the cert-manager chart's form)."""
    key = "global.imagePullSecrets[{}]" + (".name" if named else "")
    return [(key.format(i), name) for i, name in enumerate(secrets)]
