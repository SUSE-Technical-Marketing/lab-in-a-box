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
