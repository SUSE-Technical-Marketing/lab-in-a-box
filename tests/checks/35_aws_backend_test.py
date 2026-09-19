#!/usr/bin/env python3
# Unit tests for libs/backends.py's AWSBackend — every `aws` CLI call
# (subprocess.run) is mocked (no real AWS account available anywhere in this
# environment, and no `aws` CLI needs to even be installed to run these).
# Asserts command construction, MAC/image handling, config_method
# enforcement, and instance-type sizing — not real API behavior; see
# AWSBackend's own docstring for exactly what remains unverified. Run from
# 35_aws_backend.sh, in its own container — see tests/run_tests.sh.
import json
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
    import subprocess
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


# ── resolve(): AWS_REGION + one of the two credential shapes are mandatory ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve({}, "vm1", {}, False)
    except SystemExit:
        pass
check("resolve() dies without AWS_REGION", any("AWS_REGION" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve({}, "vm1", {"AWS_REGION": "eu-central-1"}, False)
    except SystemExit:
        pass
check("resolve() dies with a region but no credentials at all",
      any("AWS_PROFILE" in m for m in died))

resolved = backends.AWSBackend.resolve(
    {}, "vm1", {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "lab"}, False)
check("resolve() accepts AWS_PROFILE alone", resolved.profile == "lab")
check("resolve() picks up the region", resolved.region == "eu-central-1")

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "secret"},
    False)
check("resolve() accepts an access/secret key pair without a profile", resolved.access_key == "AKIA...")

# ── resolve(): a temporary/STS ("ASIA...") access key requires AWS_SESSION_TOKEN ──
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve(
            {}, "vm1",
            {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "ASIA...", "AWS_SECRET_ACCESS_KEY": "secret"},
            False)
    except SystemExit:
        pass
check("resolve() dies on an ASIA-prefixed key with no AWS_SESSION_TOKEN",
      any("AWS_SESSION_TOKEN" in m for m in died))

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "ASIA...", "AWS_SECRET_ACCESS_KEY": "secret",
     "AWS_SESSION_TOKEN": "tok123"},
    False)
check("resolve() accepts an ASIA-prefixed key when AWS_SESSION_TOKEN is also set",
      resolved.session_token == "tok123")

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "lab", "AWS_SUBNET_ID": "subnet-1",
     "AWS_SECURITY_GROUP_ID": "sg-1", "AWS_KEY_NAME": "labkey"},
    False)
check("resolve() picks up optional networking/key fields",
      (resolved.subnet_id, resolved.security_group_id, resolved.key_name) == ("subnet-1", "sg-1", "labkey"))

# ── resolve(): AWS_PROFILE wins outright over leftover raw keys ────────────
# Confirmed live 2026-09-13: resolve_cloud_account()'s merge only overrides
# same-named keys, so a cloud_account that sets AWS_PROFILE (to switch to
# SSO) still had /etc/lab_creation.cfg's own unrelated, stale
# AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN come through in the same effective
# config — and the `aws` CLI's own credential chain checks those explicit
# env vars BEFORE AWS_PROFILE, so a stale/expired key silently defeated a
# freshly-configured, working SSO profile (RequestExpired even though the
# profile worked fine when tested directly). resolve() must ignore any
# access/secret/session-token fields entirely once a profile is set.
resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "sso-profile",
     "AWS_ACCESS_KEY_ID": "ASIA-STALE", "AWS_SECRET_ACCESS_KEY": "stale-secret",
     "AWS_SESSION_TOKEN": "stale-token"},
    False)
check("resolve(): AWS_PROFILE set -> access_key is ignored entirely, not just unused",
      resolved.profile == "sso-profile" and resolved.access_key is None)
check("resolve(): AWS_PROFILE set -> secret_key is ignored entirely",
      resolved.secret_key is None)
check("resolve(): AWS_PROFILE set -> session_token is ignored entirely",
      resolved.session_token is None)


backend = backends.AWSBackend("eu-central-1", profile="lab")


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("ami-123", "vm1", 40, config_method="")
    except SystemExit:
        pass
check("copy_vm_image() dies on config_method != cloud-init", any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty ISO_IMAGE", any("AMI" in m for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("ami-0123456789abcdef0", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real AMI ID with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes user-data for create_vm() to read ──
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.AWSBackend("eu-central-1", profile="lab", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on EC2 ─────
check("list_used_macs() returns empty (EC2 has no MAC concept this backend uses)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): dropped 2026-09-09 — no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to AWS)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_instance_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_instance_type() picks the smallest sufficient SKU (2 vCPU / 2048 MiB)",
      backend._pick_instance_type(2, 2048, "vm1") == "t3.medium")
check("_pick_instance_type() steps up when memory needs more than cores would suggest",
      backend._pick_instance_type(2, 16384, "vm1") == "t3.xlarge")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_instance_type(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_instance_type() dies clearly when nothing in the table is big enough",
      any("INSTANCE_TYPES" in m for m in died))


# ── vm_exists() / _find_instance(): describe-instances with the Name-tag filter ─
_describe_none = _cp(0, stdout=json.dumps({"Reservations": []}))
_describe_one = _cp(0, stdout=json.dumps(
    {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]}))

with mock.patch.object(backends.subprocess, "run", return_value=_describe_one) as m_run:
    check("vm_exists() returns True when describe-instances lists a match", backend.vm_exists("vm1") is True)
    called_args = m_run.call_args[0][0]
    check("vm_exists() filters by the Name tag", any("tag:Name,Values=vm1" in a for a in called_args))

with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("vm_exists() returns False when describe-instances lists nothing", backend.vm_exists("vm1") is False)


# ── get_ip(): prefers PublicIpAddress, falls back to PrivateIpAddress, None if unassigned ──
_with_public = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
    {"InstanceId": "i-1", "PublicIpAddress": "203.0.113.10", "PrivateIpAddress": "10.0.0.5"}]}]}))
_private_only = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
    {"InstanceId": "i-1", "PrivateIpAddress": "10.0.0.5"}]}]}))
_no_ip_yet = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}))

with mock.patch.object(backends.subprocess, "run", return_value=_with_public):
    check("get_ip() prefers the public IP when both are assigned", backend.get_ip("vm1") == "203.0.113.10")
with mock.patch.object(backends.subprocess, "run", return_value=_private_only):
    check("get_ip() falls back to the private IP when no public one is assigned",
          backend.get_ip("vm1") == "10.0.0.5")
with mock.patch.object(backends.subprocess, "run", return_value=_no_ip_yet):
    check("get_ip() returns None while the instance has no IP yet (still Pending)",
          backend.get_ip("vm1") is None)
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("get_ip() returns None when the instance doesn't exist at all", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none) as m_run:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls describe-instances (no terminate) when nothing exists",
          m_run.call_count == 1)


# ── create_vm(): real command construction, incl. the root-device lookup ──
b3 = backends.AWSBackend("eu-central-1", profile="lab")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"
calls = []


def _fake_run(args, **kwargs):
    calls.append(args)
    if "describe-images" in args:
        return _cp(0, stdout=json.dumps({"Images": [{"RootDeviceName": "/dev/sda1"}]}))
    # Internet-Gateway/route management (added 2026-09-13) runs for every
    # create_vm() call that has a subnet configured — every test below that
    # sets subnet_id needs these mocked as "already fine" (an IGW already
    # attached, the default route already present) so it stays a pure no-op
    # and doesn't interfere with what these particular tests are actually
    # asserting. The dedicated Internet-Gateway test section further down
    # uses its own separate fixture to exercise the real create/attach/
    # add-route paths.
    if "describe-subnets" in args:
        return _cp(0, stdout=json.dumps({"Subnets": [{"VpcId": "vpc-1"}]}))
    if "describe-internet-gateways" in args:
        return _cp(0, stdout=json.dumps({"InternetGateways": [{"InternetGatewayId": "igw-1"}]}))
    if "describe-route-tables" in args:
        return _cp(0, stdout=json.dumps({"RouteTables": [
            {"RouteTableId": "rtb-1", "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}]}]}))
    if "describe-instances" in args:
        # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
        # PublicIpAddress here so the poll succeeds on its first check, not a 180s timeout.
        return _cp(0, stdout=json.dumps(
            {"Reservations": [{"Instances": [{"InstanceId": "i-new", "PublicIpAddress": "203.0.113.10"}]}]}))
    if "run-instances" in args:
        return _cp(0, stdout=json.dumps({"Instances": [{"InstanceId": "i-new"}]}))
    return _cp(0, stdout="")


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init",
                                iso_image="ami-0123456789abcdef0")

check("create_vm() returns the real IP once the instance is confirmed running (2026-09-09 "
      "contract — see VMBackend.create_vm()'s own docstring)", returned_ip == "203.0.113.10")

run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm() looks up the AMI's real root device name before building block-device-mappings",
      any("describe-images" in c for c in calls))
check("create_vm() picks a real instance type", "t3.medium" in run_instances_call)
check("create_vm() uses --count, NOT the old --min-count/--max-count pair (real bug found "
      "live-testing 2026-09-09 — this project's own installed aws CLI rejects the old pair "
      "outright, see TODO)",
      "--count" in run_instances_call and "1" in run_instances_call
      and "--min-count" not in run_instances_call and "--max-count" not in run_instances_call)
check("create_vm() uses the real root device name from describe-images, not a hardcoded default",
      json.loads(run_instances_call[run_instances_call.index("--block-device-mappings") + 1])[0]["DeviceName"]
      == "/dev/sda1")
check("create_vm() sends the stashed user-data", "#cloud-config\n" in run_instances_call)
check("create_vm() tags the instance with a real Name tag",
      any("Key=Name,Value=vm1" in a for a in run_instances_call))
check("create_vm() omits subnet/security-group/key-name flags when none were configured",
      "--subnet-id" not in run_instances_call and "--key-name" not in run_instances_call)
check("create_vm() omits --associate-public-ip-address too when no subnet is configured "
      "(it's only meaningful alongside an explicit subnet)",
      "--associate-public-ip-address" not in run_instances_call)

b4 = backends.AWSBackend("eu-central-1", profile="lab", subnet_id="subnet-1",
                          security_group_id="sg-1", key_name="labkey")
b4._user_data_by_vm["vm1"] = ""
calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    b4.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="ami-0123456789abcdef0")
run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm() includes subnet/security-group/key-name when configured",
      "subnet-1" in run_instances_call and "sg-1" in run_instances_call and "labkey" in run_instances_call)
check("create_vm() explicitly requests a public IP whenever a subnet is configured (real bug "
      "found live-testing 2026-09-09: a subnet with MapPublicIpOnLaunch=false, a common "
      "real-world default, otherwise leaves the instance unreachable — see TODO)",
      "--associate-public-ip-address" in run_instances_call)


# ── create_vm(): auto-raises vm_dsk_gb to the AMI's own minimum root volume size ──
# Confirmed live 2026-09-13: ensure_cloud_dns_vm() always requests an 8 GiB root
# volume regardless of which AMI a given lab actually configures — a real SLES
# 15 SP7 BYOS AMI's own snapshot needs >= 10 GiB, so run-instances rejected it
# with InvalidBlockDeviceMapping. Fixed once in create_vm() itself (the one
# place that already knows the AMI's real minimum), not in every caller.
def _fake_run_with_bdm(min_gb):
    def _run(args, **kwargs):
        if "describe-images" in args:
            return _cp(0, stdout=json.dumps({"Images": [{
                "RootDeviceName": "/dev/sda1",
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": min_gb}}],
            }]}))
        return _fake_run(args, **kwargs)
    return _run


b6 = backends.AWSBackend("eu-central-1", profile="lab")
b6._user_data_by_vm["vm1"] = ""
calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_with_bdm(10)):
    b6.create_vm("vm1", 1, 512, 8, None, config_method="cloud-init", iso_image="ami-sles15sp7")
run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm(): a requested vm_dsk_gb smaller than the AMI's own minimum is raised to that minimum",
      json.loads(run_instances_call[run_instances_call.index("--block-device-mappings") + 1])[0]["Ebs"]["VolumeSize"]
      == 10)

calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_with_bdm(10)):
    b6.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-sles15sp7")
run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm(): a requested vm_dsk_gb already BIGGER than the AMI's minimum is left unchanged, "
      "never shrunk",
      json.loads(run_instances_call[run_instances_call.index("--block-device-mappings") + 1])[0]["Ebs"]["VolumeSize"]
      == 40)


# ── create_vm(): security-group access management ───────────────────────────
# Confirmed live 2026-09-13: a freshly-created AWS node got a real IP/DNS
# entry but check_ssh_conn() then exhausted its retry limit — the security
# group had no inbound rule at all for traffic from outside AWS's own
# network (only a self-referencing member-to-member rule). create_vm() must
# always ensure SSH from this automation node's own public IP, plus any
# extra "aws_open_ports" the lab JSON configures (e.g. SMLM's own 443/4505/
# 4506) — added at the user's own explicit request after diagnosing that bug.
def _fake_run_sg(existing_rules, calls_out):
    def _run(args, **kwargs):
        calls_out.append(args)
        if "describe-images" in args:
            return _cp(0, stdout=json.dumps({"Images": [{"RootDeviceName": "/dev/sda1"}]}))
        if "describe-security-groups" in args:
            return _cp(0, stdout=json.dumps({"SecurityGroups": [{"IpPermissions": existing_rules}]}))
        if "authorize-security-group-ingress" in args:
            return _cp(0, stdout="")
        if "describe-instances" in args:
            return _cp(0, stdout=json.dumps(
                {"Reservations": [{"Instances": [{"InstanceId": "i-new", "PublicIpAddress": "203.0.113.10"}]}]}))
        if "run-instances" in args:
            return _cp(0, stdout=json.dumps({"Instances": [{"InstanceId": "i-new"}]}))
        return _cp(0, stdout="")
    return _run


b_sg = backends.AWSBackend("eu-central-1", profile="lab", security_group_id="sg-1")
b_sg._user_data_by_vm["vm1"] = ""
b_sg._cached_public_ip = "198.51.100.7"  # avoid a real network call in this test
sg_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_sg([], sg_calls)):
    b_sg.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x",
                    open_ports=["443", "4505", "4506"])
authorize_calls = [c for c in sg_calls if "authorize-security-group-ingress" in c]
check("create_vm(): opens SSH (22) from this automation node's own public IP",
      any("22" in c and "198.51.100.7/32" in c for c in authorize_calls))
check("create_vm(): opens every extra port from aws_open_ports, to 0.0.0.0/0",
      all(any(p in c and "0.0.0.0/0" in c for c in authorize_calls) for p in ("443", "4505", "4506")))
check("create_vm(): authorizes exactly 4 rules (SSH + 3 open_ports), no more",
      len(authorize_calls) == 4)

# Already-open rules are never re-authorized (idempotent).
sg_calls = []
existing = [
    {"FromPort": 22, "ToPort": 22, "IpProtocol": "tcp", "IpRanges": [{"CidrIp": "198.51.100.7/32"}]},
    {"FromPort": 443, "ToPort": 443, "IpProtocol": "tcp", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
]
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_sg(existing, sg_calls)):
    b_sg.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x",
                    open_ports=["443"])
check("create_vm(): never re-authorizes a rule that already exists (idempotent)",
      not any("authorize-security-group-ingress" in c for c in sg_calls))

# No security group configured at all -> no-op, no describe/authorize calls.
b_nosg = backends.AWSBackend("eu-central-1", profile="lab")
b_nosg._user_data_by_vm["vm1"] = ""
sg_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_sg([], sg_calls)):
    b_nosg.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x",
                      open_ports=["443"])
check("create_vm(): no security_group_id configured -> never touches security groups at all",
      not any("security-group" in c for call in sg_calls for c in call))

# A port with an explicit non-tcp protocol ("69/udp") is parsed correctly.
sg_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_sg([], sg_calls)):
    b_sg.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x",
                    open_ports=["69/udp"])
authorize_calls = [c for c in sg_calls if "authorize-security-group-ingress" in c]
check("create_vm(): aws_open_ports entries support an explicit '<port>/<protocol>' suffix",
      any("69" in c and "udp" in c and "0.0.0.0/0" in c for c in authorize_calls))


# ── ensure_ports_open(): VMBackend.ensure_ports_open() override, standalone (no create_vm) ──
# Added 2026-09-18 for overlay.py's OVERLAY_HUB_ACCOUNT — opens a port for an
# ALREADY-EXISTING host (e.g. the WireGuard overlay hub named via
# OVERLAY_HUB_HOST) without creating/touching any specific VM.
b_ports = backends.AWSBackend("eu-central-1", profile="lab", security_group_id="sg-1")
b_ports._cached_public_ip = "198.51.100.7"
ports_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_sg([], ports_calls)):
    b_ports.ensure_ports_open(["51820/udp"])
authorize_calls = [c for c in ports_calls if "authorize-security-group-ingress" in c]
check("ensure_ports_open(): opens the requested port with no VM name/create_vm() call involved",
      any("51820" in c and "udp" in c and "0.0.0.0/0" in c for c in authorize_calls))
check("ensure_ports_open(): still opens SSH from this automation node's own IP, same as create_vm()",
      any("22" in c and "198.51.100.7/32" in c for c in authorize_calls))

check("VMBackend.ensure_ports_open() base implementation is a documented no-op for every "
      "other backend (LibvirtBackend has no override)",
      backends.LibvirtBackend.ensure_ports_open is backends.VMBackend.ensure_ports_open)


# ── get_private_ip() / get_subnet_cidr() / disable_source_dest_check() ──────
# Added 2026-09-18 for libs/overlay.py's site-gateway model — a site
# gateway needs its own private IP (so OTHER nodes in the same subnet can
# route through it) and its subnet's real CIDR (to advertise to the
# overlay hub), and needs source/dest-check disabled to actually forward
# traffic that isn't addressed to itself.
def _fake_run_site(calls_out):
    def _run(args, **kwargs):
        calls_out.append(args)
        if "describe-instances" in args:
            return _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
                {"InstanceId": "i-gw1", "PrivateIpAddress": "172.31.5.10",
                 "PublicIpAddress": "203.0.113.20"}]}]}))
        if "describe-subnets" in args:
            return _cp(0, stdout=json.dumps({"Subnets": [{"CidrBlock": "172.31.0.0/20"}]}))
        if "modify-instance-attribute" in args:
            return _cp(0, stdout="")
        return _cp(0, stdout="")
    return _run


b_site = backends.AWSBackend("eu-central-1", profile="lab", subnet_id="subnet-1")
site_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_site(site_calls)):
    priv_ip = b_site.get_private_ip("gw1")
    subnet_cidr = b_site.get_subnet_cidr()
    b_site.disable_source_dest_check("gw1")
check("get_private_ip(): returns the real PrivateIpAddress from describe-instances",
      priv_ip == "172.31.5.10")
check("get_subnet_cidr(): returns the real CidrBlock for self.subnet_id via describe-subnets",
      subnet_cidr == "172.31.0.0/20")
check("disable_source_dest_check(): calls modify-instance-attribute with --no-source-dest-check "
      "on the REAL resolved instance id, not the vm_name",
      any("modify-instance-attribute" in c and "i-gw1" in c and "--no-source-dest-check" in c
          for c in site_calls))

# No subnet configured at all -> get_subnet_cidr() is a clean None, no API call attempted.
b_nosubnet = backends.AWSBackend("eu-central-1", profile="lab")
nosubnet_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_site(nosubnet_calls)):
    result = b_nosubnet.get_subnet_cidr()
check("get_subnet_cidr(): no subnet_id configured -> None, no describe-subnets call made",
      result is None and not any("describe-subnets" in c for c in nosubnet_calls))

# A vm_name that doesn't resolve to any live instance -> best-effort no-op, never raises/dies.
def _fake_run_missing(args, **kwargs):
    if "describe-instances" in args:
        return _cp(0, stdout=json.dumps({"Reservations": []}))
    return _cp(0, stdout="")


b_missing = backends.AWSBackend("eu-central-1", profile="lab")
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_missing):
    b_missing.disable_source_dest_check("does-not-exist")  # must not raise
    result2 = b_missing.get_private_ip("does-not-exist")
check("get_private_ip(): a vm_name with no live instance -> None, not an exception",
      result2 is None)

check("VMBackend base defaults: get_private_ip/get_subnet_cidr/disable_source_dest_check are "
      "documented no-ops for every other backend",
      backends.LibvirtBackend.get_private_ip is backends.VMBackend.get_private_ip
      and backends.LibvirtBackend.get_subnet_cidr is backends.VMBackend.get_subnet_cidr
      and backends.LibvirtBackend.disable_source_dest_check is backends.VMBackend.disable_source_dest_check)


# ── _own_public_ip(): fetched once via checkip.amazonaws.com, then cached ──
class _FakeUrlopenResponse:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


b_ip = backends.AWSBackend("eu-central-1", profile="lab")
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeUrlopenResponse("203.0.113.55\n")) as m_urlopen:
    ip1 = b_ip._own_public_ip()
    ip2 = b_ip._own_public_ip()
check("_own_public_ip(): returns the real (mocked) response, stripped of whitespace",
      ip1 == "203.0.113.55")
check("_own_public_ip(): only fetches once — the second call is served from cache",
      m_urlopen.call_count == 1)

died = []
b_ip_fail = backends.AWSBackend("eu-central-1", profile="lab")
with mock.patch.object(backends.urllib.request, "urlopen", side_effect=OSError("network unreachable")), \
     mock.patch.object(backends, "die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)):
    try:
        b_ip_fail._own_public_ip()
    except SystemExit:
        pass
check("_own_public_ip(): dies clearly if it can't reach the public-IP-lookup service at all",
      any("public IP" in m for m in died))


# ── cloud_instance_type: explicit override bypasses _pick_instance_type() entirely ──
# added 2026-09-10 per explicit user request that no provider's sizing catalog be a hardcoded
# ceiling — see _parse_sku_table()'s own docstring and README's Compute backends table.
b5 = backends.AWSBackend("eu-central-1", profile="lab")
b5._user_data_by_vm["vm1"] = ""
calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    # cpu/mem here would normally pick "t3.medium" — cloud_instance_type must win regardless.
    b5.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init",
                  iso_image="ami-0123456789abcdef0", cloud_instance_type="m5.2xlarge")
run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm() uses cloud_instance_type verbatim, bypassing _pick_instance_type()",
      "m5.2xlarge" in run_instances_call and "t3.medium" not in run_instances_call)


# ── AWS_INSTANCE_TYPES: resolve() parses the config override into instance_types ──
resolved = backends.AWSBackend.resolve(
    {}, "vm1", {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "lab",
                "AWS_INSTANCE_TYPES": "tiny:1:2,huge:16:64"}, False)
check("resolve() parses AWS_INSTANCE_TYPES into resolved.instance_types",
      resolved.instance_types == [("tiny", 1, 2.0), ("huge", 16, 64.0)])

# ── _pick_instance_type(): an overridden table actually replaces INSTANCE_TYPES, not merges ──
b6 = backends.AWSBackend("eu-central-1", instance_types=[("tiny", 1, 2.0), ("huge", 16, 64.0)])
check("_pick_instance_type() picks from the overridden table when instance_types is set",
      b6._pick_instance_type(1, 2048, "vm1") == "tiny")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        # 32 vCPU fits plenty of entries in the real built-in INSTANCE_TYPES table, but NOT in
        # this override (max 16 cores) — a full replacement, not a merge.
        b6._pick_instance_type(32, 4096, "vm1")
    except SystemExit:
        pass
check("_pick_instance_type() does NOT fall back to the built-in INSTANCE_TYPES once overridden "
      "(full replacement, not a merge)", any("AWS_INSTANCE_TYPES" in m for m in died))


# ── _aws(): AWS_SESSION_TOKEN is passed through the subprocess env when set ─
b5 = backends.AWSBackend("eu-central-1", access_key="ASIA...", secret_key="secret", session_token="tok123")
seen_env = {}


def _fake_run_capture_env(args, env=None, **kwargs):
    seen_env.update(env or {})
    return _cp(0, stdout=json.dumps({"Reservations": []}))


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_capture_env):
    b5.vm_exists("vm1")
check("_aws() passes AWS_SESSION_TOKEN through to the subprocess env when set",
      seen_env.get("AWS_SESSION_TOKEN") == "tok123")

b6 = backends.AWSBackend("eu-central-1", access_key="AKIA...", secret_key="secret")
seen_env = {}
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_capture_env):
    b6.vm_exists("vm1")
check("_aws() omits AWS_SESSION_TOKEN entirely for a long-lived key pair (none configured)",
      "AWS_SESSION_TOKEN" not in seen_env)


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


# ── create_vm(): Internet Gateway + default-route management ───────────────
# Confirmed live 2026-09-13: a real AWS account's VPC had no Internet
# Gateway attached at all, and its route table had no 0.0.0.0/0 route —
# every AWS node still got a real public IP (assigned/NAT'd regardless),
# passed every security-group/NACL/guest-firewall check, yet remained
# completely unreachable, since packets had no path to arrive by in the
# first place. Automated at the user's own explicit request ("add it as
# part of the process of using aws") rather than left as a one-off manual
# CLI fix.
def _fake_run_igw(vpc_igws, route_tables, calls_out, created_igw_id="igw-new1"):
    def _run(args, **kwargs):
        calls_out.append(args)
        if "describe-images" in args:
            return _cp(0, stdout=json.dumps({"Images": [{"RootDeviceName": "/dev/sda1"}]}))
        if "describe-subnets" in args:
            return _cp(0, stdout=json.dumps({"Subnets": [{"VpcId": "vpc-1"}]}))
        if "describe-internet-gateways" in args:
            return _cp(0, stdout=json.dumps({"InternetGateways": vpc_igws}))
        if "create-internet-gateway" in args:
            return _cp(0, stdout=json.dumps({"InternetGateway": {"InternetGatewayId": created_igw_id}}))
        if "attach-internet-gateway" in args:
            return _cp(0, stdout="")
        if "describe-route-tables" in args:
            return _cp(0, stdout=json.dumps({"RouteTables": route_tables}))
        if "create-route" in args:
            return _cp(0, stdout="")
        if "describe-security-groups" in args:
            return _cp(0, stdout=json.dumps({"SecurityGroups": [{"IpPermissions": []}]}))
        if "authorize-security-group-ingress" in args:
            return _cp(0, stdout="")
        if "describe-instances" in args:
            return _cp(0, stdout=json.dumps(
                {"Reservations": [{"Instances": [{"InstanceId": "i-new", "PublicIpAddress": "203.0.113.10"}]}]}))
        if "run-instances" in args:
            return _cp(0, stdout=json.dumps({"Instances": [{"InstanceId": "i-new"}]}))
        return _cp(0, stdout="")
    return _run


b_igw = backends.AWSBackend("eu-central-1", profile="lab", subnet_id="subnet-1")
b_igw._user_data_by_vm["vm1"] = ""

# Nothing exists at all -> creates + attaches an IGW, then adds the route.
igw_calls = []
with mock.patch.object(backends.subprocess, "run",
                        side_effect=_fake_run_igw([], [{"RouteTableId": "rtb-1", "Routes": []}], igw_calls)):
    b_igw.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x")
check("create_vm(): creates a new Internet Gateway when none is attached to the VPC",
      any("create-internet-gateway" in c for c in igw_calls))
check("create_vm(): attaches the newly-created Internet Gateway to the VPC",
      any("attach-internet-gateway" in c and "igw-new1" in c and "vpc-1" in c for c in igw_calls))
check("create_vm(): adds the missing 0.0.0.0/0 route pointing at the (new) Internet Gateway",
      any("create-route" in c and "0.0.0.0/0" in c and "igw-new1" in c for c in igw_calls))

# An IGW is already attached AND the route already exists -> fully idempotent, no-op.
igw_calls = []
existing_igw = [{"InternetGatewayId": "igw-existing"}]
existing_rt = [{"RouteTableId": "rtb-1", "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-existing"}]}]
with mock.patch.object(backends.subprocess, "run",
                        side_effect=_fake_run_igw(existing_igw, existing_rt, igw_calls)):
    b_igw.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x")
check("create_vm(): an already-attached IGW is reused, never creates a second one",
      not any("create-internet-gateway" in c for c in igw_calls))
check("create_vm(): an already-present 0.0.0.0/0 route is left alone, never duplicated",
      not any("create-route" in c for c in igw_calls))

# An IGW exists but the route table has no 0.0.0.0/0 route yet -> reuses the
# existing IGW, only adds the missing route.
igw_calls = []
with mock.patch.object(backends.subprocess, "run",
                        side_effect=_fake_run_igw(existing_igw, [{"RouteTableId": "rtb-1", "Routes": []}], igw_calls)):
    b_igw.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x")
check("create_vm(): reuses an existing IGW but still adds a missing default route",
      not any("create-internet-gateway" in c for c in igw_calls)
      and any("create-route" in c and "igw-existing" in c for c in igw_calls))

# No subnet configured at all -> no-op, never touches IGW/route-table APIs.
b_no_subnet = backends.AWSBackend("eu-central-1", profile="lab")
b_no_subnet._user_data_by_vm["vm1"] = ""
igw_calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_igw([], [], igw_calls)):
    b_no_subnet.create_vm("vm1", 1, 512, 40, None, config_method="cloud-init", iso_image="ami-x")
check("create_vm(): no subnet configured at all -> never touches Internet Gateway/route-table APIs",
      not any("internet-gateway" in c or "route-table" in c or "create-route" in c
              for call in igw_calls for c in call))


# ── _find_instance(): a duplicate Name tag must not fool the lookup right ──
# after create_vm() ────────────────────────────────────────────────────────
# Confirmed live 2026-09-13: EC2 Name tags aren't unique. setup_vm.py (unlike
# setup_lab.py's own full run) doesn't destroy a pre-existing same-named
# instance first, so create_vm() can find itself with TWO instances tagged
# Name=vm1 — an old one and the one it just made. Before this fix, _find_
# instance()'s tag-only lookup returned "the first" match with no ordering
# guarantee, and create_vm()'s own post-create get_ip() poll silently
# returned the WRONG (old) instance's IP.
b_dup = backends.AWSBackend("eu-central-1", profile="lab")
b_dup._user_data_by_vm["vm1"] = ""
dup_calls = []


def _fake_run_dup_name_tag(args, **kwargs):
    dup_calls.append(args)
    if "describe-images" in args:
        return _cp(0, stdout=json.dumps({"Images": [{"RootDeviceName": "/dev/sda1"}]}))
    if "run-instances" in args:
        return _cp(0, stdout=json.dumps({"Instances": [{"InstanceId": "i-new"}]}))
    if "describe-instances" in args:
        if "--instance-ids" in args:
            # The exact-ID lookup create_vm() should now use: only the
            # correct, freshly-created instance is visible this way.
            return _cp(0, stdout=json.dumps({"Reservations": [
                {"Instances": [{"InstanceId": "i-new", "PublicIpAddress": "198.51.100.99"}]}]}))
        # The old, ambiguous tag-only lookup: BOTH instances match, old one first —
        # exactly the ordering that returned the wrong IP live.
        return _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
            {"InstanceId": "i-old", "PublicIpAddress": "203.0.113.1"},
            {"InstanceId": "i-new", "PublicIpAddress": "198.51.100.99"},
        ]}]}))
    return _cp(0, stdout="")


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_dup_name_tag):
    dup_ip = b_dup.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init",
                              iso_image="ami-0123456789abcdef0")
check("create_vm() returns the IP of the instance it JUST created, not an old "
      "same-named one that happens to sort first in a tag-only lookup",
      dup_ip == "198.51.100.99")

with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_dup_name_tag):
    check("get_ip() called again afterwards still targets the cached InstanceId, "
          "not the ambiguous tag", b_dup.get_ip("vm1") == "198.51.100.99")

# A backend instance that never created "vm1" itself (nothing cached — e.g. a
# separate destroy_vm.py invocation) must still fall back to the tag lookup.
b_fresh = backends.AWSBackend("eu-central-1", profile="lab")
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_dup_name_tag):
    fallback_ip = b_fresh.get_ip("vm1")
check("get_ip() falls back to the tag-based lookup when no InstanceId is cached "
      "for this vm_name (e.g. a fresh backend instance in a separate script run)",
      fallback_ip in ("203.0.113.1", "198.51.100.99"))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all aws_backend checks passed")
