#!/usr/bin/env python3
# Unit tests for install_smlm.py's pre-built server image support and the
# server-side lab conveniences — see 60_smlm_prebuilt_image.sh. SSH and the
# XML-RPC API are mocked; mgradm_common's install helpers are stubbed the same
# way 49_smlm_baremetal_test.py does.
import sys
import xmlrpc.client
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import install_smlm as ism  # noqa: E402
import mgradm_common  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class Died(Exception):
    pass


def _die(msg):
    raise Died(msg)


ism.die = _die


def run(cfg, container_exists, present_channels=""):
    calls = []

    def fake_ssh_run(hostname, cmd, check=True, capture=False, input_text=None):
        calls.append(cmd)
        if "podman container exists uyuni-server" in cmd:
            return FakeResult(returncode=0 if container_exists else 1)
        if "command -v transactional-update" in cmd:
            return FakeResult(returncode=0)
        if "softwarechannel_list" in cmd:
            return FakeResult(stdout=present_channels)
        if "mgr-sync list channels" in cmd:
            return FakeResult(stdout="some-channel\n")
        return FakeResult()

    ism.ssh_run = fake_ssh_run
    ism.reboot_vm = lambda virt_srv, hostname: calls.append("REBOOT")
    ism.check_ssh_conn = lambda hostname: calls.append("CHECK_SSH")
    ism.time.sleep = lambda s: None
    guard = []
    mgradm_common.run_install_with_pg_hba_guard = lambda hostname, cmd: guard.append(cmd)
    mgradm_common.ensure_server_container_active = lambda hostname: None
    sc_calls = []
    for name in dir(ism.sc):
        if name.startswith("ensure_"):
            setattr(ism.sc, name, (lambda n: lambda *a, **k: sc_calls.append(n))(name))
    ism.setup_smlm_podman("smlm.lab", "qemu:///system", cfg)
    return calls, guard, sc_calls


KEYS = [{"smlm_activation_key": "sles15sp6",
         "smlm_activation_key_base_channel": "base-a",
         "smlm_activation_key_child_channels": "child-a child-b"}]

# ── _validate ──────────────────────────────────────────────────────────────
v = ac.Validator({"smlm": {"smlm_deployment": "podman", "smlm_preinstalled": "true",
                           "smlm_activation_keys": KEYS}})
ism._validate(v)
check("_validate: preinstalled needs neither regcode nor mirror credentials", v.errors == [])

v = ac.Validator({"smlm": {"smlm_deployment": "podman", "smlm_scc_regcode": "R",
                           "smlm_bootstrap_scripts": [{"name": "../x.sh", "url": "https://e/x"}]}})
ism._validate(v)
check("_validate: bootstrap script name with a path is rejected",
      any("smlm_bootstrap_scripts" in e for e in v.errors))

v = ac.Validator({"smlm": {"smlm_deployment": "podman", "smlm_scc_regcode": "R",
                           "smlm_preinstalled": "yes"}})
ism._validate(v)
check("_validate: smlm_preinstalled must be true/false",
      any("smlm_preinstalled" in e for e in v.errors))

# ── pre-built image: every channel already present ─────────────────────────
cfg = {"smlm_preinstalled": "true", "smlm_activation_keys": KEYS}
calls, guard, sc_calls = run(cfg, container_exists=True, present_channels="child-a\nchild-b\n")
check("preinstalled: no SUSEConnect registration", not any("SUSEConnect" in c for c in calls))
check("preinstalled: no tooling install", not any("install -y" in c for c in calls))
check("preinstalled: no mgradm install", guard == [])
check("preinstalled: no mgr-sync when channels are present", not any("mgr-sync" in c for c in calls))
check("preinstalled: declarative steps still run", "ensure_activation_keys" in sc_calls)

# ── pre-built image: one channel missing -> needs mirror credentials ───────
try:
    run(cfg, container_exists=True, present_channels="child-a\n")
    check("preinstalled + missing channel without mirror creds dies", False)
except Died:
    pass

cfg_creds = dict(cfg, smlm_scc_user="u", smlm_scc_password="p")
calls, _, _ = run(cfg_creds, container_exists=True, present_channels="child-a\n")
added = [c for c in calls if "mgr-sync add channels" in c]
check("preinstalled: only the missing channel is added",
      len(added) == 1 and "child-b" in added[0] and "child-a" not in added[0])

# ── smlm_preinstalled set but no container: normal install path ────────────
calls, guard, _ = run({"smlm_preinstalled": "true", "smlm_scc_regcode": "R"}, container_exists=False)
check("preinstalled without a container falls back to a full install",
      any("SUSEConnect -r" in c for c in calls) and len(guard) == 1)

# ── SSL subject flags ──────────────────────────────────────────────────────
_, guard, _ = run({"smlm_scc_regcode": "R", "smlm_ssl_country": "CH", "smlm_ssl_org": "Chameleon Air"},
                  container_exists=False)
check("ssl subject flags are passed to mgradm install",
      "--ssl-country CH" in guard[0] and "--ssl-org 'Chameleon Air'" in guard[0])
check("unset ssl subject flags are left out", "--ssl-city" not in guard[0])

# ── salt auto_accept + bootstrap scripts ───────────────────────────────────
calls, _, _ = run({"smlm_preinstalled": "true", "smlm_salt_auto_accept": "true",
                   "smlm_bootstrap_scripts": [{"name": "generic_bootstrap.sh",
                                               "url": "https://example.com/g.sh"}]},
                  container_exists=True)
check("auto_accept drop-in written and salt-master restarted",
      any("zz-lab-auto-accept.conf" in c and "restart salt-master" in c for c in calls))
check("bootstrap script copied into /pub/bootstrap",
      any("mgrctl cp" in c and "/srv/www/htdocs/pub/bootstrap/generic_bootstrap.sh" in c for c in calls))
calls, _, _ = run({"smlm_preinstalled": "true"}, container_exists=True)
check("auto_accept off by default", not any("auto-accept" in c for c in calls))

# ── smlm_byos: SUSE's own SMLM Server image ────────────────────────────────
calls, guard, _ = run({"smlm_byos": "true", "smlm_scc_regcode": "R"}, container_exists=False)
check("byos: registered with transactional-update register",
      any(c.startswith("transactional-update --quiet register -r R") for c in calls))
check("byos: rebooted after registering", "REBOOT" in calls)
check("byos: no containers module / SMLM extension registration",
      not any("SUSEConnect -p" in c for c in calls))
check("byos: no tooling install", not any("pkg install" in c or "zypper" in c for c in calls))
check("byos: mgradm install still runs", len(guard) == 1)

_orig_ssh = ism.ssh_run


def _no_mgradm(hostname, cmd, check=True, capture=False, input_text=None):
    if cmd == "command -v mgradm":
        return FakeResult(returncode=1)
    return FakeResult()


ism.ssh_run = _no_mgradm
try:
    ism._register_byos_image("smlm.lab", "qemu:///system", "R")
    check("byos without mgradm dies", False)
except Died as e:
    check("byos without mgradm names the BYOS image", "BYOS image" in str(e))
ism.ssh_run = _orig_ssh

v = ac.Validator({"smlm": {"smlm_deployment": "podman", "smlm_scc_regcode": "R", "smlm_byos": "yes"}})
ism._validate(v)
check("_validate: smlm_byos must be true/false", any("smlm_byos" in e for e in v.errors))

# ── admin password rotation ────────────────────────────────────────────────
class FakeAPI:
    def __init__(self, valid):
        self.valid = valid
        self.set_details = []
        api = self

        class Auth:
            def login(self, user, pw):
                if pw != api.valid:
                    raise xmlrpc.client.Fault(2950, "bad login")
                return "session"

            def logout(self, key):
                return 1

        class User:
            def setDetails(self, key, login, details):
                api.set_details.append((login, details))
                return 1

        self.auth = Auth()
        self.user = User()


api = FakeAPI(valid="baked")
ism.xmlrpc.client.ServerProxy = lambda url, context=None: api
ism.rotate_admin_password("smlm.lab", "admin", "fresh", "baked")
check("rotation: image password replaced by the lab password",
      api.set_details == [("admin", {"password": "fresh"})])

api = FakeAPI(valid="fresh")
ism.xmlrpc.client.ServerProxy = lambda url, context=None: api
ism.rotate_admin_password("smlm.lab", "admin", "fresh", "baked")
check("rotation: no-op when the lab password already works", api.set_details == [])

api = FakeAPI(valid="other")
ism.xmlrpc.client.ServerProxy = lambda url, context=None: api
try:
    ism.rotate_admin_password("smlm.lab", "admin", "fresh", "baked")
    check("rotation: dies when neither password works", False)
except Died:
    pass

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all smlm prebuilt image checks passed")
