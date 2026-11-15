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

    def stop_billing_note(self):
        return ""


picked = {"a.lab": aws, "b.lab": Unsupported.__new__(Unsupported), "c.lab": Broken()}
vm_power.backends.get_backend = lambda definition, config, vm, for_existing=False: picked[vm]
state["Name"] = "running"
result = vm_power.power({"nodes": {}}, {}, "stop", ["a.lab", "b.lab", "c.lab"], wait=False)
check("aws VM stopped", result["a.lab"] == "stopping")
check("unsupported backend reported", result["b.lab"] == "unsupported")
check("failure reported with its message", result["c.lab"] == "error: aws: AccessDenied")

# ── waiting, ordering, the billing note and the DNS refresh ──────────────────
vm_power.WAIT_SECONDS = 1
backends.POWER_POLL_SECONDS = 0


class Fake:
    """A backend whose VMs reach the state asked for after one more read, as a provider's do."""

    def __init__(self, note=""):
        self.note = note
        self.acted = []
        self._left = {}

    def vm_state(self, vm):
        left = self._left.get(vm, 0)
        if left:
            self._left[vm] = left - 1
            return "stopping" if self.acted and self.acted[-1][0] == "stop" else "starting"
        return "shut off" if self.acted and self.acted[-1][0] == "stop" else "running"

    def start_vm(self, vm):
        self.acted.append(("start", vm))
        self._left[vm] = 1

    def stop_vm(self, vm):
        self.acted.append(("stop", vm))
        self._left[vm] = 1

    def stop_billing_note(self):
        return self.note

    def vm_exists(self, vm):
        return False


fake = Fake(note="Hetzner Cloud: stopping saves nothing")
vm_power.backends.get_backend = lambda definition, config, vm, for_existing=False: fake
result = vm_power.power({"nodes": {}}, {}, "stop", ["x.lab"])
check("a VM is waited for until it reports the state asked for", result["x.lab"] == "shut off")
check("...and the state in between is not reported as the answer", result["x.lab"] != "stopping")

# The order the lab is acted on: agents out first, then servers, then the VMs the lab brought up for itself.
lab = {"nodes": {"srv1": {"KUBERNETES_NODE_TYPE": "server"}, "agent1": {"KUBERNETES_NODE_TYPE": "agent"},
                 "agent2": {"KUBERNETES_NODE_TYPE": "agent"}}}
_real_infra_backends = vm_power.infra_backends
vm_power.infra_backends = lambda definition: {"lab-dns-aws": fake}
stop_groups = vm_power.lab_groups(lab, "stop")
check("stopping takes the agents first, then the servers, then the lab's own VMs",
      stop_groups == [["agent1", "agent2"], ["srv1"], ["lab-dns-aws"]])
check("starting is the reverse, so DNS and the overlay are up first",
      vm_power.lab_groups(lab, "start") == [["lab-dns-aws"], ["srv1"], ["agent2", "agent1"]])

# ── the supporting VMs this lab shares with every other lab on the account ──
# A stop must not take them down: one lab stopping its site's DNS VM would stop every other lab on that account
# resolving. A start always brings them up, since the lab's own nodes need them and no other lab loses anything.
_infra = {"lab-dns-aws": fake, "lab-overlay-aws": fake}
check("stop leaves the lab's shared supporting VMs alone by default",
      vm_power.infra_to_act_on("stop", _infra, False) == {})
check("--with-infra stops them too",
      vm_power.infra_to_act_on("stop", _infra, True) == _infra)
check("start always brings them up, with or without the flag",
      vm_power.infra_to_act_on("start", _infra, False) == _infra
      and vm_power.infra_to_act_on("start", _infra, True) == _infra)
check("status reads every one of them",
      vm_power.infra_to_act_on("status", _infra, False) == _infra)
check("a stop with no supporting VMs to leave alone is unaffected",
      vm_power.infra_to_act_on("stop", {}, False) == {})
check("a default stop therefore acts on the lab's own nodes only",
      vm_power.lab_groups(lab, "stop", vm_power.infra_to_act_on("stop", _infra, False))
      == [["agent1", "agent2"], ["srv1"]])
check("...and with --with-infra they go last, after the nodes that resolve through them",
      vm_power.lab_groups(lab, "stop", vm_power.infra_to_act_on("stop", _infra, True))[-1]
      == ["lab-dns-aws", "lab-overlay-aws"])
check("--with-infra is documented in the usage text", "--with-infra" in vm_power.__doc__)
check("...and so is why they are left running by default",
      "shared by every lab" in vm_power.__doc__ or "every lab on that cloud account" in vm_power.__doc__)

# The arrival vocabulary is backends', not a second copy of it here.
check("vm_power reads its state vocabulary from backends",
      vm_power._ARRIVED["stop"] is backends.POWERED_OFF_STATES
      and vm_power._ARRIVED["start"] is backends.RUNNING_STATES)

fake = Fake()
vm_power.backends.get_backend = lambda definition, config, vm, for_existing=False: fake
refreshed = []
vm_power._refresh_dns = lambda definition, config, started, result: refreshed.extend(started)
# A VM the lab brought up for itself is acted on through the backend that comes with it: its name is not in
# the definition, so resolving it like a node would land on the lab's default backend and report "not found"
# while the provider still charges for it.
infra = {"lab-dns-aws": fake}
def _only_nodes(definition, config, vm, for_existing=False):
    """What get_backend() really does with a name that is not in the definition: resolves it to the lab's
    default backend, which knows nothing about it."""
    if vm not in (definition.get("nodes") or {}):
        raise SystemExit("no VM '{}' on this hypervisor".format(vm))
    return fake


vm_power.backends.get_backend = _only_nodes
result = vm_power.power(lab, {}, "start", vm_power.lab_groups(lab, "start", infra), infra=infra)
check("the lab's own VM is not resolved like a node",
      not str(result.get("lab-dns-aws", "")).startswith("error:"))
vm_power.backends.get_backend = lambda definition, config, vm, for_existing=False: fake
check("every group is acted on, in order",
      [vm for _action, vm in fake.acted] == ["lab-dns-aws", "srv1", "agent2", "agent1"])
check("a started node's DNS is written again, because its address may have changed",
      refreshed == ["lab-dns-aws", "srv1", "agent2", "agent1"])
check("status neither waits nor touches anything",
      vm_power.power(lab, {}, "status", ["srv1"])["srv1"] in ("running", "shut off"))
vm_power.infra_backends = _real_infra_backends

# Each backend says what stopping does to the bill, and the lab-wide stop prints it once.
check("a hypervisor of your own says nothing about billing",
      backends.LibvirtBackend.stop_billing_note(object()) == ""
      and backends.HarvesterBackend.stop_billing_note(object()) == "")
for name, cls in sorted(backends.BACKENDS.items()):
    if name in backends.CLOUD_BACKEND_NAMES:
        note = cls.stop_billing_note(cls.__new__(cls))
        check("{}: stopping says what it does to the bill".format(name), len(note) > 40)
check("Hetzner says plainly that stopping saves nothing",
      "saves nothing" in backends.HetznerBackend.stop_billing_note(
          backends.HetznerBackend.__new__(backends.HetznerBackend)))

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
      and ("StopInstance", "--InstanceId", "i-a", "--StoppedMode", "StopCharging") in seen)
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
check("ovh start/stop (a SHUTOFF instance starts, and stopping shelves it)",
      ("POST", "/cloud/project/proj/instance/o1/start") in seen
      and ("POST", "/cloud/project/proj/instance/o1/shelve") in seen)

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


# ── main(): argument handling, default VM list, exit code ──────────────────────────────────────────────────────────
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from unittest import mock  # noqa: E402


def run_main(*args, result=None):
    """vm_power.main() with argv `args` and power() returning `result`: (exit code, stdout, power() calls)."""
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["vm_power.py"] + list(args)), \
            mock.patch.object(vm_power.primary, "load_definition", return_value={"nodes": {"a": {}, "b": {}}}), \
            mock.patch.object(vm_power.primary, "load_config", return_value={}), \
            mock.patch.object(vm_power, "power", return_value=result or {}) as pw, \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            vm_power.main()
            code = None
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), pw.call_args_list


code, out, calls = run_main("--version")
check("main: --version prints the version and exits 0", code == 0 and vm_power.__version__ in out and not calls)
check("main: a missing action exits 2", run_main("lab.json")[0] == 2)
check("main: an unknown action exits 2", run_main("lab.json", "reboot")[0] == 2)
code, out, calls = run_main("lab.json", "status", result={"a": "running", "b": "shut off"})
check("main: no VM names means every node of the lab", calls[0][0][2:] == ("status", ["a", "b"]))
check("main: prints the result as JSON and exits 0",
      code == 0 and json.loads(out) == {"a": "running", "b": "shut off"})
code, out, calls = run_main("lab.json", "stop", "b", result={"b": "error: boom"})
check("main: named VMs only", calls[0][0][2:] == ("stop", ["b"]))
check("main: any error result exits 1", code == 1)

# The no-charge modes: OVHcloud shelves instead of stopping, Alibaba stops in economical mode.
ovh2, seen2 = make(backends.OVHcloudBackend, "_api",
                   lambda a: [{"id": "o1", "name": "smlm", "status": "SHUTOFF"}] if a[0] == "GET" else None,
                   service_name="proj")
ovh2.stop_vm("smlm")
check("ovh stop shelves the instance",
      ("POST", "/cloud/project/proj/instance/o1/shelve") in seen2)
ovh3, seen3 = make(backends.OVHcloudBackend, "_api",
                   lambda a: [{"id": "o1", "name": "smlm", "status": "SHELVED_OFFLOADED"}] if a[0] == "GET"
                   else None, service_name="proj")
ovh3.start_vm("smlm")
check("a shelved instance is unshelved, not started",
      ("POST", "/cloud/project/proj/instance/o1/unshelve") in seen3)
ali2, seen4 = make(backends.AlibabaBackend, "_aliyun",
                   lambda a: {"Instances": {"Instance": [{"InstanceId": "i-a", "Status": "Running"}]}}
                   if a[0] == "DescribeInstances" else None, security_group_id="sg-1")
ali2.stop_vm("smlm")
check("alibaba stops in economical mode",
      ("StopInstance", "--InstanceId", "i-a", "--StoppedMode", "StopCharging") in seen4)

# ── through the lab-builder: the helper runs vm_power.py, the admin action maps onto it ──────────
import json as _json                                                         # noqa: E402
import os as _os                                                             # noqa: E402
import subprocess as _subprocess                                             # noqa: E402
import tempfile as _tempfile                                                 # noqa: E402

sys.path.insert(0, str(_REPO / "webui" / "lib"))
import admin  # noqa: E402

_tmp = Path(_tempfile.mkdtemp())
_labs = _tmp / "labs"
_labs.mkdir()
(_labs / "mylab.json").write_text(_json.dumps({"common": {}, "nodes": {"a.lab": {}}}))
_fake_power = _tmp / "fake-vm-power"
_fake_power.write_text("#!/bin/bash\n"
                       "printf '%s\\n' \"$@\" > " + str(_tmp / "argv") + "\n"
                       "echo 'Hetzner Cloud: stopping saves nothing' >&2\n"
                       "echo '{\"a.lab\": \"off\"}'\n")
_fake_power.chmod(0o755)
_helper_env = dict(_os.environ, LABBUILDER_LABS_DIR=str(_labs), LABBUILDER_JOBS_DIR=str(_tmp / "jobs"),
                   LABBUILDER_VM_POWER=str(_fake_power), LABBUILDER_EXAMPLES_DIR=str(_REPO / "examples"),
                   PYTHONPATH=str(_REPO / "libs"))


def _helper(args):
    # This check itself runs under the host's python3 (3.6 on some of them); the helper is a 3.11 script.
    r = _subprocess.run(["python3.11", str(_REPO / "scripts" / "lab_builder_helper.py")] + args,
                        input=b"{}", stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, env=_helper_env)
    try:
        return r, _json.loads(r.stdout.decode() or "{}")
    except ValueError:
        return r, {}


r, out = _helper(["lab-power", "stop", "mylab.json"])
check("lab-power answers with the states vm_power.py printed",
      r.returncode == 0 and out.get("result") == {"a.lab": "off"} and out.get("action") == "stop")
check("...and with what the backend said stopping does to the bill",
      "saves nothing" in (out.get("note") or ""))
check("it ran vm_power.py on the saved lab, with the action last",
      (_tmp / "argv").read_text().split() == [str(_labs / "mylab.json"), "stop"])
r, out = _helper(["lab-power", "status", "--example", "cluster.json.template", "--cloud-account", "aws-tmm"])
check("lab-power takes an example lab and a cloud account",
      r.returncode == 0 and (_tmp / "argv").read_text().split()
      == ["--cloud-account", "aws-tmm", "--example", "cluster.json.template", "status"])
for args, expected in ((["lab-power", "reboot", "mylab.json"], "lab-power takes one of"),
                       (["lab-power", "stop", "nope.json"], "no saved lab"),
                       (["lab-power", "stop", "--example", "nope.json"], "no example lab"),
                       (["lab-power", "stop", "mylab.json", "--example", "cluster.json.template"], "not both"),
                       (["lab-power", "stop", "--example"], "needs a name")):
    r, out = _helper(args)
    check("helper refuses lab-power {}: '{}'".format(" ".join(args[1:]), expected),
          r.returncode != 0 and expected in (out.get("error") or ""))

check("the power action needs the login, like the other admin actions", "power" in admin.ACTIONS)
with mock.patch.object(admin, "helper", lambda args, stdin=None: {"args": args}):
    check("admin power maps a saved lab onto the helper's arguments",
          admin.dispatch("power", "POST", {}, {"filename": "mylab.json", "action": "stop"})["args"]
          == ["lab-power", "stop", "mylab.json"])
    check("...and an example with its cloud account",
          admin.dispatch("power", "POST", {}, {"example": "cluster.json.template", "action": "start",
                                               "cloud_account": "aws-tmm"})["args"]
          == ["lab-power", "start", "--example", "cluster.json.template", "--cloud-account", "aws-tmm"])

app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
css = (_REPO / "webui" / "htdocs" / "style.css").read_text()
check("the saved labs offer status, start and stop", '["Status", "Start", "Stop"].forEach' in app_js)
check("an example offers status and stop, since starting it is Launch",
      '["Status", "Stop"].forEach' in app_js)
check("they go through the admin power action", 'adminCall("power", { ...which, action })' in app_js)
check("the answer includes what the backend said about the bill", "r.note" in app_js)
check("a long action says it is running rather than looking stuck", '"Stopping…"' in app_js)
check("the answer has somewhere to go", ".power-answer" in css and "power-answer" in app_js)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all vm_power checks passed")
