#!/usr/bin/env python3
# Unit tests for the multiple-cloud-accounts feature (added 2026-09-10, same
# shape as KVM_HOSTS giving multiple hypervisors):
#   primary.try_load_cloud_account / load_cloud_account  — the account files
#   backends.resolve_cloud_account / effective_backend_name / get_backend
#   backends.ensure_cloud_dns_vm  — per-account DNS VM name
# No real cloud is touched; account files are temp files pointed at via a
# patched primary.cloud_account_path. Run from 44_cloud_accounts.sh, in its
# own container — see tests/run_tests.sh.
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import primary   # noqa: E402
import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _write(dirpath, name, ext, text):
    p = Path(dirpath) / "{}{}".format(name, ext)
    p.write_text(text)
    return p


# ── primary.try_load_cloud_account: cfg / yaml / json + CLOUDTYPE normalising ──
with tempfile.TemporaryDirectory() as d:
    _write(d, "aws-sandbox", ".cfg",
           "CLOUDTYPE=aws\nAWS_REGION=us-east-1\nAWS_PROFILE=sandbox\n")
    with mock.patch.object(primary, "cloud_account_path",
                           side_effect=lambda n, config=None: Path(d) / (n + ".cfg")):
        data, err = primary.try_load_cloud_account("aws-sandbox")
    check("cfg account: parses, no error", err is None and data is not None)
    check("cfg account: CLOUDTYPE normalised from the file's cloudtype/CLOUDTYPE key",
          data.get("CLOUDTYPE") == "aws")
    check("cfg account: the provider's own keys come through",
          data.get("AWS_REGION") == "us-east-1" and data.get("AWS_PROFILE") == "sandbox")
    check("cfg account: the raw cloudtype key is consumed, not left alongside CLOUDTYPE",
          "cloudtype" not in data and "cloud_type" not in data)

with tempfile.TemporaryDirectory() as d:
    _write(d, "gcp-prod", ".json",
           json.dumps({"cloudtype": "gcp", "GCP_PROJECT": "lab-prod", "GCP_ZONE": "europe-west1-b"}))
    with mock.patch.object(primary, "cloud_account_path",
                           side_effect=lambda n, config=None: Path(d) / (n + ".json")):
        data, err = primary.try_load_cloud_account("gcp-prod")
    check("json account: parses with lowercase 'cloudtype'", err is None and data.get("CLOUDTYPE") == "gcp")
    check("json account: keys come through", data.get("GCP_PROJECT") == "lab-prod")

with tempfile.TemporaryDirectory() as d:
    try:
        import yaml  # noqa: F401
        _write(d, "hetzner-eu", ".yaml", "cloudtype: hetzner\nHETZNER_TOKEN: tok-123\n")
        with mock.patch.object(primary, "cloud_account_path",
                               side_effect=lambda n, config=None: Path(d) / (n + ".yaml")):
            data, err = primary.try_load_cloud_account("hetzner-eu")
        check("yaml account: parses", err is None and data.get("CLOUDTYPE") == "hetzner"
              and data.get("HETZNER_TOKEN") == "tok-123")
    except ImportError:
        print("pyyaml not installed — skipping the YAML account-file check")

# missing file
with mock.patch.object(primary, "cloud_account_path", return_value=None):
    data, err = primary.try_load_cloud_account("nope")
check("missing account file: returns a clear error, no data", data is None and err and "not found" in err)

# file with no cloudtype
with tempfile.TemporaryDirectory() as d:
    _write(d, "broken", ".cfg", "AWS_REGION=us-east-1\n")
    with mock.patch.object(primary, "cloud_account_path",
                           side_effect=lambda n, config=None: Path(d) / (n + ".cfg")):
        data, err = primary.try_load_cloud_account("broken")
check("account file with no cloudtype: errors", data is None and err and "cloudtype" in err)

# load_cloud_account (dying wrapper)
with mock.patch.object(primary, "cloud_account_path", return_value=None):
    died = []
    with mock.patch.object(primary, "_die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)):
        try:
            primary.load_cloud_account("nope")
        except SystemExit:
            pass
check("load_cloud_account: dies on a missing account", any("not found" in m for m in died))


# ── primary.list_cloud_accounts / find_cloud_account_for_cloudtype ─────────
# Auto-discovery (added 2026-09-12): a cloud-backend node with no explicit
# "cloud_account" should use an encrypted credentials file automatically if
# exactly one matches its provider, instead of silently falling back to
# plaintext lab_creation.cfg keys — the point of the encrypted-store feature
# in the first place.
with tempfile.TemporaryDirectory() as d:
    _write(d, "aws-only", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=us-east-1\n")
    _write(d, "gcp-only", ".json", json.dumps({"cloudtype": "gcp", "GCP_PROJECT": "p"}))
    _write(d, "not-an-account", ".cfg", "just some text with no cloudtype\n")
    cfg_disc = {"CREDENTIALS_PATH": d}

    accounts = primary.list_cloud_accounts(cfg_disc)
    check("list_cloud_accounts: finds every parseable file with a cloudtype",
          set(accounts) == {("aws-only", "aws"), ("gcp-only", "gcp")})
    check("list_cloud_accounts: a file with no cloudtype is skipped, not an error",
          all(name != "not-an-account" for name, _ in accounts))

    found, matches = primary.find_cloud_account_for_cloudtype("aws", cfg_disc)
    check("find_cloud_account_for_cloudtype: exactly one match -> returns its name",
          found == "aws-only" and matches == ["aws-only"])

    found2, matches2 = primary.find_cloud_account_for_cloudtype("hetzner", cfg_disc)
    check("find_cloud_account_for_cloudtype: no match -> (None, [])", found2 is None and matches2 == [])

with tempfile.TemporaryDirectory() as d:
    _write(d, "aws-one", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=us-east-1\n")
    _write(d, "aws-two", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=us-west-2\n")
    found3, matches3 = primary.find_cloud_account_for_cloudtype("aws", {"CREDENTIALS_PATH": d})
    check("find_cloud_account_for_cloudtype: two matches -> (None, both names) — ambiguous",
          found3 is None and sorted(matches3) == ["aws-one", "aws-two"])


# ── backends.resolve_cloud_account / effective_backend_name: auto-discovery ──
with tempfile.TemporaryDirectory() as d:
    _write(d, "aws-auto", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=eu-central-1\nAWS_PROFILE=auto\n")
    auto_cfg = {"CREDENTIALS_PATH": d, "SHARED": "keep-me"}

    acct, eff, ct = backends.resolve_cloud_account(
        {"nodes": {"vm1": {"backend": "aws"}}, "common": {}}, auto_cfg, "vm1")
    check("resolve_cloud_account: backend=aws + no cloud_account + exactly one aws "
          "credentials file -> auto-discovered and used",
          acct == "aws-auto" and ct == "aws" and eff["AWS_PROFILE"] == "auto")
    check("resolve_cloud_account: auto-discovered account still merges over lab_creation.cfg,"
          " preserving unrelated keys", eff["SHARED"] == "keep-me")

    check("effective_backend_name: auto-discovers the same account for its cloudtype",
          backends.effective_backend_name(
              {"nodes": {"vm1": {"backend": "aws"}}, "common": {}}, auto_cfg, "vm1") == "aws")

    # libvirt (no cloud backend at all) must never trigger a credentials scan
    acct_lv, eff_lv, ct_lv = backends.resolve_cloud_account(
        {"nodes": {"vm1": {}}, "common": {}}, auto_cfg, "vm1")
    check("resolve_cloud_account: a libvirt node (no backend set) never auto-discovers anything",
          acct_lv == "" and eff_lv is auto_cfg and ct_lv is None)

    # zero matching accounts -> unchanged fallback (today's behaviour, not a hard error)
    acct_none, eff_none, ct_none = backends.resolve_cloud_account(
        {"nodes": {"vm1": {"backend": "hetzner"}}, "common": {}}, auto_cfg, "vm1")
    check("resolve_cloud_account: backend set but no matching credentials file -> "
          "falls back to lab_creation.cfg unchanged, no error",
          acct_none == "" and eff_none is auto_cfg and ct_none is None)

with tempfile.TemporaryDirectory() as d:
    _write(d, "aws-x", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=us-east-1\n")
    _write(d, "aws-y", ".cfg", "CLOUDTYPE=aws\nAWS_REGION=us-west-2\n")
    ambiguous_cfg = {"CREDENTIALS_PATH": d}
    died = []
    with mock.patch.object(backends, "die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.resolve_cloud_account({"nodes": {"vm1": {"backend": "aws"}}, "common": {}}, ambiguous_cfg, "vm1")
        except SystemExit:
            pass
    check("resolve_cloud_account: two matching credentials files -> dies clearly instead of guessing",
          any("cloud_account" in m and "aws-x" in m and "aws-y" in m for m in died))


# ── backends.resolve_cloud_account: passthrough vs. merge ────────────────────
base_cfg = {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "default", "SHARED": "keep-me"}

acct, eff, ct = backends.resolve_cloud_account({"nodes": {"vm1": {}}, "common": {}}, base_cfg, "vm1")
check("resolve_cloud_account: no cloud_account -> ('', config unchanged, None)",
      acct == "" and eff is base_cfg and ct is None)

fake_acct = {"CLOUDTYPE": "aws", "AWS_REGION": "us-west-2", "AWS_PROFILE": "sbx"}
with mock.patch.object(backends.primary, "load_cloud_account", return_value=dict(fake_acct)):
    acct, eff, ct = backends.resolve_cloud_account(
        {"nodes": {"vm1": {"cloud_account": "aws-sbx"}}, "common": {}}, base_cfg, "vm1")
check("resolve_cloud_account: cloud_account -> name + cloudtype", acct == "aws-sbx" and ct == "aws")
check("resolve_cloud_account: account keys win over lab_creation.cfg",
      eff["AWS_REGION"] == "us-west-2" and eff["AWS_PROFILE"] == "sbx")
check("resolve_cloud_account: unrelated lab_creation.cfg keys are preserved", eff["SHARED"] == "keep-me")
check("resolve_cloud_account: the passed-in config dict is not mutated", base_cfg["AWS_REGION"] == "eu-central-1")

# per-node overrides common
with mock.patch.object(backends.primary, "load_cloud_account",
                       side_effect=lambda n, config=None: (
                           {"CLOUDTYPE": "gcp"} if n == "gcp-a" else {"CLOUDTYPE": "aws"})):
    acct, _e, ct = backends.resolve_cloud_account(
        {"nodes": {"vm1": {"cloud_account": "gcp-a"}}, "common": {"cloud_account": "aws-b"}}, {}, "vm1")
check("resolve_cloud_account: per-node cloud_account beats common", acct == "gcp-a" and ct == "gcp")


# ── backends.effective_backend_name ─────────────────────────────────────────
check("effective_backend_name: no account, no backend -> libvirt",
      backends.effective_backend_name({"nodes": {"vm1": {}}, "common": {}}, {}, "vm1") == "libvirt")
check("effective_backend_name: explicit backend passes through",
      backends.effective_backend_name({"nodes": {"vm1": {"backend": "hetzner"}}, "common": {}}, {}, "vm1") == "hetzner")
with mock.patch.object(backends.primary, "load_cloud_account", return_value={"CLOUDTYPE": "aws"}):
    check("effective_backend_name: cloud_account's cloudtype wins",
          backends.effective_backend_name(
              {"nodes": {"vm1": {"cloud_account": "x"}}, "common": {}}, {}, "vm1") == "aws")


# ── backends.get_backend: sets .account, and the backend/cloudtype disagreement die ──
_aws_acct = {"CLOUDTYPE": "aws", "AWS_REGION": "eu-central-1", "AWS_PROFILE": "p"}
with mock.patch.object(backends.primary, "load_cloud_account", return_value=dict(_aws_acct)):
    b = backends.get_backend({"nodes": {"vm1": {"cloud_account": "acctA"}}, "common": {}}, {}, "vm1")
check("get_backend: a cloud_account node resolves to that provider's backend",
      isinstance(b, backends.AWSBackend))
check("get_backend: the resolved instance carries .account", b.account == "acctA")
check("get_backend: the account file's connection keys reached resolve()", b.region == "eu-central-1")

with mock.patch.object(backends.primary, "load_cloud_account", return_value={"CLOUDTYPE": "aws"}):
    died = []
    with mock.patch.object(backends, "die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)):
        try:
            backends.get_backend(
                {"nodes": {"vm1": {"cloud_account": "acctA", "backend": "gcp"}}, "common": {}}, {}, "vm1")
        except SystemExit:
            pass
check("get_backend: dies when the node's backend disagrees with the account's cloudtype",
      any("cloud_account" in m and "gcp" in m for m in died))


# ── ensure_cloud_dns_vm: per-account DNS VM name ────────────────────────────
class _FakeCloudBackend:
    def __init__(self, account=""):
        self.account = account
        self.created = []

    def vm_exists(self, name):
        return False

    def get_ip(self, name):
        return "203.0.113.9"

    def copy_vm_image(self, *a, **kw):
        pass

    def push_provisioning_files(self, *a, **kw):
        pass

    def create_vm(self, name, *a, **kw):
        self.created.append(name)
        return "203.0.113.9"


def _dns_vm_name_for(account):
    fb = _FakeCloudBackend(account=account)
    with tempfile.TemporaryDirectory() as d, \
         mock.patch.object(backends.subprocess, "run",
                           return_value=backends.subprocess.CompletedProcess([], 0)):
        backends.ensure_cloud_dns_vm(fb, "aws", "ssh-rsa AAAA", "mydemo.lab", "ami-x", d)
    return fb.created[0]

check("ensure_cloud_dns_vm: default account -> lab-dns-<backend> (unchanged)",
      _dns_vm_name_for("") == "lab-dns-aws")
check("ensure_cloud_dns_vm: 'default' account also -> lab-dns-<backend>",
      _dns_vm_name_for("default") == "lab-dns-aws")
check("ensure_cloud_dns_vm: a named account -> lab-dns-<backend>-<account>",
      _dns_vm_name_for("sandbox") == "lab-dns-aws-sandbox")


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all cloud_accounts checks passed")
