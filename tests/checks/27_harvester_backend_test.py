#!/usr/bin/env python3
# Unit tests for libs/backends.py's HarvesterBackend — every kubectl/
# virtctl call is mocked (no real Harvester cluster available anywhere in
# this environment). Asserts CRD apiVersions, config_method enforcement,
# and MAC/image wiring — not real cluster behavior; see HarvesterBackend's
# own docstring for exactly what remains unverified. Run from
# 27_harvester_backend.sh, in its own container — see tests/run_tests.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _cp(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


backend = backends.HarvesterBackend("/etc/lab-harvester/kubeconfig", namespace="default")


# ── resolve(): HARVESTER_NETWORK is optional, backward compatible ───────────
resolved = backends.HarvesterBackend.resolve({}, "vm1", {"HARVESTER_KUBECONFIG": "/kc"}, False)
check("resolve() defaults network_attachment to None (pod network, unchanged behavior)",
      resolved.network_attachment is None)
resolved = backends.HarvesterBackend.resolve(
    {}, "vm1", {"HARVESTER_KUBECONFIG": "/kc", "HARVESTER_NETWORK": "default/mgmt-vlan"}, False)
check("resolve() picks up HARVESTER_NETWORK from lab_creation.cfg",
      resolved.network_attachment == "default/mgmt-vlan")


# ── _image_name(): DNS-1123-safe derivation ───────────────────────────────────
check("_image_name strips extension and lowercases",
      backends.HarvesterBackend._image_name("SLE-Micro-6.1.qcow2") == "sle-micro-6-1")
check("_image_name collapses invalid characters to a single hyphen",
      backends.HarvesterBackend._image_name("weird__name!!.iso") == "weird-name")


# ── _parse_k8s_cpu / _parse_k8s_memory ────────────────────────────────────────
check("_parse_k8s_cpu parses millicores", backends._parse_k8s_cpu("500m") == 500)
check("_parse_k8s_cpu parses whole cores", backends._parse_k8s_cpu("4") == 4000)
check("_parse_k8s_memory parses Mi", backends._parse_k8s_memory("512Mi") == 512 * 1024)
check("_parse_k8s_memory parses Gi", backends._parse_k8s_memory("2Gi") == 2 * 1024 * 1024)


# ── copy_vm_image(): config_method enforcement + image-existence check ───────
died = False
try:
    backend.copy_vm_image("img.qcow2", "vm1", "40", config_method="")
except SystemExit:
    died = True
check("copy_vm_image dies for any config_method other than cloud-init", died)

with mock.patch.object(subprocess, "run", return_value=_cp(1)):
    died = False
    try:
        backend.copy_vm_image("img.qcow2", "vm1", "40", config_method="cloud-init")
    except SystemExit:
        died = True
    check("copy_vm_image dies when the VirtualMachineImage doesn't exist", died)

with mock.patch.object(subprocess, "run", return_value=_cp(0)) as m:
    backend.copy_vm_image("img.qcow2", "vm1", "40", config_method="cloud-init")
    check("copy_vm_image checks 'kubectl get virtualmachineimage <name>' when it exists",
          m.call_args[0][0][:4] == ["kubectl", "--kubeconfig", "/etc/lab-harvester/kubeconfig", "-n"]
          and "virtualmachineimage" in m.call_args[0][0])


# ── push_provisioning_files(): applies a cloud-init Secret ────────────────────
with tempfile.TemporaryDirectory() as tmp:
    ci_dir = Path(tmp) / "cloud-init"
    ci_dir.mkdir()
    (ci_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    (ci_dir / "vm1_network-config").write_text("version: 2\n")
    backend2 = backends.HarvesterBackend("/kc", lab_setup_path=tmp)

    died = False
    try:
        backend2.push_provisioning_files("vm1", config_method="")
    except SystemExit:
        died = True
    check("push_provisioning_files dies for any config_method other than cloud-init", died)

    with mock.patch.object(subprocess, "run", return_value=_cp(0)) as m:
        backend2.push_provisioning_files("vm1", config_method="cloud-init")
        applied = json.loads(m.call_args[1]["input"])
        check("push_provisioning_files applies a v1 Secret", applied["apiVersion"] == "v1" and applied["kind"] == "Secret")
        check("push_provisioning_files names the Secret '<vm>-cloudinit'",
              applied["metadata"]["name"] == "vm1-cloudinit")
        check("push_provisioning_files embeds the real user-data content",
              "hostname: vm1" in applied["stringData"]["userdata"])

    died = False
    try:
        backend2.push_provisioning_files("vm-nofile", config_method="cloud-init")
    except SystemExit:
        died = True
    check("push_provisioning_files dies when the user-data file doesn't exist", died)


# ── create_vm(): CRD apiVersions, imageId annotation, MAC wiring ─────────────
def _run_side_effect(cmd, **kwargs):
    if "virtualmachineimage" in cmd:
        return _cp(0, stdout=json.dumps({"status": {"storageClassName": "longhorn-image-abcde"}}))
    return _cp(0)


with mock.patch.object(subprocess, "run", side_effect=_run_side_effect) as m:
    backend.create_vm("vm1", "2", "4096", "40", "unused-network-string",
                       config_method="cloud-init", iso_image="sle-micro.qcow2", mymac="aa:bb:cc:dd:ee:ff")
    # NOTE: this container's python3 is 3.6 — index call objects as plain
    # tuples (c[0]=positional args, c[1]=kwargs), never .args/.kwargs; see
    # feedback_mock_call_py36 in project memory.
    apply_call = next(c for c in m.call_args_list if "apply" in c[0][0])
    manifest = json.loads(apply_call[1]["input"])
    check("create_vm applies a kubevirt.io/v1 VirtualMachine", manifest["apiVersion"] == "kubevirt.io/v1"
          and manifest["kind"] == "VirtualMachine")
    dv = manifest["spec"]["dataVolumeTemplates"][0]
    check("create_vm's DataVolume carries the harvesterhci.io/imageId annotation",
          dv["metadata"]["annotations"]["harvesterhci.io/imageId"] == "default/sle-micro")
    check("create_vm's DataVolume uses the image's own storageClassName",
          dv["spec"]["pvc"]["storageClassName"] == "longhorn-image-abcde")
    # Regression test: source.pvc (clone
    # from the image's own backing PVC) failed outright against a real
    # Harvester v1.7.1 cluster — no such PVC exists at all there (this
    # version backs images via Longhorn's own BackingImage feature instead,
    # which the per-image storageClassName above already accounts for).
    check("create_vm's DataVolume uses source.blank (storageClassName alone provisions from the image)",
          dv["spec"]["source"] == {"blank": {}})
    domain = manifest["spec"]["template"]["spec"]["domain"]
    check("create_vm sets both resources.requests.memory and resources.limits.memory "
          "(KubeVirt rejects requests alone)",
          domain["resources"]["requests"]["memory"] == "4096Mi"
          and domain["resources"]["limits"]["memory"] == "4096Mi")
    iface = domain["devices"]["interfaces"][0]
    check("create_vm sets the interface's macAddress when mymac is given",
          iface.get("macAddress") == "aa:bb:cc:dd:ee:ff")
    check("create_vm defaults to pod network when HARVESTER_NETWORK is unset (backward compatible)",
          manifest["spec"]["template"]["spec"]["networks"] == [{"name": "default", "pod": {}}])
    cloudinit_vol = next(v for v in manifest["spec"]["template"]["spec"]["volumes"] if v["name"] == "cloudinitdisk")
    check("create_vm's cloudinitdisk volume sets secretRef for userdata",
          cloudinit_vol["cloudInitNoCloud"]["secretRef"] == {"name": "vm1-cloudinit"})
    check("create_vm's cloudinitdisk volume ALSO sets networkDataSecretRef, since KubeVirt reads "
          "networkdata from this separate field, not from "
          "secretRef's own \"networkdata\" key: without it, cloud-init never sees the custom "
          "network-config at all and silently falls back to DHCP for every interface",
          cloudinit_vol["cloudInitNoCloud"].get("networkDataSecretRef") == {"name": "vm1-cloudinit"})

died = False
with mock.patch.object(subprocess, "run", return_value=_cp(1)):
    try:
        backend.create_vm("vm1", "2", "4096", "40", "x", config_method="cloud-init", iso_image="sle-micro.qcow2")
    except SystemExit:
        died = True
check("create_vm dies when the VirtualMachineImage doesn't exist", died)

with mock.patch.object(subprocess, "run", return_value=_cp(0, stdout=json.dumps({"status": {}}))):
    died = False
    try:
        backend.create_vm("vm1", "2", "4096", "40", "x", config_method="cloud-init", iso_image="sle-micro.qcow2")
    except SystemExit:
        died = True
check("create_vm dies when the image has no status.storageClassName yet", died)

died = False
try:
    backend.create_vm("vm1", "2", "4096", "40", "x", config_method="virt_customize")
except SystemExit:
    died = True
check("create_vm dies for any config_method other than cloud-init", died)


# ── create_vm(): Multus network attachment (HARVESTER_NETWORK) ──────────────
net_backend = backends.HarvesterBackend("/etc/lab-harvester/kubeconfig", namespace="default",
                                         network_attachment="default/mgmt-vlan")


def _run_side_effect_with_nad(cmd, **kwargs):
    if "virtualmachineimage" in cmd:
        return _cp(0, stdout=json.dumps({"status": {"storageClassName": "longhorn-image-abcde"}}))
    if "network-attachment-definitions.k8s.cni.cncf.io" in cmd:
        return _cp(0)
    return _cp(0)


with mock.patch.object(subprocess, "run", side_effect=_run_side_effect_with_nad) as m:
    net_backend.create_vm("vm1", "2", "4096", "40", "unused-network-string",
                           config_method="cloud-init", iso_image="sle-micro.qcow2")
    nad_check = next(c for c in m.call_args_list if "network-attachment-definitions.k8s.cni.cncf.io" in c[0][0])
    nad_args = nad_check[0][0]
    check("create_vm checks the NetworkAttachmentDefinition exists, in the namespace from HARVESTER_NETWORK",
          "mgmt-vlan" in nad_args and nad_args[nad_args.index("-n") + 1] == "default")
    apply_call = next(c for c in m.call_args_list if "apply" in c[0][0])
    manifest = json.loads(apply_call[1]["input"])
    check("create_vm uses a multus network when HARVESTER_NETWORK is set",
          manifest["spec"]["template"]["spec"]["networks"] ==
          [{"name": "default", "multus": {"networkName": "default/mgmt-vlan"}}])
    check("create_vm's interface binding stays 'bridge' regardless of pod vs. multus",
          manifest["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"][0].get("bridge") == {})

# A bare name (no "namespace/" prefix) resolves against HARVESTER_NAMESPACE.
bare_backend = backends.HarvesterBackend("/etc/lab-harvester/kubeconfig", namespace="myns",
                                          network_attachment="mgmt-vlan")
with mock.patch.object(subprocess, "run", side_effect=_run_side_effect_with_nad) as m:
    bare_backend.create_vm("vm1", "2", "4096", "40", "x", config_method="cloud-init", iso_image="sle-micro.qcow2")
    nad_check = next(c for c in m.call_args_list if "network-attachment-definitions.k8s.cni.cncf.io" in c[0][0])
    ns_index = nad_check[0][0].index("-n")
    check("a bare HARVESTER_NETWORK name (no namespace prefix) checks against this backend's own namespace",
          nad_check[0][0][ns_index + 1] == "myns")

# NetworkAttachmentDefinition missing → dies clearly, doesn't silently fall
# back to pod networking.
died = False
with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **kw: (
        _cp(0, stdout=json.dumps({"status": {"storageClassName": "longhorn-image-abcde"}}))
        if "virtualmachineimage" in cmd else _cp(1))):
    try:
        net_backend.create_vm("vm1", "2", "4096", "40", "x", config_method="cloud-init", iso_image="sle-micro.qcow2")
    except SystemExit:
        died = True
check("create_vm dies clearly when the configured NetworkAttachmentDefinition doesn't exist", died)


# ── delete_vm(): graceful virtctl stop before kubectl delete ─────────────────
with mock.patch.object(subprocess, "run", return_value=_cp(0)) as m:
    backend.delete_vm("vm1")
    calls = [c[0][0] for c in m.call_args_list]
    check("delete_vm calls virtctl stop before kubectl delete",
          calls[0][0] == "virtctl" and calls[0][1:3] == ["--kubeconfig", "/etc/lab-harvester/kubeconfig"]
          and "stop" in calls[0] and calls[1][0] == "kubectl" and "delete" in calls[1])


# ── vm_exists() / list_used_macs() ────────────────────────────────────────────
with mock.patch.object(subprocess, "run", return_value=_cp(0)):
    check("vm_exists returns True on rc=0", backend.vm_exists("vm1") is True)
with mock.patch.object(subprocess, "run", return_value=_cp(1)):
    check("vm_exists returns False on rc!=0", backend.vm_exists("vm1") is False)

vmi_json = json.dumps({"items": [
    {"metadata": {"name": "vm1"}, "spec": {"domain": {"devices": {"interfaces": [
        {"name": "default", "macAddress": "AA:BB:CC:DD:EE:FF"}]}}}},
]})
with mock.patch.object(subprocess, "run", return_value=_cp(0, stdout=vmi_json)):
    names, macs = backend.list_used_macs()
    check("list_used_macs lists VMI names", names == ["vm1"])
    check("list_used_macs lowercases MAC addresses", macs == {"vm1": "aa:bb:cc:dd:ee:ff"})


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all harvester_backend checks passed")
