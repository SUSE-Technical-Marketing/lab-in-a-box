#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUSE Observability (full metrics/traces/topology stack)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "suse_observability" — configurable keys:
#   suse_observability_license : [MANDATORY] SUSE Observability registration code, from SUSE
#                                Customer Center's Subscription tab (valid for the life of your
#                                Rancher Prime subscription) — https://scc.suse.com
#   suse_observability_ns      : [OPTIONAL] namespace (default: suse-observability)
#   suse_observability_version : [OPTIONAL] Helm chart version (empty = latest)
#   suse_observability_rel     : [OPTIONAL] Helm repo alias (default: suse-observability)
#   suse_observability_repo_url: [OPTIONAL] Helm repo URL
#                                (default: https://charts.rancher.com/server-charts/prime/suse-observability)
#   suse_observability_shorthn : [OPTIONAL] hostname prefix (default: observability)
#   suse_observability_profile : [OPTIONAL] sizing.profile — one of trial | 10-nonha | 20-nonha |
#                                50-nonha | 100-nonha | 150-ha | 250-ha | 500-ha | 4000-ha;
#                                trial is the lab-sized, non-HA profile (default: trial)
#
# SUSE Observability (StackState-based: metrics/traces/topology in one stack, not just a Grafana
# dashboard) is a real, distinct SUSE product from the "smlm" addon. Documentation is at
# documentation.suse.com/cloudnative/suse-observability.
# Needs Helm >= 3.13.1 on the target cluster (this project's setup_helm() always installs a current
# Helm 3, so this should already be satisfied).

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "suse_observability",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402

_VALID_PROFILES = ("trial", "10-nonha", "20-nonha", "50-nonha", "100-nonha", "150-ha", "250-ha",
                    "500-ha", "4000-ha")


def _validate(v):
    v.vreq("suse_observability", "suse_observability_license")
    v.vns("suse_observability")
    v.vver("suse_observability")


def setup_suse_observability_repo(hostname, suse_observability_rel=None, suse_observability_repo_url=None):
    """Add the SUSE Observability Helm repo."""
    helm_repo_add(hostname, suse_observability_rel or "suse-observability",
                  suse_observability_repo_url or "https://charts.rancher.com/server-charts/prime/suse-observability")


def setup_suse_observability(hostname, clu_name, mydomain, suse_observability_license=None,
                              suse_observability_rel=None, suse_observability_ns=None,
                              suse_observability_version=None, suse_observability_shorthn=None,
                              suse_observability_profile=None):
    """Install SUSE Observability."""
    if not suse_observability_license:
        print("ERROR: suse_observability_license is mandatory. Get one from SUSE Customer Center's "
              "Subscription tab (https://scc.suse.com).", file=sys.stderr)
        sys.exit(1)

    profile = suse_observability_profile or "trial"
    if profile not in _VALID_PROFILES:
        print("ERROR: suse_observability_profile '{}' is invalid — must be one of: {}".format(
              profile, ", ".join(_VALID_PROFILES)), file=sys.stderr)
        sys.exit(1)

    rel = suse_observability_rel or "suse-observability"
    ns = suse_observability_ns or "suse-observability"
    ver_arg = "--version {}".format(shlex.quote(suse_observability_version)) if suse_observability_version else ""
    fqdn = "{}.{}.{}".format(suse_observability_shorthn or "observability", clu_name, mydomain)

    ssh_run(hostname,
            "helm upgrade -i suse-observability {}/suse-observability --namespace {} --create-namespace "
            "--set global.suseObservability.license={} "
            "--set sizing.profile={} "
            "--set ingress.enabled=true "
            "--set ingress.host={} "
            "{}".format(rel, ns, shlex.quote(suse_observability_license), shlex.quote(profile),
                        shlex.quote(fqdn), ver_arg))

    print("SUSE Observability installed. Namespace: {}. Sizing profile: {}".format(ns, profile))
    print("UI available at: http://{}".format(fqdn))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("suse_observability", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_suse_observability_repo(vm_name, suse_observability_rel=cfg.get("suse_observability_rel"),
                                   suse_observability_repo_url=cfg.get("suse_observability_repo_url"))
    setup_suse_observability(vm_name, clu_name, clu_cfg.get("mydomain"),
                              suse_observability_license=cfg.get("suse_observability_license"),
                              suse_observability_rel=cfg.get("suse_observability_rel"),
                              suse_observability_ns=cfg.get("suse_observability_ns"),
                              suse_observability_version=cfg.get("suse_observability_version"),
                              suse_observability_shorthn=cfg.get("suse_observability_shorthn"),
                              suse_observability_profile=cfg.get("suse_observability_profile"))


if __name__ == "__main__":
    main()
