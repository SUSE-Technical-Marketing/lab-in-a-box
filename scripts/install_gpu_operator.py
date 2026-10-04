#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install the NVIDIA GPU Operator
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "gpu_operator" — configurable keys:
#   gpu_operator_version           : [OPTIONAL] Helm chart version (empty = latest, e.g. "v26.7.0")
#   gpu_operator_ns                : [OPTIONAL] namespace (default: gpu-operator)
#   gpu_operator_rel                : [OPTIONAL] Helm repo alias (default: nvidia)
#   gpu_operator_repo_url           : [OPTIONAL] Helm repo URL (default: https://helm.ngc.nvidia.com/nvidia)
#   gpu_operator_driver_enabled    : [OPTIONAL] "true"/"false" (default: true) — let the operator install
#                                     its own NVIDIA driver container; set "false" if the node's guest
#                                     image already ships a matching driver (a pre-baked image) instead
#   gpu_operator_time_slicing_replicas : [OPTIONAL] integer — share ONE physical GPU across this many
#                                     pods via the operator's time-slicing feature (e.g. "4"). Useful for
#                                     a lab that only has one GPU passed through but wants several AI
#                                     addons (ollama, open_webui's embedding step, milvus GPU search,
#                                     etc.) to schedule onto it concurrently. Omit for one-pod-per-GPU.
#
# This addon assumes the target node ALREADY has an NVIDIA GPU made visible to it (PCI passthrough via
# libvirt/VFIO, configured outside this project's scope — same "operator pre-configures it, this addon
# only consumes it" stance as HarvesterBackend's Multus network support). It does not attach or
# configure any PCI device itself.
#
# gpu_operator_driver_enabled=false (pre-baked driver) and the time-slicing ConfigMap shape are not
# validated on SLE Micro or openSUSE guests. NVIDIA's precompiled driver containers historically favor
# Ubuntu and RHEL-family kernels. Check NVIDIA's supported-OS matrix (docs.nvidia.com/datacenter/
# cloud-native/gpu-operator) for the guest image before relying on driver_enabled=true (the default)
# on a SUSE-family node.

__version__ = "3d35d1a"

PLUGIN = {
    "name": "gpu_operator",
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
    v.vns("gpu_operator")
    v.vver("gpu_operator")
    v.vbool("gpu_operator", "gpu_operator_driver_enabled")


def setup_gpu_operator_repo(hostname, gpu_operator_rel=None, gpu_operator_repo_url=None):
    """Add the NVIDIA Helm repo."""
    helm_repo_add(hostname, gpu_operator_rel or "nvidia", gpu_operator_repo_url or "https://helm.ngc.nvidia.com/nvidia")


def setup_gpu_operator(hostname, gpu_operator_ns=None, gpu_operator_version=None,
                        gpu_operator_rel=None, gpu_operator_driver_enabled=None,
                        gpu_operator_time_slicing_replicas=None):
    """Install the NVIDIA GPU Operator, optionally with time-slicing enabled."""
    rel = gpu_operator_rel or "nvidia"
    ns = gpu_operator_ns or "gpu-operator"
    ver_arg = "--version {}".format(shlex.quote(gpu_operator_version)) if gpu_operator_version else ""
    driver_enabled = "false" if str(gpu_operator_driver_enabled) == "false" else "true"

    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(shlex.quote(ns)), check=False)

    time_slicing_arg = ""
    if gpu_operator_time_slicing_replicas:
        replicas = str(int(gpu_operator_time_slicing_replicas))
        cm_yaml = (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: time-slicing-config\n"
            "  namespace: {}\n"
            "data:\n"
            "  any: |-\n"
            "    version: v1\n"
            "    flags:\n"
            "      migStrategy: none\n"
            "    sharing:\n"
            "      timeSlicing:\n"
            "        resources:\n"
            "        - name: nvidia.com/gpu\n"
            "          replicas: {}\n"
        ).format(ns, replicas)
        ssh_run(hostname, "kubectl apply -n {} -f -".format(shlex.quote(ns)), input_text=cm_yaml)
        time_slicing_arg = (
            " --set devicePlugin.config.name=time-slicing-config"
            " --set devicePlugin.config.default=any"
        )

    ssh_run(hostname,
            "helm upgrade -i gpu-operator {}/gpu-operator --namespace {} --create-namespace "
            "--set driver.enabled={} "
            "{}{}".format(rel, ns, driver_enabled, ver_arg, time_slicing_arg))

    print("NVIDIA GPU Operator installed. Namespace: {}".format(ns))
    print("Driver container: {}".format("enabled (operator installs its own)" if driver_enabled == "true"
                                         else "disabled (assumes a pre-installed driver)"))
    if gpu_operator_time_slicing_replicas:
        print("GPU time-slicing enabled: {} replicas per physical GPU".format(gpu_operator_time_slicing_replicas))
    print("Verify with: kubectl get pods -n {} ; kubectl describe node <gpu-node> | grep nvidia.com/gpu".format(ns))


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
    cfg = definition.get("gpu_operator", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_gpu_operator_repo(vm_name, gpu_operator_rel=cfg.get("gpu_operator_rel"),
                             gpu_operator_repo_url=cfg.get("gpu_operator_repo_url"))
    setup_gpu_operator(vm_name,
                        gpu_operator_ns=cfg.get("gpu_operator_ns"),
                        gpu_operator_version=cfg.get("gpu_operator_version"),
                        gpu_operator_rel=cfg.get("gpu_operator_rel"),
                        gpu_operator_driver_enabled=cfg.get("gpu_operator_driver_enabled"),
                        gpu_operator_time_slicing_replicas=cfg.get("gpu_operator_time_slicing_replicas"))


if __name__ == "__main__":
    main()
