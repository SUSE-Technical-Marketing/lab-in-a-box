#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Home Assistant (open-source home automation platform)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "home_assistant"
#   home_assistant_version : Helm chart version (empty = latest, which follows Home Assistant's releases through the chart's CI)
#   home_assistant_ns      : namespace (default home-assistant)
#   home_assistant_shorthn : hostname prefix (default home-assistant)
#   home_assistant_rel     : Helm repo alias (default pajikos)
#   home_assistant_repo_url: Helm repo URL (default http://pajikos.github.io/home-assistant-helm-chart/)
#   home_assistant_storage_size  : PersistentVolumeClaim size (default 5Gi)
#   home_assistant_storage_class : StorageClass name (default: cluster default)
#   home_assistant_image_tag     : Home Assistant image tag (default 2026.7.0). The tag is pinned below stable, see setup_home_assistant().
#
# Home Assistant has no official Helm chart. This addon uses pajikos/home-assistant-helm-chart, a community chart that follows each
# Home Assistant release, and it runs the official image (ghcr.io/home-assistant/home-assistant).
#
# Prerequisite, not provided by this addon: a StorageClass. A bare RKE2 cluster has none by default, and the PVC stays Pending without one.
#
# Known requirements: the ingress needs configuration.forceInit=true and a templateConfig with an http block that lists trusted proxies.
# Otherwise every request through the ingress returns 400 Bad Request. forceInit is also needed on a re-run against an existing PVC.

__version__ = "87e323b"

PLUGIN = {
    "name": "home_assistant",
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


def _validate(v):
    v.vns("home_assistant")
    v.vver("home_assistant")


def setup_home_assistant_repo(hostname, home_assistant_rel=None, home_assistant_repo_url=None):
    """Add the Home Assistant Helm repo."""
    helm_repo_add(hostname, home_assistant_rel or "pajikos",
                  home_assistant_repo_url or "http://pajikos.github.io/home-assistant-helm-chart/")


def setup_home_assistant(hostname, clu_name, mydomain, home_assistant_rel=None, home_assistant_ns=None,
                          home_assistant_version=None, home_assistant_shorthn=None,
                          home_assistant_storage_size=None, home_assistant_storage_class=None,
                          home_assistant_image_tag=None):
    """Install Home Assistant."""
    rel = home_assistant_rel or "pajikos"
    ns = home_assistant_ns or "home-assistant"
    ver_arg = "--version {}".format(shlex.quote(home_assistant_version)) if home_assistant_version else ""
    fqdn = "{}.{}.{}".format(home_assistant_shorthn or "home-assistant", clu_name, mydomain)
    size = home_assistant_storage_size or "5Gi"
    # Pinned below the version where this genuinely broke — see the long comment right below.
    image_tag = home_assistant_image_tag or "2026.7.0"

    set_args = [
        "--set ingress.enabled=true",
        "--set ingress.hosts[0].host={}".format(shlex.quote(fqdn)),
        "--set ingress.hosts[0].paths[0].path=/",
        "--set ingress.hosts[0].paths[0].pathType=Prefix",
        "--set persistence.enabled=true",
        "--set persistence.size={}".format(shlex.quote(size)),
        "--set image.tag={}".format(shlex.quote(image_tag)),
    ]
    if home_assistant_storage_class:
        set_args.append("--set persistence.storageClass={}".format(shlex.quote(home_assistant_storage_class)))

    # The image tag is pinned below stable. Home Assistant 2026.7.5 and later no longer read trusted-proxy settings from YAML http:
    # configuration at runtime. The settings are kept in internal storage, and the UI that sets them is itself unreachable through a
    # reverse proxy. The last release that still reads the YAML block is 2026.7.0, which is the default tag. Change the tag only once
    # a release can set trusted proxies without the UI.
    values_yaml = (
        "configuration:\n"
        "  enabled: true\n"
        # The setup init container writes the template only on the first init. Without forceInit, an existing volume keeps its old
        # configuration.yaml, and changed Helm values have no effect on it.
        "  forceInit: true\n"
        "  templateConfig: |-\n"
        "    default_config:\n"
        "    frontend:\n"
        "      themes: !include_dir_merge_named themes\n"
        "    automation: !include automations.yaml\n"
        "    script: !include scripts.yaml\n"
        "    scene: !include scenes.yaml\n"
        "    http:\n"
        "      use_x_forwarded_for: true\n"
        "      trusted_proxies:\n"
        "        - 10.0.0.0/8\n"
        "        - 172.16.0.0/12\n"
        "        - 192.168.0.0/16\n"
        "        - 127.0.0.0/8\n"
    )
    ssh_run(hostname, "cat > /tmp/home-assistant-values.yaml", input_text=values_yaml)

    ssh_run(hostname,
            "helm upgrade -i home-assistant {}/home-assistant --namespace {} --create-namespace "
            "-f /tmp/home-assistant-values.yaml {} {}; rm -f /tmp/home-assistant-values.yaml".format(
                rel, ns, " ".join(set_args), ver_arg))

    print("Home Assistant installed. Namespace: {}".format(ns))
    print("Available at: http://{}".format(fqdn))


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
    cfg = definition.get("home_assistant", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_home_assistant_repo(vm_name, home_assistant_rel=cfg.get("home_assistant_rel"),
                               home_assistant_repo_url=cfg.get("home_assistant_repo_url"))
    setup_home_assistant(vm_name, clu_name, clu_cfg.get("mydomain"),
                          home_assistant_rel=cfg.get("home_assistant_rel"),
                          home_assistant_ns=cfg.get("home_assistant_ns"),
                          home_assistant_version=cfg.get("home_assistant_version"),
                          home_assistant_shorthn=cfg.get("home_assistant_shorthn"),
                          home_assistant_storage_size=cfg.get("home_assistant_storage_size"),
                          home_assistant_storage_class=cfg.get("home_assistant_storage_class"),
                          home_assistant_image_tag=cfg.get("home_assistant_image_tag"))


if __name__ == "__main__":
    main()
