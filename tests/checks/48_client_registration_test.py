#!/usr/bin/env python3
# Unit tests for the per-node addon-config override. An addons[] entry is a plain "<addon>" string, or a single-key
# {"<addon>": {...}} mapping that overrides that addon's shared config for one node. The shared parsing helpers
# (apps.addon_entry_name and addon_entry_overrides), k8s.addon_nodes() and addon_node_config(), and install_client_registration
# are covered. register_client() is mocked. Run from 48_client_registration.sh, in its own container (see tests/run_tests.sh).
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import k8s  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── apps.addon_entry_name / addon_entry_overrides: the shared parsing ──────
check("addon_entry_name: a plain string entry is returned unchanged",
      apps.addon_entry_name("mariadb") == "mariadb")
check("addon_entry_name: a {addon: {...}} entry returns the addon name",
      apps.addon_entry_name({"client_registration": {"client_registration_activation_key": "1-x"}})
      == "client_registration")
check("addon_entry_overrides: a plain string entry has no overrides",
      apps.addon_entry_overrides("mariadb") == {})
check("addon_entry_overrides: a {addon: {...}} entry returns its own value",
      apps.addon_entry_overrides({"client_registration": {"client_registration_activation_key": "1-x"}})
      == {"client_registration_activation_key": "1-x"})

died = []
apps.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)
try:
    apps.addon_entry_name({"a": {}, "b": {}})
except SystemExit:
    pass
check("addon_entry_name: an entry with more than one key dies clearly", any("exactly one key" in m for m in died))


# ── apps.collect_addon_names: mixed plain-string and nested entries ────────
mixed_def = {
    "kclusters": {"c1": {"addons": ["rancher"]}},
    "nodes": {
        "vm1": {"addons": ["mariadb", {"client_registration": {"client_registration_activation_key": "1-x"}}]},
        "vm2": {"addons": ["mariadb"]},  # same addon, no override -> still just "mariadb"
    },
}
check("collect_addon_names: dedupes correctly across plain and nested entries",
      apps.collect_addon_names(mixed_def) == ["client_registration", "mariadb", "rancher"])


# ── k8s.addon_nodes: a nested entry counts as "has this addon" ─────────────
nodes = k8s.addon_nodes(mixed_def, "client_registration")
check("addon_nodes: finds the node whose addons[] entry is nested for this addon",
      [n for n, _ in nodes] == ["vm1"])
check("addon_nodes: a node using a DIFFERENT addon isn't matched",
      [n for n, _ in k8s.addon_nodes(mixed_def, "rancher")] == [])


# ── k8s.addon_node_config: the actual per-node merge ────────────────────────
cfg_def = {
    "client_registration": {
        "client_registration_server": "smlm52beta.mydemo.lab",
        "client_registration_admin_user": "admin",
        "client_registration_activation_key": "1-shared-default",
    },
    "nodes": {
        "mercury": {"addons": [{"client_registration": {"client_registration_activation_key": "1-sles15sp7"}}]},
        "callisto": {"addons": [{"client_registration": {"client_registration_activation_key": "1-debian13"}}]},
        "ganymede": {"addons": ["client_registration"]},  # no override -> shared default
    },
}
merc = k8s.addon_node_config(cfg_def, "client_registration", "mercury")
check("addon_node_config: the node's own override wins", merc["client_registration_activation_key"] == "1-sles15sp7")
check("addon_node_config: fields the node doesn't override still come from the shared section",
      merc["client_registration_server"] == "smlm52beta.mydemo.lab"
      and merc["client_registration_admin_user"] == "admin")

call = k8s.addon_node_config(cfg_def, "client_registration", "callisto")
check("addon_node_config: a different node gets its OWN override, not another node's",
      call["client_registration_activation_key"] == "1-debian13")

gany = k8s.addon_node_config(cfg_def, "client_registration", "ganymede")
check("addon_node_config: a plain-string entry (no override) falls back to the shared default",
      gany["client_registration_activation_key"] == "1-shared-default")

check("addon_node_config: overriding one node never mutates the shared top-level section",
      cfg_def["client_registration"]["client_registration_activation_key"] == "1-shared-default")


# ── install_client_registration: end-to-end through main() ──────────────
icr = apps.load_addon_module("install_client_registration", str(_REPO / "scripts"))  # no .py: loaded by path

# ensure_swap()/ensure_centos7_vault_repos() are real lab_creation.py code (their own coverage lives
# in 10_lab_creation_core_test.py) — stubbed out everywhere in this file so register_client()'s own
# tests below don't make a real SSH call. swap_calls lets specific tests confirm ensure_swap is
# actually invoked, with the right node.
swap_calls = []
icr.ensure_swap = lambda vm_name, *a, **kw: swap_calls.append(vm_name)
icr.ensure_centos7_vault_repos = lambda vm_name, *a, **kw: True

_real_register_client = icr.register_client  # main() tests below stub this out

calls = []
icr.register_client = lambda vm_name, cfg, lab_file=None: calls.append((vm_name, cfg))
icr.primary.load_definition = lambda path: cfg_def

old_argv = sys.argv
sys.argv = ["install_client_registration", "lab.json"]
try:
    icr.main()
finally:
    sys.argv = old_argv

by_node = {vm: cfg for vm, cfg in calls}
check("install_client_registration main(): registers every node with the addon, and only those",
      set(by_node) == {"mercury", "callisto", "ganymede"})
check("install_client_registration main(): each node's nested override reaches register_client()",
      by_node["mercury"]["client_registration_activation_key"] == "1-sles15sp7"
      and by_node["callisto"]["client_registration_activation_key"] == "1-debian13"
      and by_node["ganymede"]["client_registration_activation_key"] == "1-shared-default")

# _vm_name env scoping still dispatches to exactly one node, with its override
calls.clear()
import os  # noqa: E402
os.environ["_vm_name"] = "callisto"
sys.argv = ["install_client_registration", "lab.json"]
try:
    icr.main()
finally:
    del os.environ["_vm_name"]
    sys.argv = old_argv
check("install_client_registration main(): _vm_name env scopes to one node, override still applied",
      len(calls) == 1 and calls[0][0] == "callisto"
      and calls[0][1]["client_registration_activation_key"] == "1-debian13")


# ── register_client(): a background retry when channels are still syncing ──
# When the required channels are not fully synced, registration is handed to a detached background worker, which runs until the
# registration succeeds. The call returns at once, so the rest of the deployment continues.
class _FakeSc:
    def __init__(self, pending=None, key_exists=False, real_channels=None):
        self.calls = []
        self._pending = pending or set()
        self._key_exists = key_exists
        self._real_channels = real_channels or {}

    def ensure_spacecmd_config(self, *a, **kw):
        self.calls.append(("ensure_spacecmd_config", a, kw))

    def ensure_channels_synced(self, *a, **kw):
        self.calls.append(("ensure_channels_synced", a, kw))

    def ensure_activation_key(self, *a, **kw):
        self.calls.append(("ensure_activation_key", a, kw))

    def activation_key_exists(self, hostname, exec_prefix, key_name):
        self.calls.append(("activation_key_exists", key_name))
        return self._key_exists

    def describe_activation_key(self, hostname, exec_prefix, key_name, prefix):
        self.calls.append(("describe_activation_key", key_name))
        return dict(self._real_channels)

    def pending_channels(self, hostname, exec_prefix, channels):
        self.calls.append(("pending_channels", channels))
        return set(self._pending)

    def wait_for_channels_synced(self, *a, **kw):
        self.calls.append(("wait_for_channels_synced", a, kw))

    def ensure_client_registered(self, *a, **kw):
        self.calls.append(("ensure_client_registered", a, kw))

    def forever_retry_log_path(self, key):
        self.calls.append(("forever_retry_log_path", key))
        return "/fake/log/path/for/{}.log".format(key)

    def system_is_registered(self, hostname, exec_prefix, vm_name):
        self.calls.append(("system_is_registered", vm_name))
        return self._registered.get(vm_name, True) if hasattr(self, "_registered") else True

    def system_is_registered_and_healthy(self, hostname, exec_prefix, vm_name):
        self.calls.append(("system_is_registered_and_healthy", vm_name))
        return self._registered.get(vm_name, True) if hasattr(self, "_registered") else True

    def system_package_count(self, hostname, exec_prefix, vm_name):
        self.calls.append(("system_package_count", vm_name))
        return 599

    def run_forever_retry_worker(self, key, attempt, retry_delay=60, heartbeat_interval=1800, verify=None):
        # A small, local stand-in for the real forever loop (its own correctness — heartbeats, pid
        # files, crash-proofing — is covered end to end in 09_spacecmd_common_test.py). This exists only
        # to exercise WHAT _retry_until_registered() hands it: the right key, the right attempt() closure
        # (does it really call ensure_spacecmd_config/wait_for_channels_synced/_register_now in order),
        # and the right verify() closure (does a verify() that rejects actually keep it retrying).
        self.calls.append(("run_forever_retry_worker:key", key))
        while True:
            try:
                attempt()
            except SystemExit:
                continue
            except Exception:  # noqa: BLE001 — matches the real loop's own deliberately broad catch
                continue
            if verify is None or verify():
                return


launch_calls = []
icr._launch_background_retry = lambda lab_file, vm_name: launch_calls.append((lab_file, vm_name))

# -- fast path: channels already fully synced -> registers synchronously, no background retry --
launch_calls.clear()
fake_sc = _FakeSc(pending=set())
icr.sc = fake_sc
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
    "client_registration_sync_channels": "sles15-sp7-pool sles15-sp7-updates",
    "client_registration_activation_key_base_channel": "sles15-sp7-pool",
    "client_registration_activation_key_child_channels": "managertools-sles15-sp7-pool",
}, lab_file="lab.json")
names = [c[0] for c in fake_sc.calls]
pending_call = next(c for c in fake_sc.calls if c[0] == "pending_channels")
check("register_client(): checks pending_channels() on the UNION of sync_channels + "
      "activation key base/child channels",
      set(pending_call[1]) == {"sles15-sp7-pool", "sles15-sp7-updates", "managertools-sles15-sp7-pool"})
check("register_client(): fully synced -> registers synchronously (ensure_client_registered called)",
      "ensure_client_registered" in names)
check("register_client(): fully synced -> no background retry launched", launch_calls == [])
check("register_client(): fully synced -> checks the real package count via system_package_count()",
      any(c[0] == "system_package_count" for c in fake_sc.calls))
check("register_client(): ensures swap on the CLIENT (vm_name) before the real registration call,"
      " not the server", swap_calls == ["mercury.mydemo.lab"])
swap_calls.clear()

# -- same fast path, but the package profile hasn't uploaded yet -> warns, does NOT die or retry --
launch_calls.clear()


class _ScLowPackageCount(_FakeSc):
    def system_package_count(self, hostname, exec_prefix, vm_name):
        self.calls.append(("system_package_count", vm_name))
        return 3


fake_sc_low = _ScLowPackageCount(pending=set())
icr.sc = fake_sc_low
_warn_buf = []
_real_warn = icr.warn
icr.warn = lambda msg: _warn_buf.append(msg)
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
    "client_registration_activation_key_base_channel": "sles15-sp7-pool",
}, lab_file="lab.json")
icr.warn = _real_warn
check("register_client(): a suspiciously low post-registration package count warns, not dies, "
      "and still completed registration synchronously",
      len(_warn_buf) == 1 and "mercury.mydemo.lab" in _warn_buf[0] and "3" in _warn_buf[0]
      and launch_calls == [])

# -- slow path: channels NOT yet fully synced -> hands off, returns without registering --
launch_calls.clear()
fake_sc2 = _FakeSc(pending={"sles15-sp7-pool"})
icr.sc = fake_sc2
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
    "client_registration_activation_key_base_channel": "sles15-sp7-pool",
}, lab_file="lab.json")
names2 = [c[0] for c in fake_sc2.calls]
check("register_client(): channels still pending -> launches a background retry for this node",
      launch_calls == [("lab.json", "mercury.mydemo.lab")])
check("register_client(): channels still pending -> does NOT register synchronously "
      "(that's the whole point — don't block)",
      "ensure_client_registered" not in names2)

# -- the key doesn't exist yet AND this node has no base_channel to create it with -> hand off to a
# background retry instead of dying — the fix for a real bug found live 2026-11-03: register_client()
# used to call ensure_activation_key() unconditionally here, which die()s hard the instant base_channel
# is missing, so a node failed outright (never even reaching the pending-channel handoff) purely
# because its OWN activation key is defined elsewhere (e.g. install_smlm's smlm_activation_keys) and
# just hadn't been created yet by the time this ran.
launch_calls.clear()
fake_sc3 = _FakeSc(key_exists=False)
icr.sc = fake_sc3
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
}, lab_file="lab.json")
names3 = [c[0] for c in fake_sc3.calls]
check("register_client(): key doesn't exist yet, no base_channel configured here -> checks "
      "existence before giving up", any(c[0] == "activation_key_exists" for c in fake_sc3.calls))
check("register_client(): ...and hands off to a background retry instead of dying or "
      "trying (and failing) to create the key itself",
      launch_calls == [("lab.json", "mercury.mydemo.lab")]
      and "ensure_activation_key" not in names3 and "ensure_client_registered" not in names3)

# -- same situation, but with no lab_file to hand off to -> THIS is a real, un-fixable
# configuration problem, so it still dies, just with a clearer message than before --
fake_sc3b = _FakeSc(key_exists=False)
icr.sc = fake_sc3b
died = False
try:
    _real_register_client("mercury.mydemo.lab", {
        "client_registration_server": "sol.mydemo.lab",
        "client_registration_activation_key": "1-key",
    })  # lab_file omitted
except SystemExit:
    died = True
check("register_client(): key doesn't exist, no base_channel, AND no lab_file to hand off to -> "
      "dies (nothing else can resolve this)", died)

# -- when base_channel IS configured here, this addon is meant to create the key itself, same as
# before the fix --
fake_sc3c = _FakeSc(key_exists=False, pending=set())
icr.sc = fake_sc3c
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
    "client_registration_activation_key_base_channel": "sles15-sp7-pool",
}, lab_file="lab.json")
check("register_client(): base_channel IS configured here -> still creates the key itself, "
      "unchanged from before", any(c[0] == "ensure_activation_key" for c in fake_sc3c.calls))

# -- Channels from the activation key ------------------------------------------
# When the activation key is created elsewhere and the node sets only the key name, the channels are read from the server with
# describe_activation_key(). Otherwise registration would not wait for channels that are still syncing.
launch_calls.clear()
fake_sc4 = _FakeSc(
    key_exists=True,
    real_channels={
        "client_registration_activation_key_base_channel": "sle-product-sles15-sp7-pool-x86_64",
        "client_registration_activation_key_child_channels": "managertools-sle15-pool-x86_64-sp7",
    },
    pending={"managertools-sle15-pool-x86_64-sp7"},
)
icr.sc = fake_sc4
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-sles15sp7",
}, lab_file="lab.json")
names4 = [c[0] for c in fake_sc4.calls]
check("register_client(): key already exists, no local channel fields set -> checks whether "
      "it exists before assuming there's nothing to wait on",
      any(c[0] == "activation_key_exists" for c in fake_sc4.calls))
check("register_client(): looks up the key's REAL channels server-side via "
      "describe_activation_key() instead of trusting the (empty) local config",
      any(c[0] == "describe_activation_key" for c in fake_sc4.calls))
pending_call4 = next(c for c in fake_sc4.calls if c[0] == "pending_channels")
check("register_client(): the server-side base AND child channels both reach the "
      "pending_channels() check",
      set(pending_call4[1]) == {"sle-product-sles15-sp7-pool-x86_64", "managertools-sle15-pool-x86_64-sp7"})
check("register_client(): a still-pending real channel correctly defers to a background "
      "retry instead of registering straight into a protocol-version rejection",
      launch_calls == [("lab.json", "mercury.mydemo.lab")]
      and "ensure_client_registered" not in names4)

# -- register_client() refuses to launch a background retry without a lab_file --
fake_sc4 = _FakeSc(pending={"still-syncing"})
icr.sc = fake_sc4
died = False
try:
    _real_register_client("mercury.mydemo.lab", {
        "client_registration_server": "sol.mydemo.lab",
        "client_registration_activation_key": "1-key",
        "client_registration_activation_key_base_channel": "still-syncing",
    })  # lab_file omitted
except SystemExit:
    died = True
check("register_client(): dies with a clear message if asked to background-retry with no lab_file",
      died)


# ── _retry_until_registered(): delegates the actual forever-retry loop to the shared mechanism ──
# (libs/spacecmd_common.py's run_forever_retry_worker() — its own retry/heartbeat/crash-proofing
# correctness is covered end to end in 09_spacecmd_common_test.py). What belongs here is whether THIS
# function hands it the right key and the right attempt()/verify() closures.

# Succeeds on the very first attempt, and verify() (system_is_registered) agrees.
fake_sc5 = _FakeSc()
icr.sc = fake_sc5
icr._retry_until_registered("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
})
check("_retry_until_registered(): hands the forever-retry mechanism a key scoped to this exact node",
      ("run_forever_retry_worker:key", "client_registration:mercury.mydemo.lab") in fake_sc5.calls)
check("_retry_until_registered(): a clean attempt registers (ensure_client_registered called)",
      sum(1 for c in fake_sc5.calls if c[0] == "ensure_client_registered") == 1)
check("_retry_until_registered(): verify() really calls system_is_registered_and_healthy for this "
      "exact node", ("system_is_registered_and_healthy", "mercury.mydemo.lab") in fake_sc5.calls)

# Fails twice (once via SystemExit/die(), once via a plain exception), then succeeds on the third try —
# exercising that attempt() really is the ensure_spacecmd_config/wait_for_channels_synced/
# _register_now sequence, re-run whole on each pass, not something that only runs once.
attempt_count = [0]


class _FlakySc(_FakeSc):
    def ensure_client_registered(self, *a, **kw):
        attempt_count[0] += 1
        if attempt_count[0] == 1:
            raise SystemExit(1)
        if attempt_count[0] == 2:
            raise RuntimeError("transient spacecmd hiccup")
        self.calls.append(("ensure_client_registered", a, kw))


fake_sc6 = _FlakySc()
icr.sc = fake_sc6
icr._retry_until_registered("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
})
check("_retry_until_registered(): survives a SystemExit (die()) from one attempt and keeps retrying",
      attempt_count[0] == 3)
check("_retry_until_registered(): survives a plain exception too, and eventually succeeds",
      any(c[0] == "ensure_client_registered" for c in fake_sc6.calls))

# verify() rejecting even though attempt() itself raised nothing must still keep it retrying — the fix
# for a real failure mode found live: a registration call returning cleanly while the server's own
# system list still showed nothing registered.
_reg_calls = [0]


class _ScVerifyLagsBehind(_FakeSc):
    def ensure_client_registered(self, *a, **kw):
        _reg_calls[0] += 1
        self.calls.append(("ensure_client_registered", a, kw))

    def system_is_registered_and_healthy(self, hostname, exec_prefix, vm_name):
        self.calls.append(("system_is_registered_and_healthy", vm_name))
        return _reg_calls[0] >= 3  # only "really" shows up on the server after the 3rd attempt


fake_sc7 = _ScVerifyLagsBehind()
icr.sc = fake_sc7
icr._retry_until_registered("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
})
check("_retry_until_registered(): a clean ensure_client_registered() is not enough on its own — keeps "
      "retrying until system_is_registered_and_healthy() actually agrees too", _reg_calls[0] == 3)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all client_registration checks passed")
