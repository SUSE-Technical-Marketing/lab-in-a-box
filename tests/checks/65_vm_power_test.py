#!/usr/bin/env python3
# vm_power.py — see 65_vm_power.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import backends  # noqa: E402

_loader = SourceFileLoader("vm_power", str(_REPO / "scripts" / "vm_power.py"))
_spec = importlib.util.spec_from_loader("vm_power", _loader)
vm_power = importlib.util.module_from_spec(_spec)
_loader.exec_module(vm_power)

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class Died(Exception):
    pass


def _die(msg):
    raise SystemExit(msg)


backends.die = _die

# ── AWSBackend power ops against a fake `aws` ──────────────────────────────
aws = backends.AWSBackend.__new__(backends.AWSBackend)
aws._instance_id_by_vm = {}
state = {"Name": "stopped"}
calls = []


def fake_aws(*args, **kwargs):
    calls.append(args)
    if args[:2] == ("ec2", "describe-instances"):
        if "Values=ghost" in " ".join(args):
            return {"Reservations": []}
        return {"Reservations": [{"Instances": [{"InstanceId": "i-123", "State": dict(state)}]}]}
    if args[:2] == ("ec2", "start-instances"):
        state["Name"] = "pending"
    if args[:2] == ("ec2", "stop-instances"):
        state["Name"] = "stopping"
    return {}


aws._aws = fake_aws
check("stopped instance reported", aws.vm_state("smlm.lab") == "stopped")
aws.start_vm("smlm.lab")
check("start-instances called with the instance id", ("ec2", "start-instances", "--instance-ids", "i-123") in calls)
check("state after start", aws.vm_state("smlm.lab") == "pending")
aws.stop_vm("smlm.lab")
check("stop-instances called", ("ec2", "stop-instances", "--instance-ids", "i-123") in calls)
check("missing instance is 'not found'", aws.vm_state("ghost") == "not found")
try:
    aws.start_vm("ghost")
    check("starting a missing instance dies", False)
except SystemExit:
    pass

# ── base class: unsupported ─────────────────────────────────────────────────
try:
    backends.VMBackend.vm_state(object(), "x")
    check("base vm_state raises NotImplementedError", False)
except NotImplementedError:
    pass

# ── vm_power.power(): per-VM results ────────────────────────────────────────
class Unsupported(backends.VMBackend):
    """A backend that implements none of the power operations (inherits the base)."""


class Broken:
    def stop_vm(self, vm):
        raise SystemExit("aws: AccessDenied")

    def vm_state(self, vm):
        return "running"


picked = {"a.lab": aws, "b.lab": Unsupported.__new__(Unsupported), "c.lab": Broken()}
vm_power.backends.get_backend = lambda definition, config, vm, for_existing=False: picked[vm]
state["Name"] = "running"
result = vm_power.power({"nodes": {}}, {}, "stop", ["a.lab", "b.lab", "c.lab"])
check("aws VM stopped", result["a.lab"] == "stopping")
check("unsupported backend reported", result["b.lab"] == "unsupported")
check("failure reported with its message", result["c.lab"] == "error: aws: AccessDenied")

# ── every other cloud backend: power + ports through its own request helper ─
check("parse_open_ports", backends.parse_open_ports(["443", "69/UDP", 8080]) ==
      [(443, "tcp"), (69, "udp"), (8080, "tcp")])
check("gce names are valid", backends._gce_name("SMLM_1.lab") == "lab-smlm-1-lab")


def make(cls, helper, reply, **attrs):
    obj = cls.__new__(cls)
    for k, v in attrs.items():
        setattr(obj, k, v)
    seen = []

    def fake(*args, **kwargs):
        seen.append(args)
        return reply(args)
    setattr(obj, helper, fake)
    return obj, seen


# GCP (gcloud CLI)
gcp, seen = make(backends.GCPBackend, "_gcloud",
                 lambda a: [{"name": "smlm", "status": "TERMINATED"}] if a[:3] == ("compute", "instances", "list")
                 else ([] if a[:3] == ("compute", "firewall-rules", "list") else None),
                 zone="z1", network=None)
check("gcp state", gcp.vm_state("smlm") == "TERMINATED")
gcp.start_vm("smlm")
gcp.stop_vm("smlm")
check("gcp start/stop", ("compute", "instances", "start", "smlm", "--zone", "z1") in seen
      and ("compute", "instances", "stop", "smlm", "--zone", "z1") in seen)
gcp.open_vm_ports("smlm.lab", ["443", "69/udp"])
create = [a for a in seen if a[:3] == ("compute", "firewall-rules", "create")]
check("gcp firewall rule created", create and "tcp:443,udp:69" in create[0] and "lab-smlm" in create[0])
check("gcp tag added to the VM", ("compute", "instances", "add-tags", "smlm.lab", "--zone", "z1",
                                  "--tags", "lab-smlm") in seen)

# Hetzner (REST)
hz, seen = make(backends.HetznerBackend, "_api",
                lambda a: {"servers": [{"id": 7, "status": "off"}]} if a[0] == "GET" else None)
check("hetzner state", hz.vm_state("smlm") == "off")
hz.start_vm("smlm")
hz.stop_vm("smlm")
check("hetzner poweron/shutdown", ("POST", "/servers/7/actions/poweron") in seen
      and ("POST", "/servers/7/actions/shutdown") in seen)
changes_before = [a for a in seen if a[0] != "GET"]
hz.open_vm_ports("smlm", ["443"])
check("hetzner opens nothing (open by default)", [a for a in seen if a[0] != "GET"] == changes_before)

# Alibaba (aliyun CLI)
ali, seen = make(backends.AlibabaBackend, "_aliyun",
                 lambda a: {"Instances": {"Instance": [{"InstanceId": "i-a", "Status": "Stopped"}]}}
                 if a[0] == "DescribeInstances" else None, security_group_id="sg-1")
check("alibaba state", ali.vm_state("smlm") == "Stopped")
ali.start_vm("smlm")
ali.stop_vm("smlm")
check("alibaba start/stop", ("StartInstance", "--InstanceId", "i-a") in seen
      and ("StopInstance", "--InstanceId", "i-a") in seen)
ali.open_vm_ports("smlm", ["443"])
check("alibaba security-group rule", ("AuthorizeSecurityGroup", "--SecurityGroupId", "sg-1", "--IpProtocol",
                                      "tcp", "--PortRange", "443/443", "--SourceCidrIp", "0.0.0.0/0") in seen)

# Scaleway (REST)
sw, seen = make(backends.ScalewayBackend, "_api",
                lambda a: {"servers": [{"id": "s1", "state": "stopped"}]} if a[0] == "GET" else None)
check("scaleway state", sw.vm_state("smlm") == "stopped")
sw.start_vm("smlm")
sw.stop_vm("smlm")
check("scaleway poweron/poweroff", ("POST", "/servers/s1/action", {"action": "poweron"}) in seen
      and ("POST", "/servers/s1/action", {"action": "poweroff"}) in seen)

# UpCloud (REST)
uc, seen = make(backends.UpCloudBackend, "_api",
                lambda a: {"servers": {"server": [{"uuid": "u1", "title": "smlm", "state": "stopped"}]}}
                if a[0] == "GET" else None)
check("upcloud state", uc.vm_state("smlm") == "stopped")
uc.start_vm("smlm")
uc.stop_vm("smlm")
check("upcloud start/stop", ("POST", "/server/u1/start") in seen
      and any(a[:2] == ("POST", "/server/u1/stop") and a[2]["stop_server"]["stop_type"] == "soft" for a in seen))

# OVHcloud (REST)
ovh, seen = make(backends.OVHcloudBackend, "_api",
                 lambda a: [{"id": "o1", "name": "smlm", "status": "SHUTOFF"}] if a[0] == "GET" else None,
                 service_name="proj")
check("ovh state", ovh.vm_state("smlm") == "SHUTOFF")
ovh.start_vm("smlm")
ovh.stop_vm("smlm")
check("ovh start/stop", ("POST", "/cloud/project/proj/instance/o1/start") in seen
      and ("POST", "/cloud/project/proj/instance/o1/stop") in seen)

# Exoscale (exo CLI)
exo, seen = make(backends.ExoscaleBackend, "_exo",
                 lambda a: [{"name": "smlm", "state": "stopped"}] if a[:3] == ("compute", "instance", "list")
                 else ([] if a[:3] == ("compute", "security-group", "list") else None))
check("exoscale state", exo.vm_state("smlm") == "stopped")
exo.start_vm("smlm")
exo.stop_vm("smlm")
check("exoscale start/stop", ("compute", "instance", "start", "smlm") in seen
      and ("compute", "instance", "stop", "smlm", "--force") in seen)
exo.open_vm_ports("smlm.lab", ["443"])
check("exoscale security group created", ("compute", "security-group", "create", "lab-smlm") in seen)
check("exoscale ingress rule", any(a[:5] == ("compute", "security-group", "rule", "add", "lab-smlm")
                                   and "443" in a for a in seen))
check("exoscale group attached", ("compute", "instance", "security-group", "add", "smlm.lab", "lab-smlm") in seen)

# AWS reuses its security-group logic
opened = []
aws.ensure_ports_open = lambda ports: opened.append(list(ports))
aws.open_vm_ports("smlm.lab", ["443"])
check("aws open_vm_ports reuses ensure_ports_open", opened == [["443"]])

# libvirt: nothing to open, no error
backends.VMBackend.open_vm_ports(backends.LibvirtBackend.__new__(backends.LibvirtBackend), "x", ["443"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all vm_power checks passed")
