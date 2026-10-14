#!/usr/bin/env python3
# VMBackend keep/reboot — see 82_backend_keep_reboot.sh.
import subprocess
import sys
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


def bare(cls, **attrs):
    """An instance of backend `cls` without running its constructor, with attributes `attrs`."""
    obj = cls.__new__(cls)
    obj.__dict__.update(attrs)
    return obj


def done(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


# ── _reachable_for_keep: DNS must give myip, then SSH as root must answer ──────────────────────────────────────────
with mock.patch.object(backends.socket, "gethostbyname", return_value="10.0.0.7"), \
        mock.patch.object(backends.subprocess, "run", return_value=done()) as run:
    check("_reachable_for_keep: keeps a VM that resolves to its IP and answers on SSH",
          backends._reachable_for_keep("vm1", "10.0.0.7") is True and run.call_args[0][0][-2:] == ["root@vm1", "exit 0"])
    check("_reachable_for_keep: recreates a VM whose name resolves to another IP",
          backends._reachable_for_keep("vm1", "10.0.0.8") is False)
with mock.patch.object(backends.socket, "gethostbyname", side_effect=OSError("NXDOMAIN")):
    check("_reachable_for_keep: recreates a VM whose name does not resolve", backends._reachable_for_keep("vm1", "10.0.0.7") is False)
with mock.patch.object(backends.socket, "gethostbyname", return_value="10.0.0.7"), \
        mock.patch.object(backends.subprocess, "run", return_value=done(rc=255)):
    check("_reachable_for_keep: recreates a VM that does not answer on SSH", backends._reachable_for_keep("vm1", "10.0.0.7") is False)

# ── vm_is_reusable: each backend's own state check, then the shared one ──────────────────────────────────────────
# backend -> (lookup method, object when running, object in another state)
CLOUDS = {
    backends.HetznerBackend: ("_find_server", {"id": 1, "status": "running"}, {"id": 1, "status": "off"}),
    backends.AWSBackend: ("_find_instance", {"InstanceId": "i-1", "State": {"Name": "running"}},
                          {"InstanceId": "i-1", "State": {"Name": "stopped"}}),
    backends.GCPBackend: ("_find_instance", {"status": "RUNNING"}, {"status": "TERMINATED"}),
    backends.AlibabaBackend: ("_find_instance", {"InstanceId": "i-1", "Status": "Running"},
                              {"InstanceId": "i-1", "Status": "Stopped"}),
    backends.ScalewayBackend: ("_find_server", {"id": "s1", "state": "running"}, {"id": "s1", "state": "stopped"}),
    backends.UpCloudBackend: ("_find_server", {"uuid": "u1", "state": "started"}, {"uuid": "u1", "state": "stopped"}),
    backends.OVHcloudBackend: ("_find_instance", {"id": "o1", "status": "ACTIVE"}, {"id": "o1", "status": "SHUTOFF"}),
    backends.ExoscaleBackend: ("_find_instance", {"state": "running"}, {"state": "stopped"}),
}
for cls, (lookup, running, other) in CLOUDS.items():
    b = bare(cls)
    name = cls.__name__
    with mock.patch.object(backends, "_reachable_for_keep", return_value=True) as reach:
        with mock.patch.object(cls, lookup, return_value=running):
            check(name + ".vm_is_reusable: a running VM goes on to the DNS/SSH check",
                  b.vm_is_reusable("vm1", "", "10.0.0.7") is True and reach.call_args[0] == ("vm1", "10.0.0.7"))
        reach.reset_mock()
        with mock.patch.object(cls, lookup, return_value=other):
            check(name + ".vm_is_reusable: a VM in another state is recreated",
                  b.vm_is_reusable("vm1", "", "10.0.0.7") is False and not reach.called)
        with mock.patch.object(cls, lookup, return_value=None):
            check(name + ".vm_is_reusable: a missing VM is recreated", b.vm_is_reusable("vm1", "", "10.0.0.7") is False)

hv = bare(backends.HarvesterBackend)
with mock.patch.object(backends, "_reachable_for_keep", return_value=True), \
        mock.patch.object(backends.HarvesterBackend, "list_used_macs", return_value=([], {"vm1": "52:54:00:00:00:01"})):
    with mock.patch.object(backends.HarvesterBackend, "_kubectl", return_value=done("Running")):
        check("HarvesterBackend.vm_is_reusable: Running with the right MAC is kept",
              hv.vm_is_reusable("vm1", "52:54:00:00:00:01", "10.0.0.7") is True)
        check("HarvesterBackend.vm_is_reusable: a MAC mismatch is recreated",
              hv.vm_is_reusable("vm1", "52:54:00:00:00:02", "10.0.0.7") is False)
    with mock.patch.object(backends.HarvesterBackend, "_kubectl", return_value=done("Stopped")):
        check("HarvesterBackend.vm_is_reusable: a stopped VM is recreated", hv.vm_is_reusable("vm1", "", "10.0.0.7") is False)

# ── reboot_vm: the provider's reboot call for the VM; a missing VM dies ───────────────────────────────────────────
REBOOTS = [
    (bare(backends.HetznerBackend), "_find_server", {"id": 7}, "_api", ("POST", "/servers/7/actions/reboot")),
    (bare(backends.AWSBackend), "_find_instance", {"InstanceId": "i-7"}, "_aws",
     ("ec2", "reboot-instances", "--instance-ids", "i-7")),
    (bare(backends.AlibabaBackend), "_find_instance", {"InstanceId": "i-7"}, "_aliyun", ("RebootInstance", "--InstanceId", "i-7")),
    (bare(backends.ScalewayBackend), "_find_server", {"id": "s7"}, "_api", ("POST", "/servers/s7/action", {"action": "reboot"})),
    (bare(backends.UpCloudBackend), "_find_server", {"uuid": "u7"}, "_api",
     ("POST", "/server/u7/restart", {"restart_server": {}})),
    (bare(backends.OVHcloudBackend, service_name="proj"), "_find_instance", {"id": "o7"}, "_api",
     ("POST", "/cloud/project/proj/instance/o7/reboot", {"type": "hard"})),
    (bare(backends.ExoscaleBackend), "vm_exists", True, "_exo", ("compute", "instance", "reboot", "vm1", "-f")),
]
for b, lookup, found, call, expected in REBOOTS:
    name = type(b).__name__
    with mock.patch.object(type(b), lookup, return_value=found), mock.patch.object(type(b), call) as api:
        b.reboot_vm("vm1")
        check(name + ".reboot_vm: calls the provider's reboot for the VM", api.call_args[0] == expected)
    with mock.patch.object(type(b), lookup, return_value=None), mock.patch.object(type(b), call), \
            mock.patch.object(backends, "die", side_effect=SystemExit("missing")) as died:
        try:
            b.reboot_vm("vm1")
        except SystemExit:
            pass
        check(name + ".reboot_vm: dies for a missing VM", died.called)
    with mock.patch.object(type(b), lookup, return_value=found), \
            mock.patch.object(type(b), call, side_effect=RuntimeError("API said no")), \
            mock.patch.object(backends, "die", side_effect=SystemExit("api")) as died:
        try:
            b.reboot_vm("vm1")
        except SystemExit:
            pass
        check(name + ".reboot_vm: dies with the provider's error", died.called and "API said no" in died.call_args[0][0])
gcp = bare(backends.GCPBackend, zone="europe-west1-b")
with mock.patch.object(backends.GCPBackend, "_gcloud") as gc:
    gcp.reboot_vm("vm1")
check("GCPBackend.reboot_vm: resets the instance in its zone",
      gc.call_args[0] == ("compute", "instances", "reset", "vm1", "--zone", "europe-west1-b"))
with mock.patch.object(backends.HarvesterBackend, "_virtctl", return_value=done()) as vc:
    hv.reboot_vm("vm1")
check("HarvesterBackend.reboot_vm: virtctl restart", vc.call_args[0] == ("restart", "vm1"))

# ── AWS secondary private IPs ─────────────────────────────────────────────────────────────────────────────────────
INSTANCE = {"InstanceId": "i-1", "NetworkInterfaces": [
    {"NetworkInterfaceId": "eni-b", "Attachment": {"DeviceIndex": 1}, "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.9"}]},
    {"NetworkInterfaceId": "eni-a", "Attachment": {"DeviceIndex": 0}, "PrivateIpAddresses": [
        {"PrivateIpAddress": "10.0.0.7", "Primary": True}, {"PrivateIpAddress": "10.0.0.50"}]}]}
aws = bare(backends.AWSBackend, subnet_id="subnet-1", security_group_id="sg-1")
with mock.patch.object(backends.AWSBackend, "_find_instance", return_value=INSTANCE):
    check("AWSBackend.secondary_ips: the primary interface's non-primary addresses", aws.secondary_ips("vm1") == ["10.0.0.50"])
    with mock.patch.object(backends.AWSBackend, "_aws") as api:
        check("AWSBackend.assign_secondary_ip: an address already registered is returned unchanged",
              aws.assign_secondary_ip("vm1", "10.0.0.50") == "10.0.0.50" and aws.assign_secondary_ip("vm1") == "10.0.0.50"
              and not api.called)
    with mock.patch.object(backends.AWSBackend, "_aws", return_value={}) as api:
        check("AWSBackend.assign_secondary_ip: registers a given address on the primary interface",
              aws.assign_secondary_ip("vm1", "10.0.0.60") == "10.0.0.60" and api.call_args[0] == (
                  "ec2", "assign-private-ip-addresses", "--network-interface-id", "eni-a", "--private-ip-addresses", "10.0.0.60"))
with mock.patch.object(backends.AWSBackend, "_find_instance", return_value={"NetworkInterfaces": [
        {"NetworkInterfaceId": "eni-a", "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.0.7", "Primary": True}]}]}), \
        mock.patch.object(backends.AWSBackend, "_aws",
                          return_value={"AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.0.0.61"}]}) as api:
    check("AWSBackend.assign_secondary_ip: else lets AWS pick one", aws.assign_secondary_ip("vm1") == "10.0.0.61"
          and api.call_args[0][-2:] == ("--secondary-private-ip-address-count", "1"))
with mock.patch.object(backends.AWSBackend, "get_subnet_cidr", return_value="10.0.0.0/28"), \
        mock.patch.object(backends.AWSBackend, "_aws", return_value={"NetworkInterfaces": [
            {"PrivateIpAddresses": [{"PrivateIpAddress": "10.0.0.14"}, {"PrivateIpAddress": "10.0.0.13"}]}]}):
    check("AWSBackend.free_private_ip: the highest unused address of the subnet", aws.free_private_ip() == "10.0.0.12")
with mock.patch.object(backends.AWSBackend, "_aws", return_value={"SecurityGroups": [{"IpPermissions": []}]}) as api:
    aws.allow_internal_traffic()
check("AWSBackend.allow_internal_traffic: adds the group-to-itself rule when missing",
      api.call_args[0] == ("ec2", "authorize-security-group-ingress", "--group-id", "sg-1", "--protocol", "-1",
                           "--source-group", "sg-1"))
with mock.patch.object(backends.AWSBackend, "_aws", return_value={"SecurityGroups": [{"IpPermissions": [
        {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": "sg-1"}]}]}]}) as api:
    aws.allow_internal_traffic()
check("AWSBackend.allow_internal_traffic: no change when the rule exists", api.call_count == 1)

# ── LibvirtBackend: MACs of every domain, free resources of the host ──────────────────────────────────────────────
lv = bare(backends.LibvirtBackend, remote_host="hv1", vm_img_loc="/var/lib/libvirt/images")
DOMIF = {"vm1": " Interface  Type   Source  Model  MAC\n---\n vnet0  bridge  br0  virtio  52:54:00:AA:BB:01\n",
         "vm2": " Interface  Type   Source  Model  MAC\n---\n"}
with mock.patch.object(backends.LibvirtBackend, "_virsh",
                       side_effect=lambda *a, **k: done("vm1\nvm2\n\n" if a[0] == "list" else DOMIF[a[1]])):
    check("LibvirtBackend.list_used_macs: every domain, with the MAC of its first interface",
          lv.list_used_macs() == (["vm1", "vm2"], {"vm1": "52:54:00:aa:bb:01"}))
ANSWERS = {"nproc": "16", "virsh --connect qemu:///system list --name": "vm1\nvm2\n",
           "virsh --connect qemu:///system vcpucount --current vm1": "4",
           "virsh --connect qemu:///system vcpucount --current vm2": "2",
           "free -m | awk '/^Mem:/{print $7}'": "30000", "df -BM --output=avail /var/lib/libvirt/images | tail -1": " 500000M"}
with mock.patch.object(backends._lc, "ssh_output", side_effect=lambda host, cmd: ANSWERS[cmd]):
    check("LibvirtBackend.host_resources: free vCPUs, memory and disk of the host", lv.host_resources() == (10, 30000, 500000))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all backend keep/reboot checks passed")
