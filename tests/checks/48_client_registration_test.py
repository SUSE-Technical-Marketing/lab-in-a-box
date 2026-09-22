#!/usr/bin/env python3
# Unit tests for the general per-node addon-config-override mechanism (added
# 2026-09-11): an addons[] entry is either a plain "<addon>" string, or a
# single-key {"<addon>": {...}} mapping overriding that addon's shared
# top-level config for that one node — e.g. a lab registering many OSes
# against one shared Uyuni/SMLM server, each node needing its own
# client_registration_activation_key. Covers the shared parsing helpers
# (apps.addon_entry_name/addon_entry_overrides), k8s.addon_nodes()/
# addon_node_config(), and install_client_registration.py's own use of
# them. register_client() itself is mocked (no real spacecmd/mgrctl). Run
# from 48_client_registration.sh, in its own container — see
# tests/run_tests.sh.
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


# ── install_client_registration.py: end-to-end through main() ──────────────
import install_client_registration as icr  # noqa: E402

_real_register_client = icr.register_client  # main() tests below stub this out

calls = []
icr.register_client = lambda vm_name, cfg, json_file=None: calls.append((vm_name, cfg))
icr.primary.load_definition = lambda path: cfg_def

old_argv = sys.argv
sys.argv = ["install_client_registration.py", "lab.json"]
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
sys.argv = ["install_client_registration.py", "lab.json"]
try:
    icr.main()
finally:
    del os.environ["_vm_name"]
    sys.argv = old_argv
check("install_client_registration main(): _vm_name env scopes to one node, override still applied",
      len(calls) == 1 and calls[0][0] == "callisto"
      and calls[0][1]["client_registration_activation_key"] == "1-debian13")


# ── register_client(): hands off to a background retry instead of blocking ──
# Added 2026-09-21 per explicit user requirement: a first version of this
# fix blocked synchronously (with a timeout) waiting for channels to sync
# — correctly flagged as a real problem, since with many nodes sharing the
# same still-syncing channel, each would wait out its own full timeout in
# turn, potentially adding hours to a deployment. Fixed: if the required
# channels aren't ALREADY fully synced, hand off to a DETACHED background
# worker (no artificial timeout, it only stops once registration actually
# succeeds) and return immediately so the rest of the deployment moves on.
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


launch_calls = []
icr._launch_background_retry = lambda json_file, vm_name: launch_calls.append((json_file, vm_name))

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
}, json_file="lab.json")
names = [c[0] for c in fake_sc.calls]
pending_call = next(c for c in fake_sc.calls if c[0] == "pending_channels")
check("register_client(): checks pending_channels() on the UNION of sync_channels + "
      "activation key base/child channels",
      set(pending_call[1]) == {"sles15-sp7-pool", "sles15-sp7-updates", "managertools-sles15-sp7-pool"})
check("register_client(): fully synced -> registers synchronously (ensure_client_registered called)",
      "ensure_client_registered" in names)
check("register_client(): fully synced -> no background retry launched", launch_calls == [])

# -- slow path: channels NOT yet fully synced -> hands off, returns without registering --
launch_calls.clear()
fake_sc2 = _FakeSc(pending={"sles15-sp7-pool"})
icr.sc = fake_sc2
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
    "client_registration_activation_key_base_channel": "sles15-sp7-pool",
}, json_file="lab.json")
names2 = [c[0] for c in fake_sc2.calls]
check("register_client(): channels still pending -> launches a background retry for this node",
      launch_calls == [("lab.json", "mercury.mydemo.lab")])
check("register_client(): channels still pending -> does NOT register synchronously "
      "(that's the whole point — don't block)",
      "ensure_client_registered" not in names2)

# -- no local channel fields AND the key doesn't exist yet (e.g. about to be
# created by ensure_activation_key right above) -> nothing to look up, registers synchronously --
launch_calls.clear()
fake_sc3 = _FakeSc(key_exists=False)
icr.sc = fake_sc3
_real_register_client("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
}, json_file="lab.json")
check("register_client(): no sync_channels/activation-key-channels configured, key doesn't "
      "exist yet -> nothing to wait on, registers synchronously without even calling "
      "pending_channels()",
      not any(c[0] == "pending_channels" for c in fake_sc3.calls)
      and any(c[0] == "ensure_client_registered" for c in fake_sc3.calls)
      and launch_calls == [])

# -- REAL BUG found live 2026-09-22 (solar-system-lab.json): the activation key
# was created by install_smlm.py's own smlm_activation_keys list, not this
# addon's own client_registration_activation_key_base_channel/_child_channels
# fields — so a per-node client_registration override that only names the key
# (the overwhelmingly common real shape) had NO local channel fields at all.
# _wait_channels() used to return an empty list in that case, short-circuiting
# straight to synchronous registration even while the key's real channels
# were still mid-reposync — confirmed live this is exactly why a client got
# the classic salt-minion instead of venv-salt-minion (its real providing
# channel's own bootstrap marker 404s until synced), and the hardened
# salt-master then rejected it outright ("protocol version 2, minimum
# required 3"). Fix: when the key already exists and no local fields are
# set, look up its REAL channels server-side via describe_activation_key().
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
}, json_file="lab.json")
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

# -- register_client() refuses to launch a background retry without a json_file --
fake_sc4 = _FakeSc(pending={"still-syncing"})
icr.sc = fake_sc4
died = False
try:
    _real_register_client("mercury.mydemo.lab", {
        "client_registration_server": "sol.mydemo.lab",
        "client_registration_activation_key": "1-key",
        "client_registration_activation_key_base_channel": "still-syncing",
    })  # json_file omitted
except SystemExit:
    died = True
check("register_client(): dies with a clear message if asked to background-retry with no json_file",
      died)


# ── _retry_until_registered(): the background worker's own retry-forever loop ──
icr.time.sleep = lambda s: None  # no real waiting in tests

# Succeeds on the very first attempt.
fake_sc5 = _FakeSc()
icr.sc = fake_sc5
icr._retry_until_registered("mercury.mydemo.lab", {
    "client_registration_server": "sol.mydemo.lab",
    "client_registration_activation_key": "1-key",
})
check("_retry_until_registered(): a clean first attempt registers and returns (no retry loop)",
      sum(1 for c in fake_sc5.calls if c[0] == "ensure_client_registered") == 1)

# Fails twice (once via SystemExit/die(), once via a plain exception), then succeeds on the third try.
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


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all client_registration checks passed")
