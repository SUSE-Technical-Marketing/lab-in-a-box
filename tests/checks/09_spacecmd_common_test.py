#!/usr/bin/env python3
# Mocked-SSH unit tests for libs/spacecmd_common.py — the
# shared activation-key/channel-sync helpers used by install_smlm.py and
# install_uyuni.py. No live SMLM/Uyuni server is available in this project;
# these verify the exact command strings issued (matching the syntax
# verified against live SUSE/Uyuni docs, 2026-08-27) rather than real
# server behavior. Run from 09_spacecmd_common.sh, in its own container —
# see tests/run_tests.sh.
import hashlib
import json
import re
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation  # noqa: E402
import spacecmd_common as sc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def unwrap(cmd):
    """
    For assertions written against the INNER remote command's own quoting
    (e.g. a JSON arg already shlex-quoted by _api_call), undo the outer
    re-quoting _run() now applies for an mgrctl-style exec_prefix (see its
    docstring — mgrctl needs the whole remote command as ONE argument,
    unlike kubectl's ---terminated argv, so a remote_cmd that already
    contains its own single-quoted values gets those quotes escaped as
    '"'"' when re-wrapped). No-op for a kubectl-shaped command, which was
    never re-wrapped this way.
    """
    return cmd.replace('\'"\'"\'', "'")


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeSSH:
    """Records every command issued via ssh_run and returns scripted output
    keyed by a substring match, so tests can assert on exact command shape
    without a real host."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []  # list of (substring, FakeResult)

    def __call__(self, hostname, cmd, **kwargs):
        self.calls.append((hostname, cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeResult()


# -- ensure_spacecmd_config: writes a config file, credentials never on argv -
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_spacecmd_config("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "admin", "s3cr3t")
check("ensure_spacecmd_config: exactly one ssh_run call", len(fake.calls) == 1)
_, cmd, kwargs = fake.calls[0]
check("ensure_spacecmd_config: uses the given exec_prefix",
      cmd.startswith("kubectl exec ") and "-n ns deploy/uyuni -c uyuni" in cmd)
check("ensure_spacecmd_config: writes to ~/.spacecmd/config", "~/.spacecmd/config" in cmd)
check("ensure_spacecmd_config: password passed via stdin, not argv", "s3cr3t" not in cmd and kwargs.get("input_text") and "s3cr3t" in kwargs["input_text"])
check("ensure_spacecmd_config: kubectl exec gets -i since input_text is given (confirmed live: "
      "neither mgrctl nor kubectl exec forward stdin without it)", " -i " in " {} ".format(cmd))
check("ensure_spacecmd_config: server is always localhost, never a caller-supplied FQDN "
      "(confirmed live: the exec'd container/pod can't reach its own external hostname over "
      "HTTP)", "server=localhost" in kwargs.get("input_text", ""))

# Without input_text, no -i is added (kubectl exec -- stays exactly as given).
fake = FakeSSH()
sc.ssh_run = fake
sc._run("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "spacecmd -- activationkey_list")
check("_run: no -i added for a kubectl call with no input_text",
      " -i " not in " {} ".format(fake.calls[0][1]))

# mgrctl exec also gets -i when input_text is given, and NOT otherwise.
fake = FakeSSH()
sc.ssh_run = fake
sc._run("host1", "mgrctl exec --", "cat > ~/.spacecmd/config", input_text="[spacecmd]\n")
check("_run: mgrctl exec gets -i when input_text is given", "mgrctl exec -i " in fake.calls[0][1])

fake = FakeSSH()
sc.ssh_run = fake
sc._run("host1", "mgrctl exec --", "spacecmd -- activationkey_list")
check("_run: mgrctl exec does NOT get -i when there's no input_text",
      "mgrctl exec -i " not in fake.calls[0][1] and fake.calls[0][1].startswith("mgrctl exec '"))

# -- _api_call: always needs "--" before "api" ("channel.access.setOrgSharing" -----
# confirmed live, 2026-08-28: `spacecmd api -A ... method` (no "--") is
# rejected outright by spacecmd's own argument parser — "unrecognized
# arguments: -A [...] method" — for both a 1-arg and a 2-arg call. Only
# `spacecmd -- api -A ... method` (matching _spacecmd()'s own pattern)
# actually works.
fake = FakeSSH()
sc.ssh_run = fake
sc._api_call("host1", "mgrctl exec --", "channel.access.setOrgSharing", ["cutovertest-base", "protected"])
cmd = unwrap(fake.calls[0][1])
check("_api_call: includes the '--' separator before 'api', matching _spacecmd()",
      "spacecmd -- api -A" in cmd)
check("_api_call: 2-arg call encodes both values as a JSON array",
      '["cutovertest-base", "protected"]' in cmd)

fake = FakeSSH()
sc.ssh_run = fake
sc._api_call("host1", "mgrctl exec --", "saltkey.acceptedList", [])
check("_api_call: 0-arg call also gets the '--' separator",
      "spacecmd -- api -A" in unwrap(fake.calls[0][1]) and "saltkey.acceptedList" in unwrap(fake.calls[0][1]))

# -- activation_key_exists ----------------------------------------------------
fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout="1-mykey\n1-otherkey\n"))])
sc.ssh_run = fake
check("activation_key_exists: found", sc.activation_key_exists("host1", "mgrctl exec --", "1-mykey") is True)
check("activation_key_exists: not found", sc.activation_key_exists("host1", "mgrctl exec --", "1-nope") is False)

# -- resolve_activation_key_name ----------------------------------------------
# (confirmed live, 2026-08-28: activationkey_create's -n flag does not use
# the given name verbatim — Uyuni always auto-prepends the org id, even
# onto a name that already looked pre-prefixed: "-n 1-dev-key" was stored
# as "1-1-dev-key", not "1-dev-key" — breaking every exact-match follow-up
# command against the caller's original value.)
fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout="1-1-dev-key\n1-1-qa-key\n"))])
sc.ssh_run = fake
check("resolve_activation_key_name: resolves a doubly-prefixed name via suffix match",
      sc.resolve_activation_key_name("host1", "mgrctl exec --", "1-dev-key") == "1-1-dev-key")

fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout="1-mykey\n"))])
sc.ssh_run = fake
check("resolve_activation_key_name: an already-exact name is returned unchanged",
      sc.resolve_activation_key_name("host1", "mgrctl exec --", "1-mykey") == "1-mykey")

fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout=""))])
sc.ssh_run = fake
check("resolve_activation_key_name: falls back to the given name when no line matches",
      sc.resolve_activation_key_name("host1", "mgrctl exec --", "1-nope") == "1-nope")

# -- ensure_activation_key: no-op when unset ---------------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_activation_key("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_activation_key: no-op when <prefix>_activation_key unset", len(fake.calls) == 0)

# -- ensure_activation_key: already exists -> no create ----------------------
fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout="1-mykey\n"))])
sc.ssh_run = fake
sc.ensure_activation_key("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
check("ensure_activation_key: existing key -> only the list check ran, no create",
      len(fake.calls) == 1 and "activationkey_create" not in fake.calls[0][1])

# -- ensure_activation_key: missing base channel dies ------------------------
fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_activation_key("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
except SystemExit:
    died = True
check("ensure_activation_key: dies without a base channel", died)

# -- ensure_activation_key: full creation + every follow-up command ----------
fake = FakeSSH(responses=[("activationkey_list", FakeResult(stdout=""))])
sc.ssh_run = fake
cfg = {
    "smlm_activation_key": "1-mykey",
    "smlm_activation_key_desc": "my lab key",
    "smlm_activation_key_base_channel": "sle-product-base",
    "smlm_activation_key_child_channels": "child-a child-b",
    "smlm_activation_key_universal_default": "true",
    "smlm_activation_key_entitlements": "enterprise_entitled,virtualization_host",
    "smlm_activation_key_config_channels": "cfg-a",
    "smlm_activation_key_enable_config_deployment": "true",
    "smlm_activation_key_groups": "group-a group-b",
    "smlm_activation_key_contact_method": "default",
}
sc.ensure_activation_key("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("full flow: activationkey_list checked first", "activationkey_list" in cmds[0])
create_cmd = next((c for c in cmds if "activationkey_create" in c), "")
check("create: name/description/base-channel flags present",
      "-n 1-mykey" in create_cmd and "-d 'my lab key'" in create_cmd and "-b sle-product-base" in create_cmd)
check("create: universal-default flag present", " -u" in create_cmd)
check("create: entitlements flag present", "-e enterprise_entitled,virtualization_host" in create_cmd)
check("follow-up: child channels added", any("activationkey_addchildchannels 1-mykey child-a child-b" in c for c in cmds))
check("follow-up: config channels added", any("activationkey_addconfigchannels 1-mykey cfg-a" in c for c in cmds))
check("follow-up: config deployment enabled", any("activationkey_enableconfigdeployment 1-mykey" in c for c in cmds))
check("follow-up: groups added", any("activationkey_addgroups 1-mykey group-a group-b" in c for c in cmds))
check("follow-up: contact method set", any("activationkey_setcontactmethod 1-mykey default" in c for c in cmds))

# -- ensure_activation_key: a failed child-channel link now surfaces, not --
# -- silently swallowed -------------------------------------------------------
# Real bug found live 2026-09-14: activationkey_addchildchannels genuinely
# fails ("Invalid channel") whenever a listed child channel (e.g. the
# "managertools-*" channels that provide venv-salt-minion) was never
# actually added to the server — easy to do, since nothing else implies
# syncing it just because an activation key references it. This call's own
# return code used to be discarded outright, so the failure never surfaced
# anywhere: the activation key looked fine, but clients bootstrapped
# against it silently got the wrong (unlinked) channel set.
fake = FakeSSH(responses=[
    ("activationkey_list", FakeResult(stdout="")),
    ("activationkey_addchildchannels", FakeResult(returncode=1, stderr="Invalid channel")),
])
sc.ssh_run = fake
warned = []
sc.warn = lambda m: warned.append(m)
sc.ensure_activation_key("host1", "mgrctl exec --", {
    "smlm_activation_key": "1-mykey",
    "smlm_activation_key_base_channel": "sle-product-base",
    "smlm_activation_key_child_channels": "managertools-sle15-pool-x86_64-sp7",
}, "smlm")
check("ensure_activation_key: a failed child-channel link now calls warn(), not silently ignored",
      len(warned) == 1)
check("ensure_activation_key: the warning names the actual channel and the real server error",
      warned and "managertools-sle15-pool-x86_64-sp7" in warned[0] and "Invalid channel" in warned[0])

# -- ensure_activation_key: follow-ups use the REAL (org-id-prefixed) name --
# (confirmed live, 2026-08-28: creating "-n 1-otherkey" was actually stored
# as "1-1-otherkey" — every follow-up command must target that real name,
# not the caller's original config value, or it fails with "Activation Key
# [...] Not Found!")
calls = []
list_call_count = [0]


def _fake_ssh_prefix_bug(hostname, cmd, **kwargs):
    calls.append((hostname, cmd, kwargs))
    if "activationkey_list" in cmd:
        list_call_count[0] += 1
        # 1st call: activation_key_exists' pre-creation check -> not found yet.
        # 2nd call: resolve_activation_key_name, right after creation.
        stdout = "" if list_call_count[0] == 1 else "1-1-otherkey\n"
        return FakeResult(returncode=0, stdout=stdout)
    return FakeResult(returncode=0)


sc.ssh_run = _fake_ssh_prefix_bug
sc.ensure_activation_key("host1", "mgrctl exec --", {
    "uyuni_activation_key": "1-otherkey",
    "uyuni_activation_key_base_channel": "base",
    "uyuni_activation_key_groups": "group-a",
}, "uyuni")
cmds = [c[1] for c in calls]
check("ensure_activation_key: follow-up targets the resolved real name, not the given one",
      any("activationkey_addgroups 1-1-otherkey group-a" in c for c in cmds)
      and not any("activationkey_addgroups 1-otherkey " in c for c in cmds))

# -- ensure_channels_synced: skips already-present, syncs the rest ----------
fake = FakeSSH(responses=[("softwarechannel_list", FakeResult(stdout="already-here\n"))])
sc.ssh_run = fake
sc.ensure_channels_synced("host1", "mgrctl exec --", ["already-here", "needs-sync"])
sync_cmds = [c[1] for c in fake.calls if "mgr-sync add channel" in c[1]]
check("ensure_channels_synced: only the missing channel is synced",
      len(sync_cmds) == 1 and "needs-sync" in sync_cmds[0])
check("ensure_channels_synced: one channel per call (singular form)",
      "add channel needs-sync" in sync_cmds[0] and "add channels" not in sync_cmds[0])

check("ensure_channels_synced: no-op on empty list",
      not FakeSSH().calls or True)  # trivially true; real check below
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_channels_synced("host1", "mgrctl exec --", [])
check("ensure_channels_synced: truly no-op on empty list", len(fake.calls) == 0)

# -- wait_for_channels_synced: real completion detection + timeout ----------
# Added 2026-09-21 per explicit user requirement: registration scripts must
# wait for channels to be genuinely, fully synced (not just "exists"),
# reusing the exact reposync-log "Sync completed." signal already
# ground-truthed in install_smlm.py's own channel-sync monitor.
sc.time.sleep = lambda s: None  # no real waiting in tests

fake = FakeSSH(responses=[("tail -n 3", FakeResult(returncode=0, stdout="...\nSync completed.\n"))])
sc.ssh_run = fake
sc.wait_for_channels_synced("host1", "mgrctl exec --", ["ch-a", "ch-b"])
check("wait_for_channels_synced: checks the real reposync log path for each channel",
      any("/var/log/rhn/reposync/ch-a.log" in c[1] for c in fake.calls)
      and any("/var/log/rhn/reposync/ch-b.log" in c[1] for c in fake.calls))

fake = FakeSSH()
sc.ssh_run = fake
sc.wait_for_channels_synced("host1", "mgrctl exec --", [])
check("wait_for_channels_synced: no-op on an empty channel list, no SSH calls at all", len(fake.calls) == 0)

# A channel that never completes -> dies once the timeout deadline passes.
fake = FakeSSH(responses=[("tail -n 3", FakeResult(returncode=1, stdout=""))])
sc.ssh_run = fake
_now = [1000.0]
sc.time.time = lambda: _now[0]


def _fake_sleep_advance(seconds):
    _now[0] += 3600  # jump well past any real deadline, no actual waiting


sc.time.sleep = _fake_sleep_advance
died = False
try:
    sc.wait_for_channels_synced("host1", "mgrctl exec --", ["stuck-channel"], timeout=60, poll_interval=5)
except SystemExit:
    died = True
check("wait_for_channels_synced: dies with a clear message once the timeout is exceeded, "
      "rather than hanging forever", died)
sc.time.time = time.time
sc.time.sleep = time.sleep

# -- pending_channels: the one-shot, non-blocking check factored out above --
fake = FakeSSH(responses=[
    ("reposync/ready.log", FakeResult(returncode=0, stdout="...\nSync completed.\n")),
    ("reposync/not-ready.log", FakeResult(returncode=1, stdout="")),
])
sc.ssh_run = fake
pending = sc.pending_channels("host1", "mgrctl exec --", ["ready", "not-ready"])
check("pending_channels: returns only the channel(s) NOT yet showing a completed sync",
      pending == {"not-ready"})

fake = FakeSSH(responses=[("tail -n 3", FakeResult(returncode=0, stdout="...\nSync completed.\n"))])
sc.ssh_run = fake
check("pending_channels: an empty result means every channel is genuinely ready",
      sc.pending_channels("host1", "mgrctl exec --", ["a", "b"]) == set())

check("pending_channels: an empty input list returns an empty result, no SSH calls needed",
      sc.pending_channels("host1", "mgrctl exec --", []) == set())

# wait_for_channels_synced(timeout=None) waits forever — verify it actually keeps polling
# instead of dying immediately, then completes once the channel becomes ready.
poll_count = [0]


def _completes_on_third_poll(hostname, cmd, **kwargs):
    poll_count[0] += 1
    if poll_count[0] >= 3:
        return FakeResult(returncode=0, stdout="...\nSync completed.\n")
    return FakeResult(returncode=1, stdout="")


sc.ssh_run = _completes_on_third_poll
sc.time.sleep = lambda s: None
sc.wait_for_channels_synced("host1", "mgrctl exec --", ["slow-channel"], timeout=None, poll_interval=1)
check("wait_for_channels_synced: timeout=None polls indefinitely and returns once genuinely ready",
      poll_count[0] >= 3)
sc.time.sleep = time.sleep

# -- ensure_appstreams: no-op when key or appstreams unset -------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_appstreams("host1", "mgrctl exec --", {}, "uyuni")
sc.ensure_appstreams("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
check("ensure_appstreams: no-op when key or appstreams field is unset", len(fake.calls) == 0)

# -- ensure_appstreams: success, one call per module:stream pair, right JSON -
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_appstreams("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --",
                      {"smlm_activation_key": "1-mykey", "smlm_activation_key_appstreams": "nodejs:20 postgresql:16"},
                      "smlm")
check("ensure_appstreams: resolves the real key name once, then one api call per pair",
      len(fake.calls) == 3 and "activationkey_list" in fake.calls[0][1])
cmds = [c[1] for c in fake.calls if "addAppStreams" in c[1]]
check("ensure_appstreams: uses the api passthrough with activationkey.addAppStreams",
      len(cmds) == 2 and all("spacecmd -- api -A" in c and c.endswith("activationkey.addAppStreams") for c in cmds))
check("ensure_appstreams: JSON args carry key name + module/stream struct",
      any('["1-mykey", [{"module": "nodejs", "stream": "20"}]]' in c for c in cmds))

# -- ensure_appstreams: already-enabled fault is treated as success ----------
fake = FakeSSH(responses=[("addAppStreams", FakeResult(
    returncode=1, stderr="App stream 'nodejs' already exists in the activation key."))])
sc.ssh_run = fake
sc.ensure_appstreams("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey",
                                                  "uyuni_activation_key_appstreams": "nodejs:20"}, "uyuni")
check("ensure_appstreams: duplicate-fault treated as already-satisfied (no die)", True)  # would have raised otherwise

# -- ensure_appstreams: any other failure dies -------------------------------
fake = FakeSSH(responses=[("addAppStreams", FakeResult(returncode=1, stderr="no such module 'bogus'"))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_appstreams("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey",
                                                      "uyuni_activation_key_appstreams": "bogus:1"}, "uyuni")
except SystemExit:
    died = True
check("ensure_appstreams: a non-duplicate failure dies", died)

# -- ensure_appstreams: malformed "module:stream" entry dies ----------------
fake = FakeSSH()
sc.ssh_run = fake
died = False
try:
    sc.ensure_appstreams("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey",
                                                      "uyuni_activation_key_appstreams": "nodejs-no-colon"}, "uyuni")
except SystemExit:
    died = True
check("ensure_appstreams: entry without ':' dies before issuing any command", died and len(fake.calls) == 0)

# -- activation_key_packages / ensure_activation_key_packages ----------------
fake = FakeSSH(responses=[("activationkey_listpackages", FakeResult(returncode=0, stdout="nodejs\npostgresql\n"))])
sc.ssh_run = fake
check("activation_key_packages: returns the current set",
      sc.activation_key_packages("host1", "mgrctl exec --", "1-mykey") == {"nodejs", "postgresql"})

fake = FakeSSH(responses=[("activationkey_listpackages", FakeResult(returncode=1, stderr="no such key"))])
sc.ssh_run = fake
check("activation_key_packages: returns empty set on failure",
      sc.activation_key_packages("host1", "mgrctl exec --", "bogus") == set())

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_activation_key_packages("host1", "mgrctl exec --", {}, "uyuni")
sc.ensure_activation_key_packages("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
check("ensure_activation_key_packages: no-op when key or packages field is unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[("activationkey_listpackages", FakeResult(returncode=0, stdout="nodejs\npostgresql\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_packages(
    "host1", "mgrctl exec --",
    {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_packages": "nodejs postgresql"}, "uyuni")
check("ensure_activation_key_packages: all already present -> no addpackages call",
      len(fake.calls) == 2 and not any("activationkey_addpackages" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("activationkey_listpackages", FakeResult(returncode=0, stdout="nodejs\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_packages(
    "host1", "kubectl exec -n ns deploy/uyuni -c uyuni --",
    {"smlm_activation_key": "1-mykey", "smlm_activation_key_packages": "nodejs postgresql curl"}, "smlm")
add_cmd = next((c[1] for c in fake.calls if "activationkey_addpackages" in c[1]), "")
check("ensure_activation_key_packages: adds only the missing packages",
      "activationkey_addpackages 1-mykey postgresql curl" in add_cmd)

fake = FakeSSH(responses=[
    ("activationkey_listpackages", FakeResult(returncode=0, stdout="")),
    ("activationkey_addpackages", FakeResult(returncode=1, stderr="no such package")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_activation_key_packages(
        "host1", "mgrctl exec --",
        {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_packages": "bogus-pkg"}, "uyuni")
except SystemExit:
    died = True
check("ensure_activation_key_packages: addpackages failure dies", died)

# -- config_channel_exists / ensure_config_channel_exists --------------------
fake = FakeSSH(responses=[("configchannel_list", FakeResult(stdout="web-config\nother-chan\n"))])
sc.ssh_run = fake
check("config_channel_exists: found", sc.config_channel_exists("host1", "mgrctl exec --", "web-config") is True)
check("config_channel_exists: not found", sc.config_channel_exists("host1", "mgrctl exec --", "nope") is False)

fake = FakeSSH(responses=[("configchannel_list", FakeResult(stdout="web-config\n"))])
sc.ssh_run = fake
sc.ensure_config_channel_exists("host1", "mgrctl exec --", "web-config", "webconfig-name", "desc")
check("ensure_config_channel_exists: existing channel -> no create call",
      len(fake.calls) == 1 and "configchannel_create" not in fake.calls[0][1])

fake = FakeSSH(responses=[("configchannel_list", FakeResult(stdout=""))])
sc.ssh_run = fake
sc.ensure_config_channel_exists("host1", "mgrctl exec --", "web-config", "webconfig-name", "desc", "state")
create_cmd = next((c[1] for c in fake.calls if "configchannel_create" in c[1]), "")
check("ensure_config_channel_exists: create command carries -n/-l/-d/-t",
      "-n webconfig-name" in create_cmd and "-l web-config" in create_cmd
      and "-d desc" in create_cmd and "-t state" in create_cmd)

# -- ensure_config_file: matching sha256 -> skip, no addfile/stage calls -----
content = "server { listen 80; }\n"
digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
fake = FakeSSH(responses=[("configchannel_filedetails", FakeResult(returncode=0, stdout="sha256: {}".format(digest)))])
sc.ssh_run = fake
sc.ensure_config_file("host1", "mgrctl exec --", "web-config", "/etc/nginx/nginx.conf", content)
check("ensure_config_file: matching sha256 -> skip, no addfile call",
      len(fake.calls) == 1 and "configchannel_addfile" not in fake.calls[0][1])

# -- ensure_config_file: content differs -> stage, addfile, cleanup ----------
fake = FakeSSH(responses=[("configchannel_filedetails", FakeResult(returncode=0, stdout="sha256: stale"))])
sc.ssh_run = fake
sc.ensure_config_file("host1", "mgrctl exec --", "web-config", "/etc/nginx/nginx.conf", content,
                       owner="root", group="root", mode="0644")
cmds = [c[1] for c in fake.calls]
check("ensure_config_file: stages content, adds the file, then cleans up",
      any("cat >" in c for c in cmds)
      and any("configchannel_addfile -c web-config -p /etc/nginx/nginx.conf" in c for c in cmds)
      and any("rm -f" in c for c in cmds))
check("ensure_config_file: owner/group/mode flags present",
      any("-o root" in c and "-g root" in c and "-m 0644" in c for c in cmds if "configchannel_addfile" in c))
stage_call = next(c for c in fake.calls if "cat >" in c[1])
check("ensure_config_file: content passed via input_text stdin, not argv",
      stage_call[2].get("input_text") == content and content not in stage_call[1])

# -- ensure_init_sls: matching sha256 -> skip; mismatch -> updateinitsls ----
init_content = "include:\n  - my.state\n"
init_digest = hashlib.sha256(init_content.encode("utf-8")).hexdigest()
fake = FakeSSH(responses=[("configchannel_filedetails",
                            FakeResult(returncode=0, stdout="sha256: {}".format(init_digest)))])
sc.ssh_run = fake
sc.ensure_init_sls("host1", "mgrctl exec --", "app-state", init_content)
check("ensure_init_sls: matching sha256 -> skip", len(fake.calls) == 1)

fake = FakeSSH(responses=[("configchannel_filedetails", FakeResult(returncode=0, stdout="sha256: stale"))])
sc.ssh_run = fake
sc.ensure_init_sls("host1", "mgrctl exec --", "app-state", init_content)
cmds = [c[1] for c in fake.calls]
check("ensure_init_sls: uses configchannel_updateinitsls, not addfile",
      any("configchannel_updateinitsls -c app-state" in c for c in cmds)
      and not any("configchannel_addfile" in c for c in cmds))

# -- ensure_config_channels: no-op when field is unset -----------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_config_channels("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_config_channels: no-op when field unset", len(fake.calls) == 0)

# -- ensure_config_channels: full orchestration (normal + state channel) ----
fake = FakeSSH(responses=[
    ("configchannel_list", FakeResult(stdout="")),
    ("configchannel_filedetails", FakeResult(returncode=1)),
])
sc.ssh_run = fake
cfg = {
    "smlm_config_channels": [
        {"label": "web-config", "name": "webconfig-name", "files": [
            {"path": "/etc/nginx/nginx.conf", "content": "server {}\n"},
        ]},
        {"label": "app-state", "type": "state", "init_sls": "include:\n  - x\n"},
    ]
}
sc.ensure_config_channels("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_config_channels: creates both channels",
      sum(1 for c in cmds if "configchannel_create" in c) == 2)
check("ensure_config_channels: pushes the normal channel's file",
      any("configchannel_addfile -c web-config" in c for c in cmds))
check("ensure_config_channels: pushes the state channel's init.sls",
      any("configchannel_updateinitsls -c app-state" in c for c in cmds))

# -- ensure_config_channels: validation dies on missing required fields -----
died = False
try:
    sc.ensure_config_channels("host1", "mgrctl exec --", {"uyuni_config_channels": [{"name": "no label"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_config_channels: entry missing 'label' dies", died)

fake = FakeSSH(responses=[("configchannel_list", FakeResult(stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_config_channels("host1", "mgrctl exec --",
                               {"uyuni_config_channels": [{"label": "x", "files": [{"content": "no path"}]}]},
                               "uyuni")
except SystemExit:
    died = True
check("ensure_config_channels: file entry missing 'path' dies", died)

fake = FakeSSH(responses=[("configchannel_list", FakeResult(stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_config_channels("host1", "mgrctl exec --",
                               {"uyuni_config_channels": [{"label": "x", "files": [{"path": "/f"}]}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_config_channels: file entry missing 'content' dies", died)

# -- org_exists: exact-line match, not substring ------------------------------
fake = FakeSSH(responses=[("org_list", FakeResult(stdout="lab\nOrgB\n"))])
sc.ssh_run = fake
check("org_exists: found (exact line match)", sc.org_exists("host1", "mgrctl exec --", "lab") is True)
check("org_exists: not found (no partial/substring match)", sc.org_exists("host1", "mgrctl exec --", "la") is False)

# -- ensure_org: existing org -> no create call ------------------------------
fake = FakeSSH(responses=[("org_list", FakeResult(stdout="lab\n"))])
sc.ssh_run = fake
sc.ensure_org("host1", "mgrctl exec --", {"name": "lab"})
check("ensure_org: existing org -> no create call",
      len(fake.calls) == 1 and "org_create " not in fake.calls[0][1])

# -- ensure_org: missing admin fields dies -----------------------------------
fake = FakeSSH(responses=[("org_list", FakeResult(stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_org("host1", "mgrctl exec --", {"name": "OrgB"})
except SystemExit:
    died = True
check("ensure_org: missing admin_user/admin_pass/admin_email dies", died)

# -- ensure_org: full creation, every flag present ---------------------------
fake = FakeSSH(responses=[("org_list", FakeResult(stdout=""))])
sc.ssh_run = fake
sc.ensure_org("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", {
    "name": "OrgB", "admin_user": "orgb-admin", "admin_pass": "s3cr3t",
    "admin_email": "admin@orgb.lab", "admin_first_name": "Org", "admin_last_name": "Bee",
    "prefix": "mr", "pam": True,
})
create_cmd = next((c[1] for c in fake.calls if "org_create " in c[1]), "")
check("ensure_org: create command carries -n/-u/-f/-l/-e/-p/-P/--pam",
      "-n OrgB" in create_cmd and "-u orgb-admin" in create_cmd and "-f Org" in create_cmd
      and "-l Bee" in create_cmd and "-e admin@orgb.lab" in create_cmd and "-p s3cr3t" in create_cmd
      and "-P mr" in create_cmd and "--pam" in create_cmd)

# -- ensure_org: defaults first/last name, omits -P/--pam when unset --------
fake = FakeSSH(responses=[("org_list", FakeResult(stdout=""))])
sc.ssh_run = fake
sc.ensure_org("host1", "mgrctl exec --", {"name": "OrgC", "admin_user": "orgc-admin",
                                           "admin_pass": "pw", "admin_email": "a@b.c"})
create_cmd = next((c[1] for c in fake.calls if "org_create " in c[1]), "")
check("ensure_org: defaults first/last name from admin_user/org name when unset",
      "-f orgc-admin" in create_cmd and "-l OrgC" in create_cmd
      and "-P" not in create_cmd and "--pam" not in create_cmd)

# -- ensure_org_trust: skip if already trusted, add otherwise ---------------
fake = FakeSSH(responses=[("org_listtrusts", FakeResult(stdout="lab\nOrgC\n"))])
sc.ssh_run = fake
sc.ensure_org_trust("host1", "mgrctl exec --", "OrgB", "lab")
check("ensure_org_trust: already trusted -> no addtrust call",
      len(fake.calls) == 1 and "org_addtrust" not in fake.calls[0][1])

fake = FakeSSH(responses=[("org_listtrusts", FakeResult(stdout=""))])
sc.ssh_run = fake
sc.ensure_org_trust("host1", "mgrctl exec --", "OrgB", "lab")
check("ensure_org_trust: not yet trusted -> addtrust called",
      any("org_addtrust OrgB lab" in c[1] for c in fake.calls))

# -- ensure_channel_sharing: validation, skip-if-set, set-if-not ------------
died = False
try:
    sc.ensure_channel_sharing("host1", "mgrctl exec --", "chan1", "bogus")
except SystemExit:
    died = True
check("ensure_channel_sharing: invalid access level dies", died)

fake = FakeSSH(responses=[("getOrgSharing", FakeResult(returncode=0, stdout="protected"))])
sc.ssh_run = fake
sc.ensure_channel_sharing("host1", "mgrctl exec --", "chan1", "protected")
check("ensure_channel_sharing: already-set access -> no setOrgSharing call",
      len(fake.calls) == 1 and "setOrgSharing" not in fake.calls[0][1])

fake = FakeSSH(responses=[("getOrgSharing", FakeResult(returncode=0, stdout="private"))])
sc.ssh_run = fake
sc.ensure_channel_sharing("host1", "mgrctl exec --", "chan1", "protected")
set_cmd = next((c[1] for c in fake.calls if "setOrgSharing" in c[1]), "")
check("ensure_channel_sharing: mismatched access -> setOrgSharing called with right JSON",
      "api -A" in set_cmd and '["chan1", "protected"]' in unwrap(set_cmd)
      and "channel.access.setOrgSharing" in set_cmd)

# -- ensure_orgs: no-op when field unset -------------------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_orgs("host1", "mgrctl exec --", {}, "uyuni", "admin", "pw")
check("ensure_orgs: no-op when field unset", len(fake.calls) == 0)

# -- ensure_orgs: full orchestration -----------------------------------------
# OrgB doesn't exist yet (needs admin creds to create); OrgC already exists
# (org_create requires an admin at creation time, so "no admin creds" is
# only ever valid for an already-existing org).
fake = FakeSSH(responses=[
    ("org_listtrusts", FakeResult(stdout="")),
    ("org_list", FakeResult(stdout="OrgC\n")),
    ("getOrgSharing", FakeResult(returncode=0, stdout="private")),
    ("activationkey_list", FakeResult(stdout="")),
])
sc.ssh_run = fake
cfg = {
    "smlm_orgs": [
        {
            "name": "OrgB", "admin_user": "orgb-admin", "admin_pass": "pw", "admin_email": "a@b.c",
            "trust_with": ["lab"], "share_channels": ["base-channel"],
            "smlm_activation_key": "1-orgb-key", "smlm_activation_key_base_channel": "base-channel",
        },
        # already exists, no admin_user/admin_pass -> its own key must NOT be provisioned
        {"name": "OrgC", "smlm_activation_key": "1-orgc-key", "smlm_activation_key_base_channel": "base-channel"},
    ]
}
sc.ensure_orgs("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm", "admin", "admin123")
cmds = [c[1] for c in fake.calls]
check("ensure_orgs: creates the missing org, leaves the existing one alone",
      sum(1 for c in cmds if "org_create " in c) == 1)
check("ensure_orgs: trust_with triggers org_addtrust", any("org_addtrust OrgB lab" in c for c in cmds))
check("ensure_orgs: share_channels triggers setOrgSharing", any("setOrgSharing" in c for c in cmds))
check("ensure_orgs: org with admin creds gets its own activation key created",
      any("activationkey_create" in c and "1-orgb-key" in c for c in cmds))
check("ensure_orgs: org without admin creds skips its own scoped provisioning",
      not any("1-orgc-key" in c for c in cmds))
last_call = fake.calls[-1]
check("ensure_orgs: restores the default admin session before returning",
      "cat > ~/.spacecmd/config" in last_call[1]
      and "username=admin\npassword=admin123" in (last_call[2].get("input_text") or ""))

# -- ensure_orgs: entry missing 'name' dies ----------------------------------
died = False
try:
    sc.ensure_orgs("host1", "mgrctl exec --", {"uyuni_orgs": [{"admin_user": "x"}]}, "uyuni", "admin", "pw")
except SystemExit:
    died = True
check("ensure_orgs: entry missing 'name' dies", died)

# -- user_exists / ensure_user / ensure_users --------------------------------
fake = FakeSSH(responses=[("user_list", FakeResult(returncode=0, stdout="admin\nalice\n"))])
sc.ssh_run = fake
check("user_exists: found", sc.user_exists("host1", "mgrctl exec --", "alice") is True)
check("user_exists: not found", sc.user_exists("host1", "mgrctl exec --", "bob") is False)

# Existing user -> no user_create call, but roles are still (idempotently) applied.
fake = FakeSSH(responses=[
    ("user_list", FakeResult(returncode=0, stdout="alice\n")),
    ("user_details", FakeResult(returncode=0, stdout="Roles: org_admin")),
])
sc.ssh_run = fake
sc.ensure_user("host1", "mgrctl exec --", {"username": "alice", "roles": ["org_admin"]})
cmds = [c[1] for c in fake.calls]
check("ensure_user: existing user -> no user_create call", not any("user_create" in c for c in cmds))
check("ensure_user: already-held role -> no user_addrole call", not any("user_addrole" in c for c in cmds))

# Missing user, all required fields present -> created, then roles granted.
fake = FakeSSH(responses=[
    ("user_list", FakeResult(returncode=0, stdout="")),
    ("user_details", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
sc.ensure_user("host1", "mgrctl exec --", {
    "username": "curie", "password": "pw", "first_name": "Marie", "last_name": "Curie",
    "email": "curie@edge.mydemo.lab", "roles": ["channel_admin"],
})
cmds = [c[1] for c in fake.calls]
check("ensure_user: missing user -> user_create called with the right argv",
      any("user_create -u curie -p pw -f Marie -l Curie -e curie@edge.mydemo.lab" in c for c in cmds))
check("ensure_user: no --pam flag when not requested",
      not any("--pam" in c for c in cmds))
check("ensure_user: roles granted after creation",
      any("user_addrole curie channel_admin" in c for c in cmds))

# pam flag appended when set.
fake = FakeSSH(responses=[("user_list", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_user("host1", "mgrctl exec --", {
    "username": "turing", "password": "pw", "first_name": "Alan", "last_name": "Turing",
    "email": "turing@edge.mydemo.lab", "pam": True,
})
check("ensure_user: --pam appended when requested",
      any("-e turing@edge.mydemo.lab --pam" in c[1] for c in fake.calls))

# Missing user, a required field absent -> dies rather than silently skip.
fake = FakeSSH(responses=[("user_list", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_user("host1", "mgrctl exec --", {"username": "incomplete", "password": "pw"})
except SystemExit:
    died = True
check("ensure_user: missing user + missing required field dies", died)

# Entry missing 'username' dies.
died = False
try:
    sc.ensure_user("host1", "mgrctl exec --", {"password": "pw"})
except SystemExit:
    died = True
check("ensure_user: entry missing 'username' dies", died)

# ensure_users: orchestrates a list, no-op when unset.
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_users("host1", "mgrctl exec --", {}, "smlm")
check("ensure_users: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[
    ("user_list", FakeResult(returncode=0, stdout="")),
    ("user_details", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {
    "smlm_users": [
        {"username": "curie", "password": "pw", "first_name": "Marie", "last_name": "Curie",
         "email": "curie@edge.mydemo.lab", "roles": ["channel_admin"]},
        {"username": "turing", "password": "pw", "first_name": "Alan", "last_name": "Turing",
         "email": "turing@edge.mydemo.lab", "roles": ["config_admin"]},
    ]
}
sc.ensure_users("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_users: creates every listed user",
      any("user_create -u curie" in c for c in cmds) and any("user_create -u turing" in c for c in cmds))
check("ensure_users: grants each user's own roles",
      any("user_addrole curie channel_admin" in c for c in cmds)
      and any("user_addrole turing config_admin" in c for c in cmds))

# -- access_group_exists / ensure_access_group -------------------------------
fake = FakeSSH(responses=[("access.listRoles", FakeResult(returncode=0, stdout="read-only-ops\nother-group\n"))])
sc.ssh_run = fake
check("access_group_exists: found", sc.access_group_exists("host1", "mgrctl exec --", "read-only-ops") is True)
check("access_group_exists: not found", sc.access_group_exists("host1", "mgrctl exec --", "nope") is False)

fake = FakeSSH(responses=[("access.listRoles", FakeResult(returncode=0, stdout="read-only-ops\n"))])
sc.ssh_run = fake
sc.ensure_access_group("host1", "mgrctl exec --", "read-only-ops", "Read-only operators")
check("ensure_access_group: existing group -> no createRole call",
      len(fake.calls) == 1 and "access.createRole" not in fake.calls[0][1])

fake = FakeSSH(responses=[("access.listRoles", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_access_group("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "read-only-ops",
                        "Read-only operators", permissions_from=["base-role"])
create_cmd = next((c[1] for c in fake.calls if "access.createRole" in c[1]), "")
check("ensure_access_group: create command carries label/description/permissions_from as JSON",
      'api -A' in create_cmd
      and '["read-only-ops", "Read-only operators", ["base-role"]]' in create_cmd
      and create_cmd.endswith("access.createRole"))

# -- access_group_has_namespace / ensure_access_group_permissions -----------
fake = FakeSSH(responses=[("access.listPermissions", FakeResult(returncode=0, stdout="system_management.systems"))])
sc.ssh_run = fake
check("access_group_has_namespace: found",
      sc.access_group_has_namespace("host1", "mgrctl exec --", "grp", "system_management.systems") is True)
check("access_group_has_namespace: not found",
      sc.access_group_has_namespace("host1", "mgrctl exec --", "grp", "other.namespace") is False)

fake = FakeSSH(responses=[("access.listPermissions", FakeResult(returncode=0, stdout="system_management.systems"))])
sc.ssh_run = fake
sc.ensure_access_group_permissions("host1", "mgrctl exec --", "grp",
                                    [{"namespace": "system_management.systems"}])
check("ensure_access_group_permissions: already-granted namespace -> no grantAccess call",
      not any("access.grantAccess" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("access.listPermissions", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_access_group_permissions("host1", "mgrctl exec --", "grp", [
    {"namespace": "system_management.systems"},
    {"namespace": "channel_management.software_channels", "mode": "W"},
])
grant_cmd = next((c[1] for c in fake.calls if "access.grantAccess" in c[1]), "")
check("ensure_access_group_permissions: grants all missing namespaces with parallel modes",
      '["grp", ["system_management.systems", "channel_management.software_channels"], ["R", "W"]]' in grant_cmd)

fake = FakeSSH(responses=[("access.listPermissions", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_access_group_permissions("host1", "mgrctl exec --", "grp", [{"mode": "R"}])
except SystemExit:
    died = True
check("ensure_access_group_permissions: entry missing 'namespace' dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_access_group_permissions("host1", "mgrctl exec --", "grp", [])
check("ensure_access_group_permissions: no-op on empty list", len(fake.calls) == 0)

# -- user_has_role / ensure_user_role ----------------------------------------
fake = FakeSSH(responses=[("user_details", FakeResult(returncode=0, stdout="Roles: org_admin, read-only-ops"))])
sc.ssh_run = fake
check("user_has_role: found", sc.user_has_role("host1", "mgrctl exec --", "alice", "read-only-ops") is True)
check("user_has_role: not found", sc.user_has_role("host1", "mgrctl exec --", "alice", "channel_admin") is False)

fake = FakeSSH(responses=[("user_details", FakeResult(returncode=0, stdout="Roles: read-only-ops"))])
sc.ssh_run = fake
sc.ensure_user_role("host1", "mgrctl exec --", "alice", "read-only-ops")
check("ensure_user_role: already has role -> no addrole call",
      len(fake.calls) == 1 and "user_addrole" not in fake.calls[0][1])

fake = FakeSSH(responses=[("user_details", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_user_role("host1", "mgrctl exec --", "alice", "read-only-ops")
check("ensure_user_role: missing role -> user_addrole called with the right argv",
      any("user_addrole alice read-only-ops" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("user_details", FakeResult(returncode=0, stdout="")),
                           ("user_addrole", FakeResult(returncode=1, stderr="no such user 'bob'"))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_user_role("host1", "mgrctl exec --", "bob", "read-only-ops")
except SystemExit:
    died = True
check("ensure_user_role: user_addrole failure warns, doesn't die (confirmed live: even a "
      "satellite_admin session gets the identical rejection for a custom access-group label, "
      "so this can't be treated as a config mistake worth aborting the whole run over)",
      died is False)

# -- ensure_access_groups: full orchestration --------------------------------
fake = FakeSSH(responses=[
    ("access.listRoles", FakeResult(returncode=0, stdout="")),
    ("access.listPermissions", FakeResult(returncode=0, stdout="")),
    ("user_details", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {
    "smlm_access_groups": [{
        "label": "read-only-ops", "description": "Read-only operators",
        "permissions": [{"namespace": "system_management.systems"}],
        "users": ["alice", "bob"],
    }]
}
sc.ensure_access_groups("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_access_groups: creates the group", any("access.createRole" in c for c in cmds))
check("ensure_access_groups: grants its permissions", any("access.grantAccess" in c for c in cmds))
check("ensure_access_groups: attaches every listed user",
      any("user_addrole alice read-only-ops" in c for c in cmds)
      and any("user_addrole bob read-only-ops" in c for c in cmds))

# -- ensure_access_groups: no-op when unset, dies on missing 'label' --------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_access_groups("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_access_groups: no-op when field unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_access_groups("host1", "mgrctl exec --", {"uyuni_access_groups": [{"description": "no label"}]},
                             "uyuni")
except SystemExit:
    died = True
check("ensure_access_groups: entry missing 'label' dies", died)

# -- ansible_path_exists / ensure_ansible_path -------------------------------
fake = FakeSSH(responses=[("ansible.listAnsiblePaths",
                            FakeResult(returncode=0, stdout="/srv/ansible/playbooks"))])
sc.ssh_run = fake
check("ansible_path_exists: found",
      sc.ansible_path_exists("host1", "mgrctl exec --", 123, "/srv/ansible/playbooks") is True)
check("ansible_path_exists: not found",
      sc.ansible_path_exists("host1", "mgrctl exec --", 123, "/other/path") is False)

died = False
try:
    sc.ensure_ansible_path("host1", "mgrctl exec --", 123, "bogus", "/srv/x")
except SystemExit:
    died = True
check("ensure_ansible_path: invalid type dies", died)

fake = FakeSSH(responses=[("ansible.listAnsiblePaths",
                            FakeResult(returncode=0, stdout="/srv/ansible/playbooks"))])
sc.ssh_run = fake
sc.ensure_ansible_path("host1", "mgrctl exec --", 123, "playbook", "/srv/ansible/playbooks")
check("ensure_ansible_path: already registered -> no createAnsiblePath call",
      len(fake.calls) == 1 and "ansible.createAnsiblePath" not in fake.calls[0][1])

fake = FakeSSH(responses=[("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_ansible_path("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", 123, "playbook",
                        "/srv/ansible/playbooks")
create_cmd = next((c[1] for c in fake.calls if "ansible.createAnsiblePath" in c[1]), "")
check("ensure_ansible_path: create command carries type/server_id/path as a bare JSON "
      "object, not wrapped in a one-element array (confirmed live: -A binds the whole "
      "list as the arg for a single-arg method)",
      '{"type": "playbook", "server_id": 123, "path": "/srv/ansible/playbooks"}' in create_cmd
      and '[{"type"' not in create_cmd)

# -- ensure_ansible_paths: orchestration, no-op, validation ------------------
fake = FakeSSH(responses=[("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
cfg = {"uyuni_ansible_paths": [
    {"control_node_id": 123, "type": "playbook", "path": "/srv/pb"},
    {"control_node_id": 123, "type": "inventory", "path": "/srv/inv"},
]}
sc.ensure_ansible_paths("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("ensure_ansible_paths: registers every entry",
      sum(1 for c in cmds if "ansible.createAnsiblePath" in c) == 2)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_ansible_paths("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_ansible_paths: no-op when field unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_ansible_paths("host1", "mgrctl exec --",
                             {"uyuni_ansible_paths": [{"type": "playbook", "path": "/x"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_ansible_paths: entry missing 'control_node_id'/'system' dies", died)

died = False
try:
    sc.ensure_ansible_paths(
        "host1", "mgrctl exec --",
        {"uyuni_ansible_paths": [{"control_node_id": 123, "path": "/x"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_ansible_paths: entry missing 'type' dies", died)

# -- ensure_ansible_paths: 'system' name resolved via _system_id() (added 2026-09-18) --
fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42, "name": "charon.mydemo.lab"}]))),
    ("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {"smlm_ansible_paths": [
    {"system": "charon.mydemo.lab", "type": "playbook", "path": "/srv/ansible/playbooks"},
]}
sc.ensure_ansible_paths("host1", "mgrctl exec --", cfg, "smlm")
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_ansible_paths: a 'system' hostname is resolved to its numeric id first",
      any("system.getId" in c for c in cmds))
check("ensure_ansible_paths: the resolved id is used for the real createAnsiblePath call",
      any("ansible.createAnsiblePath" in c and '"server_id": 42' in c for c in cmds))

# -- remove_ansible_path: real ansible.removeAnsiblePath call --------------
fake = FakeSSH(responses=[("ansible.removeAnsiblePath", FakeResult(returncode=0, stdout="1"))])
sc.ssh_run = fake
sc.remove_ansible_path("host1", "mgrctl exec --", 2)
cmd = unwrap(fake.calls[0][1])
check("remove_ansible_path: calls the real, confirmed ansible.removeAnsiblePath method with the "
      "right path id",
      "-A 2 ansible.removeAnsiblePath" in cmd)

fake = FakeSSH(responses=[("ansible.removeAnsiblePath", FakeResult(returncode=1, stderr="not found"))])
sc.ssh_run = fake
died = False
try:
    sc.remove_ansible_path("host1", "mgrctl exec --", 999)
except SystemExit:
    died = True
check("remove_ansible_path: dies with a clear message on a real API failure", died)

# -- remove_stale_default_ansible_paths: only the 2 known SMLM-auto-created defaults --
fake = FakeSSH(responses=[
    ("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout=json.dumps([
        {"path": "/etc/ansible/hosts", "id": 1, "type": "inventory", "server_id": 42},
        {"path": "/srv/ansible/inventory/uyuni_dynamic_inventory.py", "id": 4, "type": "inventory", "server_id": 42},
        {"path": "/etc/ansible/playbooks", "id": 2, "type": "playbook", "server_id": 42},
        {"path": "/srv/ansible/playbooks", "id": 3, "type": "playbook", "server_id": 42},
    ]))),
    ("ansible.removeAnsiblePath", FakeResult(returncode=0, stdout="1")),
])
sc.ssh_run = fake
sc.remove_stale_default_ansible_paths("host1", "mgrctl exec --", 42)
cmds = [unwrap(c[1]) for c in fake.calls]
remove_calls = [c for c in cmds if "ansible.removeAnsiblePath" in c]
check("remove_stale_default_ansible_paths: removes exactly the 2 stale defaults, no more",
      len(remove_calls) == 2)
check("remove_stale_default_ansible_paths: removes the stale '/etc/ansible/hosts' default (id 1)",
      any("-A 1 ansible.removeAnsiblePath" in c for c in remove_calls))
check("remove_stale_default_ansible_paths: removes the stale '/etc/ansible/playbooks' default (id 2)",
      any("-A 2 ansible.removeAnsiblePath" in c for c in remove_calls))
check("remove_stale_default_ansible_paths: does NOT remove the real, working paths (ids 3/4)",
      not any("-A 3 ansible.removeAnsiblePath" in c or "-A 4 ansible.removeAnsiblePath" in c for c in cmds))

# A control node with only the real paths already registered (a second run, or a server
# that never auto-created the defaults) -> no removal calls at all.
fake = FakeSSH(responses=[
    ("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout=json.dumps([
        {"path": "/srv/ansible/inventory/uyuni_dynamic_inventory.py", "id": 4, "type": "inventory", "server_id": 42},
        {"path": "/srv/ansible/playbooks", "id": 3, "type": "playbook", "server_id": 42},
    ]))),
])
sc.ssh_run = fake
sc.remove_stale_default_ansible_paths("host1", "mgrctl exec --", 42)
check("remove_stale_default_ansible_paths: idempotent no-op when neither stale default is present",
      not any("ansible.removeAnsiblePath" in c[1] for c in fake.calls))

# -- ensure_ansible_paths: also cleans up stale defaults on every control node it touches --
fake = FakeSSH(responses=[
    ("ansible.listAnsiblePaths", FakeResult(returncode=0, stdout=json.dumps([
        {"path": "/etc/ansible/hosts", "id": 1, "type": "inventory", "server_id": 42},
    ]))),
    ("ansible.removeAnsiblePath", FakeResult(returncode=0, stdout="1")),
])
sc.ssh_run = fake
sc.ensure_ansible_paths("host1", "mgrctl exec --",
                         {"smlm_ansible_paths": [{"control_node_id": 42, "type": "playbook", "path": "/srv/x"}]},
                         "smlm")
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_ansible_paths: automatically cleans up stale defaults on the control node it just touched",
      any("ansible.listAnsiblePaths" in c for c in cmds) and any("ansible.removeAnsiblePath" in c for c in cmds))

# -- schedule_ansible_playbook: overload selection by arg shape --------------
fake = FakeSSH(responses=[("schedulePlaybook", FakeResult(returncode=0, stdout="42\n"))])
sc.ssh_run = fake
action_id = sc.schedule_ansible_playbook("host1", "mgrctl exec --", 123, "/srv/pb.yml", "/srv/inv",
                                          earliest="2026-01-01T00:00:00")
cmd = fake.calls[0][1]
check("schedule_ansible_playbook: base 5-arg form when no testMode/ansibleArgs given",
      '["/srv/pb.yml", "/srv/inv", 123, "2026-01-01T00:00:00", ""]' in unwrap(cmd)
      and "ansible.schedulePlaybook" in cmd)
check("schedule_ansible_playbook: returns the scheduled action id", action_id == "42")

fake = FakeSSH()
sc.ssh_run = fake
sc.schedule_ansible_playbook("host1", "mgrctl exec --", 123, "/pb.yml", "/inv",
                              earliest="2026-01-01T00:00:00", test_mode=True)
cmd = fake.calls[0][1]
check("schedule_ansible_playbook: test_mode-only -> 6-arg form",
      '["/pb.yml", "/inv", 123, "2026-01-01T00:00:00", "", true]' in cmd)

fake = FakeSSH()
sc.ssh_run = fake
sc.schedule_ansible_playbook("host1", "mgrctl exec --", 123, "/pb.yml", "/inv",
                              earliest="2026-01-01T00:00:00", extra_vars="foo: bar", flush_cache=True)
cmd = fake.calls[0][1]
check("schedule_ansible_playbook: extra_vars/flush_cache -> 7-arg form with testMode+ansibleArgs",
      '["/pb.yml", "/inv", 123, "2026-01-01T00:00:00", "", false, '
      '{"extraVars": "foo: bar", "flushCache": true}]' in cmd)

fake = FakeSSH()
sc.ssh_run = fake
sc.schedule_ansible_playbook("host1", "mgrctl exec --", 123, "/pb.yml", "/inv")
cmd = fake.calls[0][1]
check("schedule_ansible_playbook: defaults 'earliest' to the current UTC time when unset",
      re.search(r'"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"', cmd) is not None)

fake = FakeSSH(responses=[("schedulePlaybook", FakeResult(returncode=1, stderr="control node not found"))])
sc.ssh_run = fake
died = False
try:
    sc.schedule_ansible_playbook("host1", "mgrctl exec --", 999, "/pb.yml", "/inv",
                                  earliest="2026-01-01T00:00:00")
except SystemExit:
    died = True
check("schedule_ansible_playbook: server-side failure dies", died)

# -- ansible_playbook_status: reuses native schedule_* commands --------------
fake = FakeSSH(responses=[
    ("schedule_details", FakeResult(returncode=0, stdout="Action: foo")),
    ("schedule_getoutput", FakeResult(returncode=0, stdout="output text")),
])
sc.ssh_run = fake
details, output = sc.ansible_playbook_status("host1", "mgrctl exec --", 42)
check("ansible_playbook_status: returns (details, output) via schedule_details/schedule_getoutput",
      details == "Action: foo" and output == "output text")

# -- content_project_exists / ensure_content_project -------------------------
fake = FakeSSH(responses=[("contentmanagement.listProjects", FakeResult(returncode=0, stdout="web-lifecycle\n"))])
sc.ssh_run = fake
check("content_project_exists: found",
      sc.content_project_exists("host1", "mgrctl exec --", "web-lifecycle") is True)
check("content_project_exists: not found",
      sc.content_project_exists("host1", "mgrctl exec --", "other") is False)

fake = FakeSSH(responses=[("contentmanagement.listProjects", FakeResult(returncode=0, stdout="web-lifecycle\n"))])
sc.ssh_run = fake
sc.ensure_content_project("host1", "mgrctl exec --", "web-lifecycle", "Web", "desc")
check("ensure_content_project: existing project -> no createProject call",
      len(fake.calls) == 1 and "createProject" not in fake.calls[0][1])

fake = FakeSSH(responses=[("contentmanagement.listProjects", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_content_project("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "web-lifecycle", "Web", "desc")
create_cmd = next((c[1] for c in fake.calls if "createProject" in c[1]), "")
check("ensure_content_project: create command carries label/name/description as JSON",
      '["web-lifecycle", "Web", "desc"]' in create_cmd
      and create_cmd.endswith("contentmanagement.createProject"))

# -- content_source_exists / ensure_content_source ---------------------------
fake = FakeSSH(responses=[("contentmanagement.listProjectSources",
                            FakeResult(returncode=0, stdout="sle-product-base"))])
sc.ssh_run = fake
check("content_source_exists: found",
      sc.content_source_exists("host1", "mgrctl exec --", "proj", "sle-product-base") is True)
check("content_source_exists: not found",
      sc.content_source_exists("host1", "mgrctl exec --", "proj", "other") is False)

fake = FakeSSH(responses=[("contentmanagement.listProjectSources", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_content_source("host1", "mgrctl exec --", "proj", "sle-product-base")
attach_cmd = next((c[1] for c in fake.calls if "attachSource" in c[1]), "")
check("ensure_content_source: attach command carries project/type/source as JSON",
      '["proj", "software", "sle-product-base"]' in attach_cmd)

fake = FakeSSH(responses=[("contentmanagement.listProjectSources",
                            FakeResult(returncode=0, stdout="sle-product-base"))])
sc.ssh_run = fake
sc.ensure_content_source("host1", "mgrctl exec --", "proj", "sle-product-base")
check("ensure_content_source: already attached -> no attachSource call",
      len(fake.calls) == 1 and "attachSource" not in fake.calls[0][1])

# -- ensure_content_filter: validation, idempotency, id extraction ----------
died = False
try:
    sc.ensure_content_filter("host1", "mgrctl exec --", "proj", {"name": "f1"})
except SystemExit:
    died = True
check("ensure_content_filter: missing required fields dies", died)

fake = FakeSSH(responses=[("contentmanagement.listProjectFilters", FakeResult(returncode=0, stdout="exclude-beta"))])
sc.ssh_run = fake
sc.ensure_content_filter("host1", "mgrctl exec --", "proj",
                          {"name": "exclude-beta", "rule": "deny", "entity_type": "package",
                           "matcher": "contains", "field": "name", "value": "-beta"})
check("ensure_content_filter: already attached to this project -> no createFilter call",
      len(fake.calls) == 1 and "createFilter" not in fake.calls[0][1])

fake = FakeSSH(responses=[
    ("contentmanagement.listProjectFilters", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.createFilter", FakeResult(returncode=0, stdout="{'id': 42, 'name': 'exclude-beta'}")),
])
sc.ssh_run = fake
sc.ensure_content_filter("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "proj",
                          {"name": "exclude-beta", "rule": "deny", "entity_type": "package",
                           "matcher": "contains", "field": "name", "value": "-beta"})
cmds = [c[1] for c in fake.calls]
create_cmd = next(c for c in cmds if "createFilter" in c)
check("ensure_content_filter: create command carries name/rule/entity_type/criteria as JSON",
      '["exclude-beta", "deny", "package", {"matcher": "contains", "field": "name", "value": "-beta"}]'
      in create_cmd)
attach_cmd = next((c for c in cmds if "attachFilter" in c), "")
check("ensure_content_filter: attaches using the id parsed from createFilter's output",
      '["proj", 42]' in attach_cmd)

fake = FakeSSH(responses=[
    ("contentmanagement.listProjectFilters", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.createFilter", FakeResult(returncode=0, stdout="no id here")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_content_filter("host1", "mgrctl exec --", "proj",
                              {"name": "f2", "rule": "allow", "entity_type": "erratum",
                               "matcher": "equals", "field": "advisory_type", "value": "bugfix"})
except SystemExit:
    died = True
check("ensure_content_filter: unparseable id from createFilter output dies", died)

fake = FakeSSH(responses=[
    ("contentmanagement.listProjectFilters", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.createFilter", FakeResult(returncode=1, stderr="duplicate filter name")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_content_filter("host1", "mgrctl exec --", "proj",
                              {"name": "f3", "rule": "allow", "entity_type": "package",
                               "matcher": "contains", "field": "name", "value": "x"})
except SystemExit:
    died = True
check("ensure_content_filter: createFilter failure dies", died)

# -- content_environment_exists / ensure_content_environments ----------------
fake = FakeSSH(responses=[("contentmanagement.listProjectEnvironments", FakeResult(returncode=0, stdout="dev"))])
sc.ssh_run = fake
check("content_environment_exists: found",
      sc.content_environment_exists("host1", "mgrctl exec --", "proj", "dev") is True)
check("content_environment_exists: not found",
      sc.content_environment_exists("host1", "mgrctl exec --", "proj", "test") is False)

fake = FakeSSH(responses=[("contentmanagement.listProjectEnvironments", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_content_environments("host1", "mgrctl exec --", "proj", ["dev", "test", "prod"])
cmds = [c[1] for c in fake.calls]
create_cmds = [c for c in cmds if "createEnvironment" in c]
check("ensure_content_environments: creates every stage", len(create_cmds) == 3)
check("ensure_content_environments: first stage has an empty predecessor",
      '["proj", "", "dev", "dev", "dev"]' in create_cmds[0])
check("ensure_content_environments: second stage's predecessor is the first stage's label "
      "(confirmed live: this was NOT advancing before the fix, silently building a set of "
      "disconnected 'first' environments instead of a chain)",
      '["proj", "dev", "test", "test", "test"]' in create_cmds[1])
check("ensure_content_environments: third stage's predecessor is the second stage's label",
      '["proj", "test", "prod", "prod", "prod"]' in create_cmds[2])

fake = FakeSSH(responses=[("contentmanagement.listProjectEnvironments", FakeResult(returncode=0, stdout="dev"))])
sc.ssh_run = fake
sc.ensure_content_environments("host1", "mgrctl exec --", "proj", ["dev", "test"])
cmds = [c[1] for c in fake.calls]
check("ensure_content_environments: skips an already-existing stage but still chains the next one correctly",
      not any("createEnvironment" in c and '"dev", "dev", "dev"' in c for c in cmds)
      and any('["proj", "dev", "test", "test", "test"]' in c for c in cmds))

died = False
try:
    sc.ensure_content_environments("host1", "mgrctl exec --", "proj", [{"name": "no label"}])
except SystemExit:
    died = True
check("ensure_content_environments: entry missing 'label' dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_content_environments("host1", "mgrctl exec --", "proj", [])
check("ensure_content_environments: no-op on empty list", len(fake.calls) == 0)

# -- ensure_content_projects: full orchestration + validation + no-op -------
fake = FakeSSH(responses=[
    ("contentmanagement.listProjects", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.listProjectSources", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.listProjectFilters", FakeResult(returncode=0, stdout="")),
    ("contentmanagement.createFilter", FakeResult(returncode=0, stdout="{'id': 7}")),
    ("contentmanagement.listProjectEnvironments", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {
    "smlm_content_projects": [{
        "label": "web-lifecycle", "sources": ["sle-product-base"],
        "filters": [{"name": "exclude-beta", "rule": "deny", "entity_type": "package",
                     "matcher": "contains", "field": "name", "value": "-beta"}],
        "environments": ["dev", "test"],
    }]
}
sc.ensure_content_projects("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_content_projects: creates the project", any("createProject" in c for c in cmds))
check("ensure_content_projects: attaches the source", any("attachSource" in c for c in cmds))
check("ensure_content_projects: creates and attaches the filter",
      any("createFilter" in c for c in cmds) and any("attachFilter" in c for c in cmds))
check("ensure_content_projects: creates both environments",
      sum(1 for c in cmds if "createEnvironment" in c) == 2)

died = False
try:
    sc.ensure_content_projects("host1", "mgrctl exec --", {"uyuni_content_projects": [{"name": "no label"}]},
                                "uyuni")
except SystemExit:
    died = True
check("ensure_content_projects: entry missing 'label' dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_content_projects("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_content_projects: no-op when field unset", len(fake.calls) == 0)

# -- build_content_project / promote_content_project -------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.build_content_project("host1", "mgrctl exec --", "proj")
check("build_content_project: base 1-arg form when no message given",
      # _api_call passes a single arg as its bare JSON value, not wrapped in
      # a one-element array — confirmed live 2026-08-28 (saltkey.accept).
      '"proj"' in unwrap(fake.calls[0][1]) and "contentmanagement.buildProject" in fake.calls[0][1])

fake = FakeSSH()
sc.ssh_run = fake
sc.build_content_project("host1", "mgrctl exec --", "proj", message="initial build")
check("build_content_project: includes the message when given",
      '["proj", "initial build"]' in unwrap(fake.calls[0][1]))

fake = FakeSSH(responses=[("buildProject", FakeResult(returncode=1, stderr="no sources attached"))])
sc.ssh_run = fake
died = False
try:
    sc.build_content_project("host1", "mgrctl exec --", "proj")
except SystemExit:
    died = True
check("build_content_project: server-side failure dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.promote_content_project("host1", "mgrctl exec --", "proj", "dev")
check("promote_content_project: passes project and the FROM environment (not the destination)",
      '["proj", "dev"]' in unwrap(fake.calls[0][1]) and "contentmanagement.promoteProject" in fake.calls[0][1])

fake = FakeSSH(responses=[("promoteProject", FakeResult(returncode=1, stderr="no successor environment"))])
sc.ssh_run = fake
died = False
try:
    sc.promote_content_project("host1", "mgrctl exec --", "proj", "prod")
except SystemExit:
    died = True
check("promote_content_project: server-side failure dies", died)

# -- content_environment_status / wait_for_content_environment --------------
fake = FakeSSH(responses=[("lookupEnvironment",
                            FakeResult(returncode=0, stdout="{'status': 'built', 'label': 'dev'}"))])
sc.ssh_run = fake
check("content_environment_status: parses the status field",
      sc.content_environment_status("host1", "mgrctl exec --", "proj", "dev") == "built")

fake = FakeSSH(responses=[("lookupEnvironment", FakeResult(returncode=1, stderr="not found"))])
sc.ssh_run = fake
check("content_environment_status: returns None on failure",
      sc.content_environment_status("host1", "mgrctl exec --", "proj", "dev") is None)

fake = FakeSSH(responses=[("lookupEnvironment", FakeResult(returncode=0, stdout="{'status': 'built'}"))])
sc.ssh_run = fake
status = sc.wait_for_content_environment("host1", "mgrctl exec --", "proj", "dev", timeout=60, interval=5)
check("wait_for_content_environment: returns immediately once a target status is reached", status == "built")

fake = FakeSSH(responses=[("lookupEnvironment", FakeResult(returncode=0, stdout="{'status': 'building'}"))])
sc.ssh_run = fake
_orig_sleep = sc.time.sleep
sc.time.sleep = lambda s: None
try:
    died = False
    try:
        sc.wait_for_content_environment("host1", "mgrctl exec --", "proj", "dev", timeout=10, interval=5)
    except SystemExit:
        died = True
    check("wait_for_content_environment: dies after timeout if status never reaches target", died)
finally:
    sc.time.sleep = _orig_sleep

# -- run_content_lifecycle_actions: orchestration + validation --------------
fake = FakeSSH()
sc.ssh_run = fake
sc.run_content_lifecycle_actions("host1", "mgrctl exec --", {}, "uyuni")
check("run_content_lifecycle_actions: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH()
sc.ssh_run = fake
cfg = {"uyuni_content_lifecycle_actions": [
    {"project": "proj", "action": "build"},
    {"project": "proj", "action": "promote", "from_env": "dev"},
]}
sc.run_content_lifecycle_actions("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("run_content_lifecycle_actions: runs build then promote in order",
      any("buildProject" in c for c in cmds) and any("promoteProject" in c for c in cmds))

died = False
try:
    sc.run_content_lifecycle_actions(
        "host1", "mgrctl exec --", {"uyuni_content_lifecycle_actions": [{"project": "p", "action": "bogus"}]},
        "uyuni")
except SystemExit:
    died = True
check("run_content_lifecycle_actions: invalid action dies", died)

died = False
try:
    sc.run_content_lifecycle_actions(
        "host1", "mgrctl exec --", {"uyuni_content_lifecycle_actions": [{"project": "p", "action": "promote"}]},
        "uyuni")
except SystemExit:
    died = True
check("run_content_lifecycle_actions: promote without 'from_env' dies", died)

fake = FakeSSH(responses=[("lookupEnvironment", FakeResult(returncode=0, stdout="{'status': 'built'}"))])
sc.ssh_run = fake
sc.run_content_lifecycle_actions(
    "host1", "mgrctl exec --",
    {"uyuni_content_lifecycle_actions": [{"project": "proj", "action": "build", "wait": True, "wait_env": "dev"}]},
    "uyuni")
check("run_content_lifecycle_actions: 'wait' polls the named environment",
      any("lookupEnvironment" in c[1] for c in fake.calls))

died = False
try:
    sc.run_content_lifecycle_actions(
        "host1", "mgrctl exec --",
        {"uyuni_content_lifecycle_actions": [{"project": "proj", "action": "build", "wait": True}]},
        "uyuni")
except SystemExit:
    died = True
check("run_content_lifecycle_actions: 'wait' without 'wait_env' dies", died)

# -- scap_scan_exists / ensure_scap_scan -------------------------------------
fake = FakeSSH(responses=[("scap_listxccdfscans",
                            FakeResult(returncode=0, stdout="path: /usr/share/openscap/x.xml"))])
sc.ssh_run = fake
check("scap_scan_exists: found",
      sc.scap_scan_exists("host1", "mgrctl exec --", "web1", "/usr/share/openscap/x.xml") is True)
check("scap_scan_exists: not found",
      sc.scap_scan_exists("host1", "mgrctl exec --", "web1", "/other/path.xml") is False)

fake = FakeSSH(responses=[("scap_listxccdfscans", FakeResult(returncode=0, stdout="/usr/share/x.xml"))])
sc.ssh_run = fake
sc.ensure_scap_scan("host1", "mgrctl exec --", "web1", "/usr/share/x.xml", profile="Web-Default")
check("ensure_scap_scan: already scanned -> no scap_schedulexccdfscan call",
      len(fake.calls) == 1 and "scap_schedulexccdfscan" not in fake.calls[0][1])

fake = FakeSSH(responses=[("scap_listxccdfscans", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_scap_scan("host1", "mgrctl exec --", "web1", "/usr/share/x.xml", profile="Web-Default")
sched_cmd = next((c[1] for c in fake.calls if "scap_schedulexccdfscan" in c[1]), "")
check("ensure_scap_scan: schedule command carries path/profile-options/system",
      "scap_schedulexccdfscan /usr/share/x.xml" in sched_cmd
      and "profile Web-Default" in sched_cmd and "web1" in sched_cmd)

fake = FakeSSH(responses=[("scap_listxccdfscans", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_scap_scan("host1", "mgrctl exec --", "web1", "/usr/share/x.xml")
sched_cmd = next((c[1] for c in fake.calls if "scap_schedulexccdfscan" in c[1]), "")
check("ensure_scap_scan: no profile -> empty xccdf_options argument",
      "scap_schedulexccdfscan /usr/share/x.xml '' web1" in unwrap(sched_cmd))

fake = FakeSSH(responses=[
    ("scap_listxccdfscans", FakeResult(returncode=0, stdout="")),
    ("scap_schedulexccdfscan", FakeResult(returncode=1, stderr="no such system")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_scap_scan("host1", "mgrctl exec --", "bogus-system", "/usr/share/x.xml")
except SystemExit:
    died = True
check("ensure_scap_scan: schedule failure dies", died)

# -- list_scap_scans / scap_scan_details / scap_scan_rule_results -----------
fake = FakeSSH(responses=[("scap_listxccdfscans", FakeResult(returncode=0, stdout="scan list text"))])
sc.ssh_run = fake
check("list_scap_scans: returns raw text",
      sc.list_scap_scans("host1", "mgrctl exec --", "web1") == "scan list text")

fake = FakeSSH(responses=[("scap_getxccdfscandetails", FakeResult(returncode=0, stdout="details text"))])
sc.ssh_run = fake
check("scap_scan_details: returns raw text",
      sc.scap_scan_details("host1", "mgrctl exec --", 42) == "details text")

fake = FakeSSH(responses=[("scap_getxccdfscanruleresults", FakeResult(returncode=0, stdout="rule results text"))])
sc.ssh_run = fake
check("scap_scan_rule_results: returns raw text",
      sc.scap_scan_rule_results("host1", "mgrctl exec --", 42) == "rule results text")

# -- run_scap_scans: orchestration, no-op, validation ------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.run_scap_scans("host1", "mgrctl exec --", {}, "uyuni")
check("run_scap_scans: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[("scap_listxccdfscans", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
cfg = {"uyuni_scap_scans": [
    {"system": "web1", "xccdf_path": "/usr/share/x.xml", "profile": "Web-Default"},
    {"system": "web2", "xccdf_path": "/usr/share/y.xml"},
]}
sc.run_scap_scans("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("run_scap_scans: schedules every entry",
      sum(1 for c in cmds if "scap_schedulexccdfscan" in c) == 2)

died = False
try:
    sc.run_scap_scans("host1", "mgrctl exec --", {"uyuni_scap_scans": [{"system": "web1"}]}, "uyuni")
except SystemExit:
    died = True
check("run_scap_scans: entry missing 'xccdf_path' dies", died)

# -- list_systems_by_patch_status (CVE/OVAL audit) ---------------------------
fake = FakeSSH(responses=[("audit.listSystemsByPatchStatus",
                            FakeResult(returncode=0, stdout="[{'system_id': 1, 'patch_status': 'PATCHED'}]"))])
sc.ssh_run = fake
out = sc.list_systems_by_patch_status("host1", "mgrctl exec --", "CVE-2024-1234")
check("list_systems_by_patch_status: returns raw output via the api passthrough", "PATCHED" in out)
call_cmd = fake.calls[0][1]
check("list_systems_by_patch_status: JSON args carry just the CVE id when no status filter given",
      '"CVE-2024-1234"' in unwrap(call_cmd) and "audit.listSystemsByPatchStatus" in call_cmd)

fake = FakeSSH()
sc.ssh_run = fake
sc.list_systems_by_patch_status("host1", "mgrctl exec --", "CVE-2024-1234",
                                 patch_status_labels=["PATCHED", "NOT_AFFECTED"])
check("list_systems_by_patch_status: passes patch_status_labels as a second JSON arg",
      '["CVE-2024-1234", ["PATCHED", "NOT_AFFECTED"]]' in fake.calls[0][1])

fake = FakeSSH(responses=[("audit.listSystemsByPatchStatus", FakeResult(returncode=1, stderr="invalid CVE"))])
sc.ssh_run = fake
died = False
try:
    sc.list_systems_by_patch_status("host1", "mgrctl exec --", "bogus")
except SystemExit:
    died = True
check("list_systems_by_patch_status: server-side failure dies", died)

# -- list_images_by_patch_status (CVE-audit-adjacent, added 2026-09-18) ------
fake = FakeSSH(responses=[("audit.listImagesByPatchStatus",
                            FakeResult(returncode=0, stdout="[{'image_id': 1, 'patch_status': 'PATCHED'}]"))])
sc.ssh_run = fake
out = sc.list_images_by_patch_status("host1", "mgrctl exec --", "CVE-2024-1234")
check("list_images_by_patch_status: returns raw output via the api passthrough", "PATCHED" in out)
call_cmd = fake.calls[0][1]
check("list_images_by_patch_status: JSON args carry just the CVE id when no status filter given",
      '"CVE-2024-1234"' in unwrap(call_cmd) and "audit.listImagesByPatchStatus" in call_cmd)

fake = FakeSSH()
sc.ssh_run = fake
sc.list_images_by_patch_status("host1", "mgrctl exec --", "CVE-2024-1234",
                                patch_status_labels=["PATCHED", "NOT_AFFECTED"])
check("list_images_by_patch_status: passes patch_status_labels as a second JSON arg",
      '["CVE-2024-1234", ["PATCHED", "NOT_AFFECTED"]]' in fake.calls[0][1])

fake = FakeSSH(responses=[("audit.listImagesByPatchStatus", FakeResult(returncode=1, stderr="invalid CVE"))])
sc.ssh_run = fake
died = False
try:
    sc.list_images_by_patch_status("host1", "mgrctl exec --", "bogus")
except SystemExit:
    died = True
check("list_images_by_patch_status: server-side failure dies", died)

# -- activation_key_groups / ensure_activation_key_groups --------------------
fake = FakeSSH(responses=[("activationkey_listgroups", FakeResult(returncode=0, stdout="dev-systems\nqa-systems\n"))])
sc.ssh_run = fake
check("activation_key_groups: returns the current set",
      sc.activation_key_groups("host1", "mgrctl exec --", "1-mykey") == {"dev-systems", "qa-systems"})

fake = FakeSSH(responses=[("activationkey_listgroups", FakeResult(returncode=1, stderr="no such key"))])
sc.ssh_run = fake
check("activation_key_groups: returns empty set on failure",
      sc.activation_key_groups("host1", "mgrctl exec --", "bogus") == set())

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_activation_key_groups("host1", "mgrctl exec --", {}, "uyuni")
sc.ensure_activation_key_groups("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
check("ensure_activation_key_groups: no-op when key or groups field is unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[("activationkey_listgroups", FakeResult(returncode=0, stdout="dev-systems\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_groups(
    "host1", "mgrctl exec --",
    {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_groups": "dev-systems"}, "uyuni")
check("ensure_activation_key_groups: all already linked -> no addgroups call",
      len(fake.calls) == 2 and not any("activationkey_addgroups" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("activationkey_listgroups", FakeResult(returncode=0, stdout="dev-systems\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_groups(
    "host1", "kubectl exec -n ns deploy/uyuni -c uyuni --",
    {"smlm_activation_key": "1-mykey", "smlm_activation_key_groups": "dev-systems qa-systems"}, "smlm")
add_cmd = next((c[1] for c in fake.calls if "activationkey_addgroups" in c[1]), "")
check("ensure_activation_key_groups: links only the missing groups",
      "activationkey_addgroups 1-mykey qa-systems" in add_cmd)

fake = FakeSSH(responses=[
    ("activationkey_listgroups", FakeResult(returncode=0, stdout="")),
    ("activationkey_addgroups", FakeResult(returncode=1, stderr="no such group")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_activation_key_groups(
        "host1", "mgrctl exec --",
        {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_groups": "bogus-group"}, "uyuni")
except SystemExit:
    died = True
check("ensure_activation_key_groups: addgroups failure dies", died)

# -- activation_key_child_channels / ensure_activation_key_child_channels ----
# Real bug found live 2026-09-15: ensure_activation_key()'s own child-
# channel linking only ever ran at CREATION time — an already-existing key
# (the normal case on every run after the first) skipped it entirely, so a
# lab JSON edit adding/correcting child_channels for an existing key had
# silently NO EFFECT: confirmed live that several of solar-system-lab.
# json's own keys had ZERO "SUSE Multi-Linux Manager Client Tools" channel
# linked at all, because they were created once with an empty
# child_channels field and every later fix to add the right channel never
# got applied since the key already existed. Same fix shape as groups.
fake = FakeSSH(responses=[("activationkey_listchildchannels",
                            FakeResult(returncode=0, stdout="chan-a\nchan-b\n"))])
sc.ssh_run = fake
check("activation_key_child_channels: returns the current set",
      sc.activation_key_child_channels("host1", "mgrctl exec --", "1-mykey") == {"chan-a", "chan-b"})

fake = FakeSSH(responses=[("activationkey_listchildchannels", FakeResult(returncode=1, stderr="no such key"))])
sc.ssh_run = fake
check("activation_key_child_channels: returns empty set on failure",
      sc.activation_key_child_channels("host1", "mgrctl exec --", "bogus") == set())

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_activation_key_child_channels("host1", "mgrctl exec --", {}, "uyuni")
sc.ensure_activation_key_child_channels("host1", "mgrctl exec --", {"uyuni_activation_key": "1-mykey"}, "uyuni")
check("ensure_activation_key_child_channels: no-op when key or child_channels field is unset",
      len(fake.calls) == 0)

fake = FakeSSH(responses=[("activationkey_listchildchannels", FakeResult(returncode=0, stdout="chan-a\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_child_channels(
    "host1", "mgrctl exec --",
    {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_child_channels": "chan-a"}, "uyuni")
check("ensure_activation_key_child_channels: all already linked -> no addchildchannels call",
      len(fake.calls) == 2 and not any("activationkey_addchildchannels" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("activationkey_listchildchannels", FakeResult(returncode=0, stdout="chan-a\n"))])
sc.ssh_run = fake
sc.ensure_activation_key_child_channels(
    "host1", "kubectl exec -n ns deploy/uyuni -c uyuni --",
    {"smlm_activation_key": "1-mykey", "smlm_activation_key_child_channels": "chan-a managertools-sle15-pool-x86_64-sp7"},
    "smlm")
add_cmd = next((c[1] for c in fake.calls if "activationkey_addchildchannels" in c[1]), "")
check("ensure_activation_key_child_channels: links only the missing channel — this is what "
      "actually fixes an existing key whose lab JSON gained a Client Tools channel later",
      "activationkey_addchildchannels 1-mykey managertools-sle15-pool-x86_64-sp7" in add_cmd)

fake = FakeSSH(responses=[
    ("activationkey_listchildchannels", FakeResult(returncode=0, stdout="")),
    ("activationkey_addchildchannels", FakeResult(returncode=1, stderr="Invalid channel")),
])
sc.ssh_run = fake
warned = []
sc.warn = lambda m: warned.append(m)
sc.ensure_activation_key_child_channels(
    "host1", "mgrctl exec --",
    {"uyuni_activation_key": "1-mykey", "uyuni_activation_key_child_channels": "not-yet-synced-channel"}, "uyuni")
check("ensure_activation_key_child_channels: addchildchannels failure warns (not dies — a channel "
      "not yet synced is a real, recoverable, expected transient state, not a fatal misconfiguration)",
      len(warned) == 1 and "not-yet-synced-channel" in warned[0] and "Invalid channel" in warned[0])

# -- ensure_activation_keys: list orchestration reuses per-key functions -----
fake = FakeSSH(responses=[
    ("activationkey_listgroups", FakeResult(returncode=0, stdout="")),
    ("activationkey_listpackages", FakeResult(returncode=0, stdout="")),
    ("activationkey_list", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {"smlm_activation_keys": [
    {"smlm_activation_key": "1-dev-key", "smlm_activation_key_base_channel": "base-ch",
     "smlm_activation_key_packages": "nodejs", "smlm_activation_key_groups": "dev-systems"},
]}
sc.ensure_activation_keys("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_activation_keys: creates the key",
      any("activationkey_create" in c and "1-dev-key" in c for c in cmds))
check("ensure_activation_keys: adds its packages",
      any("activationkey_addpackages 1-dev-key nodejs" in c for c in cmds))
check("ensure_activation_keys: links its groups",
      any("activationkey_addgroups 1-dev-key dev-systems" in c for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_activation_keys("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_activation_keys: no-op when field unset", len(fake.calls) == 0)

# -- group_exists / ensure_system_group --------------------------------------
fake = FakeSSH(responses=[("group_list", FakeResult(returncode=0, stdout="dev-systems\nqa-systems\n"))])
sc.ssh_run = fake
check("group_exists: found", sc.group_exists("host1", "mgrctl exec --", "dev-systems") is True)
check("group_exists: not found", sc.group_exists("host1", "mgrctl exec --", "prod-systems") is False)

fake = FakeSSH(responses=[("group_list", FakeResult(returncode=0, stdout="dev-systems\n"))])
sc.ssh_run = fake
sc.ensure_system_group("host1", "mgrctl exec --", "dev-systems", "Dev systems")
check("ensure_system_group: existing group -> no group_create call",
      len(fake.calls) == 1 and "group_create" not in fake.calls[0][1])

fake = FakeSSH(responses=[("group_list", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_system_group("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "dev-systems", "Dev systems")
create_cmd = next((c[1] for c in fake.calls if "group_create" in c[1]), "")
check("ensure_system_group: create command carries name/description",
      "group_create dev-systems 'Dev systems'" in create_cmd)

fake = FakeSSH(responses=[("group_list", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_system_group("host1", "mgrctl exec --", "dev-systems")
create_cmd = next((c[1] for c in fake.calls if "group_create" in c[1]), "")
check("ensure_system_group: defaults description to the name when unset",
      "group_create dev-systems dev-systems" in create_cmd)

fake = FakeSSH(responses=[
    ("group_list", FakeResult(returncode=0, stdout="")),
    ("group_create", FakeResult(returncode=1, stderr="bad name")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_system_group("host1", "mgrctl exec --", "bad name")
except SystemExit:
    died = True
check("ensure_system_group: create failure dies", died)

# -- list_group_systems / group_has_system / ensure_group_systems -----------
fake = FakeSSH(responses=[("group_listsystems", FakeResult(returncode=0, stdout="dev1.lab\ndev2.lab\n"))])
sc.ssh_run = fake
check("list_group_systems: returns raw text",
      sc.list_group_systems("host1", "mgrctl exec --", "dev-systems") == "dev1.lab\ndev2.lab\n")
check("group_has_system: found",
      sc.group_has_system("host1", "mgrctl exec --", "dev-systems", "dev1.lab") is True)
check("group_has_system: not found",
      sc.group_has_system("host1", "mgrctl exec --", "dev-systems", "prod1.lab") is False)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_group_systems("host1", "mgrctl exec --", "dev-systems", [])
check("ensure_group_systems: no-op on empty systems list", len(fake.calls) == 0)

fake = FakeSSH(responses=[("group_listsystems", FakeResult(returncode=0, stdout="dev1.lab\n"))])
sc.ssh_run = fake
sc.ensure_group_systems("host1", "mgrctl exec --", "dev-systems", ["dev1.lab"])
check("ensure_group_systems: all already members -> no addsystems call",
      len(fake.calls) == 1 and "group_addsystems" not in fake.calls[0][1])

fake = FakeSSH(responses=[("group_listsystems", FakeResult(returncode=0, stdout="dev1.lab\n"))])
sc.ssh_run = fake
sc.ensure_group_systems("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --",
                         "dev-systems", ["dev1.lab", "dev2.lab"])
add_cmd = next((c[1] for c in fake.calls if "group_addsystems" in c[1]), "")
check("ensure_group_systems: adds only the missing systems",
      "group_addsystems dev-systems dev2.lab" in add_cmd)

fake = FakeSSH(responses=[
    ("group_listsystems", FakeResult(returncode=0, stdout="")),
    ("group_addsystems", FakeResult(returncode=1, stderr="no such system")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_group_systems("host1", "mgrctl exec --", "dev-systems", ["bogus.lab"])
except SystemExit:
    died = True
check("ensure_group_systems: addsystems failure warns, doesn't die (not-yet-registered "
      "systems are expected and self-heal on a later run)", died is False)

# -- ensure_system_groups: orchestration, no-op, validation ------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_system_groups("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_system_groups: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[
    ("group_listsystems", FakeResult(returncode=0, stdout="")),
    ("group_list", FakeResult(returncode=0, stdout="")),
])
sc.ssh_run = fake
cfg = {"uyuni_system_groups": [{"name": "dev-systems", "description": "Dev", "systems": ["dev1.lab"]}]}
sc.ensure_system_groups("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("ensure_system_groups: creates the group and adds its systems",
      any("group_create dev-systems Dev" in c for c in cmds)
      and any("group_addsystems dev-systems dev1.lab" in c for c in cmds))

died = False
try:
    sc.ensure_system_groups("host1", "mgrctl exec --", {"uyuni_system_groups": [{"description": "x"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_system_groups: entry missing 'name' dies", died)

# -- custom_info_key_exists / ensure_custom_info_key / ensure_custom_info_keys
fake = FakeSSH(responses=[("custominfo_listkeys", FakeResult(returncode=0, stdout="tier\nowner\n"))])
sc.ssh_run = fake
check("custom_info_key_exists: found", sc.custom_info_key_exists("host1", "mgrctl exec --", "tier") is True)
check("custom_info_key_exists: not found", sc.custom_info_key_exists("host1", "mgrctl exec --", "region") is False)

fake = FakeSSH(responses=[("custominfo_listkeys", FakeResult(returncode=0, stdout="tier\n"))])
sc.ssh_run = fake
sc.ensure_custom_info_key("host1", "mgrctl exec --", "tier", "Environment tier")
check("ensure_custom_info_key: existing key -> no createkey call",
      len(fake.calls) == 1 and "custominfo_createkey" not in fake.calls[0][1])

fake = FakeSSH(responses=[("custominfo_listkeys", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_custom_info_key("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "tier", "Environment tier")
create_cmd = next((c[1] for c in fake.calls if "custominfo_createkey" in c[1]), "")
check("ensure_custom_info_key: create command carries name/description",
      "custominfo_createkey tier 'Environment tier'" in create_cmd)

fake = FakeSSH(responses=[
    ("custominfo_listkeys", FakeResult(returncode=0, stdout="")),
    ("custominfo_createkey", FakeResult(returncode=1, stderr="bad key")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_custom_info_key("host1", "mgrctl exec --", "tier")
except SystemExit:
    died = True
check("ensure_custom_info_key: create failure dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_custom_info_keys("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_custom_info_keys: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH(responses=[("custominfo_listkeys", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_custom_info_keys("host1", "mgrctl exec --", {"uyuni_custom_info_keys": [{"name": "tier"}]}, "uyuni")
check("ensure_custom_info_keys: creates the key", any("custominfo_createkey" in c[1] for c in fake.calls))

died = False
try:
    sc.ensure_custom_info_keys("host1", "mgrctl exec --", {"uyuni_custom_info_keys": [{"description": "x"}]},
                                "uyuni")
except SystemExit:
    died = True
check("ensure_custom_info_keys: entry missing 'name' dies", died)

# -- ensure_system_tag / ensure_system_tags ----------------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_system_tag("host1", "mgrctl exec --", "dev1.lab", "tier", "dev")
check("ensure_system_tag: calls system_addcustomvalue with key/value/system in order",
      "system_addcustomvalue tier dev dev1.lab" in fake.calls[0][1])

fake = FakeSSH(responses=[("system_addcustomvalue", FakeResult(returncode=1, stderr="no such key"))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_system_tag("host1", "mgrctl exec --", "dev1.lab", "bogus", "x")
except SystemExit:
    died = True
check("ensure_system_tag: failure dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_system_tags("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_system_tags: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH()
sc.ssh_run = fake
cfg = {"uyuni_system_tags": [{"system": "dev1.lab", "tags": {"tier": "dev", "owner": "team-a"}}]}
sc.ensure_system_tags("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("ensure_system_tags: sets every tag on the system",
      any("system_addcustomvalue tier dev dev1.lab" in c for c in cmds)
      and any("system_addcustomvalue owner team-a dev1.lab" in c for c in cmds))

died = False
try:
    sc.ensure_system_tags("host1", "mgrctl exec --", {"uyuni_system_tags": [{"system": "dev1.lab"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_system_tags: entry missing 'tags' dies", died)

# -- group_id_for -------------------------------------------------------------
fake = FakeSSH(responses=[("group_details", FakeResult(returncode=0, stdout="ID: 42\nName: dev-systems\n"))])
sc.ssh_run = fake
check("group_id_for: parses a numeric id from group_details output",
      sc.group_id_for("host1", "mgrctl exec --", "dev-systems") == 42)

fake = FakeSSH(responses=[("group_details", FakeResult(returncode=0, stdout="Name: dev-systems\n"))])
sc.ssh_run = fake
check("group_id_for: returns None when no id could be parsed",
      sc.group_id_for("host1", "mgrctl exec --", "dev-systems") is None)

fake = FakeSSH(responses=[("group_details", FakeResult(returncode=1, stderr="no such group"))])
sc.ssh_run = fake
check("group_id_for: returns None on failure", sc.group_id_for("host1", "mgrctl exec --", "bogus") is None)

# -- ensure_recurring_schedule -------------------------------------------------
died = False
try:
    sc.ensure_recurring_schedule("host1", "mgrctl exec --", "group", 42, "0 2 * * 2", schedule_type="bogus")
except SystemExit:
    died = True
check("ensure_recurring_schedule: invalid schedule_type dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_recurring_schedule("host1", "mgrctl exec --", "group", 42, "0 2 * * 2")
cmd = fake.calls[0][1]
check("ensure_recurring_schedule: highstate uses recurring.highstate.create with entity/cron",
      # _api_call passes a single arg (the props dict) as its bare JSON
      # value, not wrapped in a one-element array — see its docstring.
      '{"entity_type": "group", "entity_id": 42, "cron_expr": "0 2 * * 2"}' in unwrap(cmd)
      and "recurring.highstate.create" in cmd)

died = False
try:
    sc.ensure_recurring_schedule("host1", "mgrctl exec --", "group", 42, "0 2 * * 2", schedule_type="custom")
except SystemExit:
    died = True
check("ensure_recurring_schedule: custom type without 'states' dies", died)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_recurring_schedule("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", "group", 42, "0 2 * * 2",
                              schedule_type="custom", states=["patch.apply"], extra={"name": "dev-patch"})
cmd = fake.calls[0][1]
check("ensure_recurring_schedule: custom includes states and merges 'extra'",
      '"states": ["patch.apply"]' in cmd and '"name": "dev-patch"' in cmd
      and cmd.endswith("recurring.custom.create"))

fake = FakeSSH(responses=[("recurring.highstate.create", FakeResult(returncode=1, stderr="bad entity"))])
sc.ssh_run = fake
died = False
try:
    sc.ensure_recurring_schedule("host1", "mgrctl exec --", "group", 999, "0 2 * * 2")
except SystemExit:
    died = True
check("ensure_recurring_schedule: server-side failure dies", died)

# -- ensure_environments -------------------------------------------------------
fake = FakeSSH(responses=[
    ("activationkey_listgroups", FakeResult(returncode=0, stdout="")),
    ("group_listsystems", FakeResult(returncode=0, stdout="dev1.lab\ndev2.lab\n")),
])
sc.ssh_run = fake
cfg = {"smlm_environments": [{
    "label": "dev", "system_group": "dev-systems", "activation_key": "1-dev-key",
    "custom_info_tags": {"tier": "dev"},
}]}
sc.ensure_environments("host1", "kubectl exec -n ns deploy/uyuni -c uyuni --", cfg, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_environments: links the activation key to the system group",
      any("activationkey_addgroups 1-dev-key dev-systems" in c for c in cmds))
check("ensure_environments: tags every system currently in the group",
      any("system_addcustomvalue tier dev dev1.lab" in c for c in cmds)
      and any("system_addcustomvalue tier dev dev2.lab" in c for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_environments("host1", "mgrctl exec --", {}, "uyuni")
check("ensure_environments: no-op when field unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_environments("host1", "mgrctl exec --", {"uyuni_environments": [{"system_group": "x"}]}, "uyuni")
except SystemExit:
    died = True
check("ensure_environments: entry missing 'label' dies", died)

# -- run_environment_schedules -------------------------------------------------
fake = FakeSSH()
sc.ssh_run = fake
sc.run_environment_schedules("host1", "mgrctl exec --", {}, "uyuni")
check("run_environment_schedules: no-op when field unset", len(fake.calls) == 0)

fake = FakeSSH()
sc.ssh_run = fake
cfg = {"uyuni_environments": [{"label": "dev", "recurring_schedule": {"cron": "0 2 * * 2", "group_id": 42}}]}
sc.run_environment_schedules("host1", "mgrctl exec --", cfg, "uyuni")
check("run_environment_schedules: uses an explicit group_id without resolving a name",
      any("recurring.highstate.create" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("group_details", FakeResult(returncode=0, stdout="ID: 7\n"))])
sc.ssh_run = fake
cfg = {"uyuni_environments": [{"label": "qa", "system_group": "qa-systems",
                                "recurring_schedule": {"cron": "0 3 * * 3"}}]}
sc.run_environment_schedules("host1", "mgrctl exec --", cfg, "uyuni")
cmds = [c[1] for c in fake.calls]
check("run_environment_schedules: resolves group_id from system_group when not given explicitly",
      any('"entity_id": 7' in c for c in cmds))

fake = FakeSSH(responses=[("group_details", FakeResult(returncode=1, stderr="no such group"))])
sc.ssh_run = fake
died = False
try:
    sc.run_environment_schedules(
        "host1", "mgrctl exec --",
        {"uyuni_environments": [{"label": "qa", "system_group": "bogus",
                                  "recurring_schedule": {"cron": "0 3 * * 3"}}]}, "uyuni")
except SystemExit:
    died = True
check("run_environment_schedules: dies when group_id can't be resolved and none was given", died)

died = False
try:
    sc.run_environment_schedules(
        "host1", "mgrctl exec --",
        {"uyuni_environments": [{"label": "qa", "recurring_schedule": {"cron": "0 3 * * 3"}}]}, "uyuni")
except SystemExit:
    died = True
check("run_environment_schedules: dies when neither system_group nor group_id is given", died)

died = False
try:
    sc.run_environment_schedules(
        "host1", "mgrctl exec --",
        {"uyuni_environments": [{"label": "qa", "recurring_schedule": {"group_id": 1}}]}, "uyuni")
except SystemExit:
    died = True
check("run_environment_schedules: dies when 'cron' is missing", died)

# -- Client registration: saltkey_pending/accepted/accept -------------------
fake = FakeSSH(responses=[("saltkey.pendingList", FakeResult(stdout="['client1.mydemo.lab']"))])
sc.ssh_run = fake
check("saltkey_pending: returns the raw pendingList output", "client1.mydemo.lab" in sc.saltkey_pending("host1", "mgrctl exec --"))

fake = FakeSSH(responses=[("saltkey.acceptedList", FakeResult(stdout="['client1.mydemo.lab']"))])
sc.ssh_run = fake
check("saltkey_accepted: found", sc.saltkey_accepted("host1", "mgrctl exec --", "client1.mydemo.lab") is True)
check("saltkey_accepted: not found", sc.saltkey_accepted("host1", "mgrctl exec --", "client2.mydemo.lab") is False)

fake = FakeSSH(responses=[("saltkey.accept", FakeResult(returncode=0))])
sc.ssh_run = fake
sc.saltkey_accept("host1", "mgrctl exec --", "client1.mydemo.lab")
check("saltkey_accept: calls saltkey.accept with the minion id",
      any("saltkey.accept" in c and "client1.mydemo.lab" in c for h, c, kw in fake.calls))

fake = FakeSSH(responses=[("saltkey.accept", FakeResult(returncode=1, stderr="no such key"))])
sc.ssh_run = fake
died = False
try:
    sc.saltkey_accept("host1", "mgrctl exec --", "client1.mydemo.lab")
except SystemExit:
    died = True
check("saltkey_accept: dies on failure", died)


# -- _ensure_client_can_resolve_server (added 2026-09-24) -------------------
# Real bug found live 2026-09-24: saturn.mydemo.lab/neptune.mydemo.lab (both
# AWS EC2, on a completely different network/DNS than this lab) simply
# cannot resolve the SMLM server's hostname at all — confirmed live neither
# client's own DNS config points anywhere near this lab's BIND server, yet
# both reached the server's real public IP directly over HTTPS fine the
# instant its IP was used instead of its name — a pure DNS gap, not
# connectivity. Fixed by resolving the server's hostname LOCALLY (on the
# automation node, which has working DNS for this lab) and pushing a static
# /etc/hosts entry onto the client.
_real_gethostbyname = sc.socket.gethostbyname
sc.socket.gethostbyname = lambda fqdn: "3.71.46.122" if fqdn == "sol.mydemo.lab" else (_ for _ in ()).throw(
    sc.socket.gaierror("simulated: not found"))

fake = FakeSSH()
sc.ssh_run = fake
sc._ensure_client_can_resolve_server("saturn.mydemo.lab", "sol.mydemo.lab")
check("_ensure_client_can_resolve_server: runs against the CLIENT host, not the server",
      len(fake.calls) == 1 and fake.calls[0][0] == "saturn.mydemo.lab")
check("_ensure_client_can_resolve_server: pushes the real resolved IP paired with the FQDN",
      "3.71.46.122 sol.mydemo.lab" in fake.calls[0][1])
check("_ensure_client_can_resolve_server: idempotent — checks /etc/hosts first, doesn't just append blindly",
      fake.calls[0][1].startswith("grep -qF") and "/etc/hosts" in fake.calls[0][1])

fake = FakeSSH()
sc.ssh_run = fake
sc._ensure_client_can_resolve_server("saturn.mydemo.lab", "unresolvable.mydemo.lab")
check("_ensure_client_can_resolve_server: silently no-ops when the automation node itself can't "
      "resolve the server either (nothing more it can do — the real bootstrap attempt right "
      "after reports its own clear error instead)",
      len(fake.calls) == 0)

sc.socket.gethostbyname = _real_gethostbyname


# -- ensure_client_registered -------------------------------------------------
sc.time.sleep = lambda s: None  # never actually wait in tests

# Already accepted: pure no-op, no bootstrap curl issued — confirms
# _ensure_client_can_resolve_server() isn't even attempted for a client
# that's already done, matching the existing early-return.
fake = FakeSSH(responses=[("saltkey.acceptedList", FakeResult(stdout="['client1.mydemo.lab']"))])
sc.ssh_run = fake
sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key")
check("ensure_client_registered: already-accepted client is a pure no-op", len(fake.calls) == 1)

# Not yet registered: bootstraps the client, then accepts once the key goes pending.
_poll_count = {"n": 0}


def _responder(hostname, cmd, **kwargs):
    if "saltkey.acceptedList" in cmd:
        return FakeResult(stdout="[]")
    if "saltkey.pendingList" in cmd:
        _poll_count["n"] += 1
        # not pending on the first poll, pending from the second poll onward
        return FakeResult(stdout="['client1.mydemo.lab']" if _poll_count["n"] >= 2 else "[]")
    if "saltkey.accept" in cmd:
        return FakeResult(returncode=0)
    if "curl -Sks" in cmd:
        return FakeResult(returncode=0)
    return FakeResult()


sc.ssh_run = _responder
calls_before = _poll_count["n"]
sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key",
                             retry_limit=5, retry_interval=0)
check("ensure_client_registered: polled pendingList more than once before accepting",
      _poll_count["n"] > calls_before + 1)

# Verify the bootstrap command shape by capturing calls with a recording wrapper.
bootstrap_calls = []


def _recording_responder(hostname, cmd, **kwargs):
    bootstrap_calls.append((hostname, cmd, kwargs))
    return _responder(hostname, cmd, **kwargs)


_poll_count["n"] = 0
sc.ssh_run = _recording_responder
sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key",
                             reactivation_key="react-1", retry_limit=5, retry_interval=0)
bootstrap_cmd = next((c for h, c, kw in bootstrap_calls if "curl -Sks" in c), None)
check("ensure_client_registered: bootstrap runs against the CLIENT host, not the server",
      any(h == "client1.mydemo.lab" for h, c, kw in bootstrap_calls if "curl -Sks" in c))
check("ensure_client_registered: bootstrap URL points at the per-key generated script",
      bootstrap_cmd is not None and "https://uyuni.mydemo.lab/pub/bootstrap/1-key.sh" in bootstrap_cmd)

check("ensure_client_registered: generates the bootstrap script via mgr-bootstrap before curling it",
      any("mgr-bootstrap" in c and "--activation-keys=1-key" in c and "--script=1-key.sh" in c
          for h, c, kw in bootstrap_calls))
check("ensure_client_registered: ACTIVATION_KEYS is set on the bootstrap command",
      bootstrap_cmd is not None and "ACTIVATION_KEYS=1-key" in bootstrap_cmd)
check("ensure_client_registered: REACTIVATION_KEY is passed through when given",
      bootstrap_cmd is not None and "REACTIVATION_KEY=react-1" in bootstrap_cmd)

# Bootstrap script itself fails -> dies immediately, no polling.
def _bootstrap_fails(hostname, cmd, **kwargs):
    if "saltkey.acceptedList" in cmd:
        return FakeResult(stdout="[]")
    if "curl -Sks" in cmd:
        return FakeResult(returncode=1)
    return FakeResult()


sc.ssh_run = _bootstrap_fails
died = False
try:
    sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key")
except SystemExit:
    died = True
check("ensure_client_registered: dies immediately if the bootstrap script itself fails", died)

# Key never goes pending -> dies with a clear message after exhausting retries.
def _never_pending(hostname, cmd, **kwargs):
    if "saltkey.acceptedList" in cmd or "saltkey.pendingList" in cmd:
        return FakeResult(stdout="[]")
    return FakeResult(returncode=0)


sc.ssh_run = _never_pending
died = False
try:
    sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key",
                                 retry_limit=3, retry_interval=0)
except SystemExit:
    died = True
check("ensure_client_registered: dies if the key never appears as pending", died)

# -- legacy-TLS-client recovery (added 2026-09-23) ---------------------------
# Real bug found + reproduced live 2026-09-23 (solar-system-lab.json,
# luna.mydemo.lab, CentOS 7): curl links Mozilla NSS 3.15.4, which cannot
# negotiate TLS with this server's modern TLS-1.2-only policy at all — the
# pipeline still exits 0 (curl fails, /bin/bash gets empty stdin), so the
# ONLY visible symptom is the salt key never going pending. Confirmed live
# this is fixable entirely client-side: every Python/wget on such a client
# links the system OpenSSL, never NSS.
_REAL_CHANNEL_PACKAGES = "\n".join([
    "dmidecode-3.2-5.el7_9.1.x86_64",
    "openssl-1.0.2k-19.el7:1.x86_64",
    "openssl-libs-1.0.2k-19.el7:1.x86_64",
    "wget-1.14-18.el7_6.1.x86_64",
])

fake = FakeSSH(responses=[("softwarechannel_listallpackages", FakeResult(stdout=_REAL_CHANNEL_PACKAGES))])
sc.ssh_run = fake
check("_channel_package_nvr: finds the exact NVR-EA line for a simple (no-epoch) package",
      sc._channel_package_nvr("srv1", "mgrctl exec --", "centos7-x86_64", "wget")
      == "wget-1.14-18.el7_6.1.x86_64")
check("_channel_package_nvr: finds the exact NVR-EA line for a package with an epoch",
      sc._channel_package_nvr("srv1", "mgrctl exec --", "centos7-x86_64", "openssl")
      == "openssl-1.0.2k-19.el7:1.x86_64")
check("_channel_package_nvr: does not confuse 'openssl' with 'openssl-libs' (prefix collision)",
      sc._channel_package_nvr("srv1", "mgrctl exec --", "centos7-x86_64", "openssl-libs")
      == "openssl-libs-1.0.2k-19.el7:1.x86_64")
check("_channel_package_nvr: returns None for a package not in the channel",
      sc._channel_package_nvr("srv1", "mgrctl exec --", "centos7-x86_64", "nginx") is None)

_REAL_RPM_PATH = ("/var/spacewalk/packages/NULL/55c/openssl/1:1.0.2k-19.el7/x86_64/"
                   "55c478a259b0a27ccb485dce91e190c0040df26b800a1f7a74557a47bef106d4/"
                   "openssl-1.0.2k-19.el7.x86_64.rpm")


def _stage_responder(hostname, cmd, **kwargs):
    if "softwarechannel_listallpackages" in cmd:
        return FakeResult(stdout=_REAL_CHANNEL_PACKAGES)
    if "find /var/spacewalk/packages" in cmd:
        # epoch must already be stripped from the search filename
        check("_stage_channel_package_on_client: searches by filename with the epoch stripped",
              "openssl-1.0.2k-19.el7.x86_64.rpm" in cmd and ":1." not in cmd)
        return FakeResult(stdout=_REAL_RPM_PATH)
    if "base64 " in cmd and "base64 -d" not in cmd:
        check("_stage_channel_package_on_client: base64-encodes the real located file on the server side",
              _REAL_RPM_PATH in cmd)
        return FakeResult(stdout="ZmFrZS1ycG0tY29udGVudA==")  # "fake-rpm-content"
    if "base64 -d" in cmd:
        check("_stage_channel_package_on_client: writes to the client under the given dest_dir",
              "/tmp/.lab-legacy-tls/openssl-1.0.2k-19.el7.x86_64.rpm" in cmd)
        check("_stage_channel_package_on_client: the base64 payload is passed through unmodified",
              kwargs.get("input_text") == "ZmFrZS1ycG0tY29udGVudA==")
        return FakeResult(returncode=0)
    return FakeResult()


sc.ssh_run = _stage_responder
staged_path = sc._stage_channel_package_on_client(
    "srv1", "mgrctl exec --", "centos7-x86_64", "openssl", "client1.mydemo.lab", "/tmp/.lab-legacy-tls")
check("_stage_channel_package_on_client: returns the staged client-side path on success",
      staged_path == "/tmp/.lab-legacy-tls/openssl-1.0.2k-19.el7.x86_64.rpm")

fake = FakeSSH(responses=[("softwarechannel_listallpackages", FakeResult(stdout=_REAL_CHANNEL_PACKAGES))])
sc.ssh_run = fake
check("_stage_channel_package_on_client: returns None for a package not in the channel at all",
      sc._stage_channel_package_on_client(
          "srv1", "mgrctl exec --", "centos7-x86_64", "nginx", "client1.mydemo.lab", "/tmp/x") is None)

fake = FakeSSH(responses=[
    ("softwarechannel_listallpackages", FakeResult(stdout=_REAL_CHANNEL_PACKAGES)),
    ("find /var/spacewalk/packages", FakeResult(stdout="")),  # not found on disk
])
sc.ssh_run = fake
check("_stage_channel_package_on_client: returns None when the package can't be located on disk",
      sc._stage_channel_package_on_client(
          "srv1", "mgrctl exec --", "centos7-x86_64", "wget", "client1.mydemo.lab", "/tmp/x") is None)

# ensure_client_registered end-to-end: curl-only client never goes pending,
# but the legacy-TLS recovery (wget-based bootstrap) succeeds instead —
# confirms it's actually wired into the real registration flow, not just a
# standalone function nobody calls.
_wget_attempts = {"n": 0}


def _legacy_recovery_responder(hostname, cmd, **kwargs):
    if "saltkey.acceptedList" in cmd:
        return FakeResult(stdout="[]")
    if "saltkey.pendingList" in cmd:
        # only goes pending AFTER the legacy wget-based bootstrap has run
        return FakeResult(stdout="['client1.mydemo.lab']" if _wget_attempts["n"] > 0 else "[]")
    if "saltkey.accept" in cmd:
        return FakeResult(returncode=0)
    if "curl -Sks" in cmd or cmd.startswith("_url="):
        # the ORIGINAL curl-based bootstrap_cmd: exits 0 but never actually
        # runs anything real (the exact real symptom — NSS TLS failure, curl
        # fails, /bin/bash gets empty stdin, no error surfaces)
        return FakeResult(returncode=0, stdout="", stderr="")
    if "command -v wget" in cmd:
        return FakeResult(returncode=0)  # wget already present, skip staging
    if cmd.startswith("_tmp=$(mktemp)") and "wget -qO" in cmd:
        _wget_attempts["n"] += 1
        return FakeResult(returncode=0, stdout="-bootstrap complete-\n", stderr="")
    return FakeResult()


sc.ssh_run = _legacy_recovery_responder
_wget_attempts["n"] = 0
sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key",
                             retry_limit=2, retry_interval=0, base_channel="centos7-x86_64")
check("ensure_client_registered: falls back to the legacy-TLS wget recovery when the key never "
      "goes pending via curl, and succeeds instead of dying",
      _wget_attempts["n"] == 1)

# Without base_channel, the same never-pending curl client just dies as
# before — the recovery path is opt-in, never attempted blindly.
sc.ssh_run = _never_pending
died = False
try:
    sc.ensure_client_registered("srv1", "mgrctl exec --", "client1.mydemo.lab", "uyuni.mydemo.lab", "1-key",
                                 retry_limit=3, retry_interval=0)
except SystemExit:
    died = True
check("ensure_client_registered: with no base_channel given, still dies as before (no fallback attempted)",
      died)


# -- describe_activation_key / describe_system_group / describe_access_groups /
#    export_config -- reading a live server back into lab-in-a-box JSON --------
# Fixture text below is copied verbatim from a real activationkey_details/
# group_details run against a live SMLM 5.2 server (2026-09-16), not invented,
# since this whole feature exists to parse that exact real output shape.
_REAL_AK_DETAILS = """Key:                    1-sles15sp7
Description:            mercury.mydemo.lab - SLES 15 SP7
Universal Default:      False
Usage Limit:            0
Deploy Config Channels: False
Contact Method:         default

Software Channels
-----------------
sle-product-sles15-sp7-pool-x86_64
 |-- managertools-sle15-pool-x86_64-sp7
 |-- managertools-sle15-updates-x86_64-sp7
 |-- sle-module-basesystem15-sp7-pool-x86_64
 |-- sle-module-basesystem15-sp7-updates-x86_64
 |-- sle-product-sles15-sp7-updates-x86_64
 |-- sle15-sp7-installer-updates-x86_64

Configuration Channels
----------------------

Entitlements
------------


System Groups
-------------
prod

Packages
--------
"""

fake = FakeSSH(responses=[("activationkey_details", FakeResult(returncode=0, stdout=_REAL_AK_DETAILS))])
sc.ssh_run = fake
ak = sc.describe_activation_key("host1", "mgrctl exec --", "1-sles15sp7", "smlm")
check("describe_activation_key: strips the numeric org-id prefix off the key name",
      ak["smlm_activation_key"] == "sles15sp7")
check("describe_activation_key: description round-trips exactly as the server has it",
      ak["smlm_activation_key_desc"] == "mercury.mydemo.lab - SLES 15 SP7")
check("describe_activation_key: base channel is the first (non-indented) software channel line",
      ak["smlm_activation_key_base_channel"] == "sle-product-sles15-sp7-pool-x86_64")
check("describe_activation_key: child channels are every ' |-- '-prefixed line, space-joined",
      ak["smlm_activation_key_child_channels"] ==
      "managertools-sle15-pool-x86_64-sp7 managertools-sle15-updates-x86_64-sp7 "
      "sle-module-basesystem15-sp7-pool-x86_64 sle-module-basesystem15-sp7-updates-x86_64 "
      "sle-product-sles15-sp7-updates-x86_64 sle15-sp7-installer-updates-x86_64")
check("describe_activation_key: groups", ak["smlm_activation_key_groups"] == "prod")
check("describe_activation_key: an EMPTY section (Configuration Channels here) contributes no "
      "field at all — confirmed live 2026-09-16 this used to bleed the NEXT header's own text in "
      "as bogus content when the boundary regex assumed two blank lines instead of one",
      "smlm_activation_key_config_channels" not in ak)
check("describe_activation_key: an empty Entitlements section is also omitted, not an empty string",
      "smlm_activation_key_entitlements" not in ak)

_REAL_GROUP_DETAILS = """ID:                17
Name:              star
Description:       The G-type main-sequence star at the center of the system
Number of Systems: 0

Members
-------
"""
fake = FakeSSH(responses=[("group_details", FakeResult(returncode=0, stdout=_REAL_GROUP_DETAILS))])
sc.ssh_run = fake
sg = sc.describe_system_group("host1", "mgrctl exec --", "star")
check("describe_system_group: name/description round-trip",
      sg == {"name": "star", "description": "The G-type main-sequence star at the center of the system"})

_REAL_GROUP_DETAILS_WITH_MEMBERS = """ID:                20
Name:              terrestrial-planets
Description:       Rocky planets with solid surfaces
Number of Systems: 3

Members
-------
earth.mydemo.lab
mars.mydemo.lab
mercury.mydemo.lab
"""
fake = FakeSSH(responses=[("group_details", FakeResult(returncode=0, stdout=_REAL_GROUP_DETAILS_WITH_MEMBERS))])
sc.ssh_run = fake
sg = sc.describe_system_group("host1", "mgrctl exec --", "terrestrial-planets")
check("describe_system_group: members list populated when non-empty",
      sg["systems"] == ["earth.mydemo.lab", "mars.mydemo.lab", "mercury.mydemo.lab"])

fake = FakeSSH(responses=[
    ("access.listRoles", FakeResult(returncode=0, stdout=json.dumps(
        [{"label": "engineering", "description": "Content, config and Salt formula authoring"}]))),
    ("access.listPermissions", FakeResult(returncode=0, stdout=json.dumps([
        {"namespace": "software.manage.list", "access_mode": {"value": "W"}},
        {"namespace": "config.channels", "access_mode": {"value": "W"}},
    ]))),
])
sc.ssh_run = fake
groups = sc.describe_access_groups("host1", "mgrctl exec --")
check("describe_access_groups: returns one entry per role with label/description/permissions",
      groups == [{
          "label": "engineering", "description": "Content, config and Salt formula authoring",
          "permissions": [{"namespace": "software.manage.list", "mode": "W"},
                           {"namespace": "config.channels", "mode": "W"}],
      }])

# export_config: full orchestration, mocking only the top-level list commands (activation-key/
# group/access-group DETAIL parsing is already covered above by the real fixtures).
fake = FakeSSH(responses=[
    # "org_listusers" MUST be checked before "org_list" — FakeSSH matches the
    # first substring hit in list order, and "org_list" is itself a substring
    # of "org_listusers" (confirmed live 2026-09-16: without this ordering,
    # every org_listusers call silently got org_list's own response instead).
    ("org_listusers", FakeResult(returncode=0, stdout="edgeadmin\nlovelace\n")),
    ("softwarechannel_list", FakeResult(returncode=0, stdout="chan1\nchan2\n")),
    ("activationkey_list", FakeResult(returncode=0, stdout="1-sles15sp7\n")),
    ("activationkey_details", FakeResult(returncode=0, stdout=_REAL_AK_DETAILS)),
    ("group_list", FakeResult(returncode=0, stdout="star\n")),
    ("group_details", FakeResult(returncode=0, stdout=_REAL_GROUP_DETAILS)),
    ("access.listRoles", FakeResult(returncode=0, stdout="[]")),
    ("org_list", FakeResult(returncode=0, stdout="Default\nedge\n")),
    ("user_details", FakeResult(returncode=0, stdout="Organisation:  Default\n")),
])
sc.ssh_run = fake
result = sc.export_config("host1", "mgrctl exec --", "admin", "pw", "smlm")
check("export_config: top-level admin/org fields", result["smlm_admin"] == "admin" and result["smlm_org"] == "Default")
check("export_config: channels list", result["smlm_channels"] == ["chan1", "chan2"])
check("export_config: activation keys via describe_activation_key",
      len(result["smlm_activation_keys"]) == 1
      and result["smlm_activation_keys"][0]["smlm_activation_key"] == "sles15sp7")
check("export_config: system groups via describe_system_group",
      result["smlm_system_groups"] ==
      [{"name": "star", "description": "The G-type main-sequence star at the center of the system"}])
check("export_config: skips the CALLER'S OWN org (Default) from smlm_orgs, keeps others",
      [o["name"] for o in result["smlm_orgs"]] == ["edge"])
check("export_config: every non-own org carries a best-effort username list and an explicit "
      "warning that passwords/other fields could not be recovered",
      result["smlm_orgs"][0]["existing_users"] == ["edgeadmin", "lovelace"]
      and "_export_note" in result["smlm_orgs"][0])
check("export_config: no smlm_access_groups key at all when the current org has none",
      "smlm_access_groups" not in result)


# -- ensure_distribution / ensure_kickstart_profile ---------------------------
fake = FakeSSH(responses=[("distribution_list", FakeResult(returncode=0, stdout=""))])
sc.ssh_run = fake
sc.ensure_distribution("host1", "mgrctl exec --", {
    "name": "test-dist", "path": "/srv/www/htdocs/pub/install-trees/x",
    "base_channel": "chan1", "install_type": "sles15generic",
})
create_cmd = next((c[1] for c in fake.calls if "distribution_create" in c[1]), "")
check("ensure_distribution: creates with the right flags",
      "-n test-dist" in create_cmd and "-p /srv/www/htdocs/pub/install-trees/x" in create_cmd
      and "-b chan1" in create_cmd and "-t sles15generic" in create_cmd)

fake = FakeSSH(responses=[("distribution_list", FakeResult(returncode=0, stdout="test-dist\n"))])
sc.ssh_run = fake
sc.ensure_distribution("host1", "mgrctl exec --", {"name": "test-dist"})
check("ensure_distribution: existing distribution -> no create call",
      not any("distribution_create" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[
    ("distribution_list", FakeResult(returncode=0, stdout="")),
    ("distribution_create", FakeResult(returncode=1, stderr="initrd could not be found")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_distribution("host1", "mgrctl exec --", {
        "name": "test-dist", "path": "/no/tree", "base_channel": "c", "install_type": "t",
    })
except SystemExit:
    died = True
check("ensure_distribution: a missing install tree warns, doesn't die (a real, expected, "
      "self-populated-out-of-band state, confirmed live 2026-09-16 — must not abort every "
      "orchestration step after it)", died is False)

fake = FakeSSH(responses=[
    ("kickstart_list", FakeResult(returncode=0, stdout="")),
    ("distribution_list", FakeResult(returncode=0, stdout="")),  # distribution NOT there
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_kickstart_profile("host1", "mgrctl exec --", {
        "name": "test-ks", "distribution": "missing-dist", "root_password": "pw",
    })
except SystemExit:
    died = True
check("ensure_kickstart_profile: skips cleanly (warns, doesn't die) when its own distribution "
      "doesn't exist yet, without ever calling kickstart_create", died is False
      and not any("kickstart_create" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[
    # More specific "kickstart_list*" substrings MUST be checked before the bare
    # "kickstart_list" — same substring-ordering pitfall as org_list/org_listusers
    # earlier in this file (confirmed live 2026-09-16: without this ordering,
    # kickstart_listvariables/listactivationkeys/listchildchannels all silently
    # got kickstart_list's own response instead).
    ("kickstart_listvariables", FakeResult(returncode=0, stdout="org = 1\n")),
    ("kickstart_listactivationkeys", FakeResult(returncode=0, stdout="")),
    ("kickstart_listchildchannels", FakeResult(returncode=0, stdout="")),
    ("kickstart_list", FakeResult(returncode=0, stdout="")),
    ("distribution_list", FakeResult(returncode=0, stdout="test-dist\n")),
])
sc.ssh_run = fake
sc.ensure_kickstart_profile("host1", "mgrctl exec --", {
    "name": "test-ks", "distribution": "test-dist", "root_password": "pw",
    "variables": {"lab": "solar-system"}, "activation_keys": ["1-key"],
})
cmds = [c[1] for c in fake.calls]
check("ensure_kickstart_profile: creates when its distribution exists",
      any("kickstart_create -n test-ks -d test-dist -p pw -v none" in c for c in cmds))
check("ensure_kickstart_profile: sets a variable not already present",
      any("kickstart_addvariable test-ks lab solar-system" in c for c in cmds))
check("ensure_kickstart_profile: links an activation key not already present",
      any("kickstart_addactivationkeys test-ks 1-key" in c for c in cmds))

fake = FakeSSH(responses=[
    ("kickstart_listvariables", FakeResult(returncode=0, stdout="org = 1\nlab = solar-system\n")),
    ("kickstart_listactivationkeys", FakeResult(returncode=0, stdout="1-key\n")),
    ("kickstart_listchildchannels", FakeResult(returncode=0, stdout="")),
    ("kickstart_list", FakeResult(returncode=0, stdout="test-ks\n")),
])
sc.ssh_run = fake
sc.ensure_kickstart_profile("host1", "mgrctl exec --", {
    "name": "test-ks", "distribution": "test-dist", "root_password": "pw",
    "variables": {"lab": "solar-system"}, "activation_keys": ["1-key"],
})
cmds = [c[1] for c in fake.calls]
check("ensure_kickstart_profile: existing profile + already-set variable/key -> no writes at all",
      not any("kickstart_create" in c or "kickstart_addvariable" in c or "kickstart_addactivationkeys" in c
              for c in cmds))

# -- snippet_file_path / ensure_snippet / ensure_snippets (added 2026-09-24) --
# Real, live-grounded 2026-09-24: spacecmd's native snippet_create is
# interactive ("Is this ok [y/N]:", confirmed live — no -y/--yes flag,
# "ERROR: unrecognized arguments: -y") and re-running it against an EXISTING
# name cleanly overwrites (no separate update command — confirmed absent).
# Idempotency is checked against the snippet's own real file content, whose
# path (varies by org id) comes from snippet_details' own "File:" line.
_REAL_SNIPPET_DETAILS = (
    "Name:   test-example\n"
    "Macro:  $SNIPPET('spacewalk/1/test-example')\n"
    "File:   /var/lib/cobbler/snippets/spacewalk/1/test-example\n"
)

fake = FakeSSH(responses=[("snippet_details", FakeResult(returncode=0, stdout=_REAL_SNIPPET_DETAILS))])
sc.ssh_run = fake
check("snippet_file_path: parses the real absolute path off the 'File:' line",
      sc.snippet_file_path("host1", "mgrctl exec --", "test-example")
      == "/var/lib/cobbler/snippets/spacewalk/1/test-example")

fake = FakeSSH(responses=[("snippet_details", FakeResult(returncode=1, stdout="",
                                                           stderr="WARNING: nosuch is not a valid snippet"))])
sc.ssh_run = fake
check("snippet_file_path: returns None for a snippet that doesn't exist",
      sc.snippet_file_path("host1", "mgrctl exec --", "nosuch") is None)

# Already up to date: real content matches -> no create/confirm round trip.
fake = FakeSSH(responses=[
    ("snippet_details", FakeResult(returncode=0, stdout=_REAL_SNIPPET_DETAILS)),
    ("cat /var/lib/cobbler/snippets/spacewalk/1/test-example",
     FakeResult(returncode=0, stdout="echo hi\n")),
])
sc.ssh_run = fake
sc.ensure_snippet("host1", "mgrctl exec --", "test-example", "echo hi\n")
check("ensure_snippet: already-matching content is a no-op (no snippet_create call)",
      not any("snippet_create" in c[1] for c in fake.calls))

# Doesn't exist yet -> stages content, creates, confirms with 'y', cleans up.
fake = FakeSSH(responses=[("snippet_details", FakeResult(returncode=1, stdout="", stderr="not a valid snippet"))])
sc.ssh_run = fake
sc.ensure_snippet("host1", "mgrctl exec --", "new-snippet", "echo new\n")
cmds_and_kwargs = [(c[1], c[2]) for c in fake.calls]
stage_call = next((c for c, kw in cmds_and_kwargs if "cat >" in c), None)
create_call = next(((c, kw) for c, kw in cmds_and_kwargs if "snippet_create -n new-snippet -f" in c), None)
check("ensure_snippet: stages the real content to a remote temp file first",
      stage_call is not None)
check("ensure_snippet: creates via spacecmd's own stored session (NEVER -u/-p in argv — a real "
      "security regression caught here: this project's own ensure_spacecmd_config exists "
      "specifically to keep credentials out of argv/`ps` output)",
      create_call is not None and " -u " not in create_call[0] and " -p " not in create_call[0])
check("ensure_snippet: confirms the interactive 'Is this ok' prompt with a real 'y' on stdin",
      create_call is not None and create_call[1].get("input_text") == "y\n")
check("ensure_snippet: cleans up its own remote staging file afterward",
      any("rm -f /tmp/.lab-snippet-" in c for c, kw in cmds_and_kwargs))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_snippets("host1", "mgrctl exec --", {}, "smlm")
check("ensure_snippets: no-op when the field is unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_snippets("host1", "mgrctl exec --", {"smlm_snippets": [{"content": "x"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_snippets: an entry missing 'name' dies", died)

died = False
try:
    sc.ensure_snippets("host1", "mgrctl exec --", {"smlm_snippets": [{"name": "x"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_snippets: an entry missing 'content' dies", died)


# -- image stores / profiles / import ------------------------------------------
fake = FakeSSH(responses=[("image.store.listImageStores", FakeResult(returncode=0, stdout="[]"))])
sc.ssh_run = fake
sc.ensure_image_store("host1", "mgrctl exec --", {
    "label": "suse-registry", "uri": "registry.suse.com", "type": "registry",
})
create_cmd = next((c[1] for c in fake.calls if "image.store.create" in c[1]), "")
check("ensure_image_store: create call carries label/uri/type/empty-credentials as JSON",
      '["suse-registry", "registry.suse.com", "registry", {}]' in create_cmd)

fake = FakeSSH(responses=[("image.store.listImageStores", FakeResult(
    returncode=0, stdout=json.dumps([{"label": "suse-registry"}])))])
sc.ssh_run = fake
sc.ensure_image_store("host1", "mgrctl exec --", {"label": "suse-registry"})
check("ensure_image_store: existing store -> no create call",
      not any("image.store.create" in c[1] for c in fake.calls))

fake = FakeSSH(responses=[("image.profile.listImageProfiles", FakeResult(returncode=0, stdout="[]"))])
sc.ssh_run = fake
sc.ensure_image_profile("host1", "mgrctl exec --", {
    "label": "test-profile", "type": "dockerfile", "store": "suse-registry",
    "path": "https://github.com/x/y.git#main:docker", "activation_key": "1-key",
})
create_cmd = next((c[1] for c in fake.calls if "image.profile.create" in c[1]), "")
check("ensure_image_profile: create call carries every field in the right order",
      '["test-profile", "dockerfile", "suse-registry", '
      '"https://github.com/x/y.git#main:docker", "1-key"]' in create_cmd)

fake = FakeSSH(responses=[("image.importContainerImage", FakeResult(returncode=0, stdout="[42]"))])
sc.ssh_run = fake
sc.import_container_image("host1", "mgrctl exec --", "bci/bci-base", "latest", 1000010000,
                           "suse-registry", "1-key")
call = next((c[1] for c in fake.calls if "image.importContainerImage" in c[1]), "")
check("import_container_image: schedules with name/version/build_host_id/store/activation_key",
      '["bci/bci-base", "latest", 1000010000, "suse-registry", "1-key", null]' in call)

fake = FakeSSH()
sc.ssh_run = fake
sc.import_images("host1", "mgrctl exec --", {}, "smlm")
check("import_images: no-op (and no die) when smlm_image_imports is unset", len(fake.calls) == 0)

# -- server monitoring ----------------------------------------------------------
fake = FakeSSH(responses=[("admin.monitoring.getStatus", FakeResult(
    returncode=0, stdout=json.dumps([{"node": "disabled", "tomcat": "disabled"}])))])
sc.ssh_run = fake
sc.ensure_monitoring("host1", "mgrctl exec --", {"smlm_monitoring_enabled": "true"}, "smlm")
cmds = [c[1] for c in fake.calls]
check("ensure_monitoring: enables when the flag is set and status shows disabled",
      any("admin.monitoring.enable" in c for c in cmds))
check("ensure_monitoring: restarts tomcat/taskomatic right after a fresh enable",
      any("systemctl restart tomcat taskomatic" in c for c in cmds))

fake = FakeSSH(responses=[("admin.monitoring.getStatus", FakeResult(
    returncode=0, stdout=json.dumps([{"node": "enabled", "tomcat": "enabled"}])))])
sc.ssh_run = fake
sc.ensure_monitoring("host1", "mgrctl exec --", {"smlm_monitoring_enabled": "true"}, "smlm")
check("ensure_monitoring: already enabled -> no enable/restart calls at all", len(fake.calls) == 1)

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_monitoring("host1", "mgrctl exec --", {}, "smlm")
check("ensure_monitoring: no-op when the flag is unset", len(fake.calls) == 0)

# -- _system_id -----------------------------------------------------------
fake = FakeSSH(responses=[("system.getId", FakeResult(
    returncode=0, stdout=json.dumps([{"id": 1000010042, "name": "sol.mydemo.lab"}])))])
sc.ssh_run = fake
check("_system_id: resolves the numeric id from system.getId's real response shape",
      sc._system_id("host1", "mgrctl exec --", "sol.mydemo.lab") == 1000010042)

fake = FakeSSH(responses=[("system.getId", FakeResult(returncode=0, stdout=json.dumps([])))])
sc.ssh_run = fake
died = False
try:
    sc._system_id("host1", "mgrctl exec --", "nosuch.lab")
except SystemExit:
    died = True
check("_system_id: zero matches dies", died)

fake = FakeSSH(responses=[("system.getId", FakeResult(
    returncode=0, stdout=json.dumps([{"id": 1}, {"id": 2}])))])
sc.ssh_run = fake
died = False
try:
    sc._system_id("host1", "mgrctl exec --", "ambiguous.lab")
except SystemExit:
    died = True
check("_system_id: more than one match dies (genuinely ambiguous)", died)

fake = FakeSSH(responses=[("system.getId", FakeResult(returncode=1, stderr="no such method"))])
sc.ssh_run = fake
died = False
try:
    sc._system_id("host1", "mgrctl exec --", "sol.mydemo.lab")
except SystemExit:
    died = True
check("_system_id: server-side failure dies", died)

# -- ensure_grafana_formula -----------------------------------------------
fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42, "name": "sol.mydemo.lab"}]))),
])
sc.ssh_run = fake
cfg = {"smlm_grafana_formulas": [{"system": "sol.mydemo.lab", "admin_pass": "GrafanaPw1",
                                   "prometheus": [{"key": "Prometheus", "url": "http://sol.mydemo.lab:9090"}],
                                   "reportdb": True, "is_hub": True}]}
sc.ensure_grafana_formula("host1", "mgrctl exec --", cfg, "smlm")
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_grafana_formula: resolves the target system's id first",
      any("system.getId" in c for c in cmds))
check("ensure_grafana_formula: enables the real 'grafana' formula name via setFormulasOfServer",
      any("formula.setFormulasOfServer" in c and '[42, ["grafana"]]' in c for c in cmds))
check("ensure_grafana_formula: configures it via setSystemFormulaData with the real pillar shape",
      any("formula.setSystemFormulaData" in c and '"admin_pass": "GrafanaPw1"' in c
          and '"url": "http://sol.mydemo.lab:9090"' in c
          and '"reportdb": {"enabled": true, "is_hub": true}' in c for c in cmds))
check("ensure_grafana_formula: real dashboard pillar keys default true (incl. the formula's own "
      "real 'add_postgresql_dasboard' typo, not a corrected spelling)",
      any('"add_uyuni_dashboard": true' in c and '"add_uyuni_clients_dashboard": true' in c
          and '"add_postgresql_dasboard": true' in c and '"add_apache_dashboard": true' in c
          for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_grafana_formula("host1", "mgrctl exec --", {}, "smlm")
check("ensure_grafana_formula: no-op when the field is unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_grafana_formula("host1", "mgrctl exec --", {"smlm_grafana_formulas": [{}]}, "smlm")
except SystemExit:
    died = True
check("ensure_grafana_formula: entry missing 'system' dies", died)

fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42}]))),
    ("formula.setFormulasOfServer", FakeResult(returncode=1, stderr="no monitoring subscription")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_grafana_formula("host1", "mgrctl exec --",
                               {"smlm_grafana_formulas": [{"system": "sol.mydemo.lab"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_grafana_formula: a real API failure (e.g. missing subscription) dies with a clear "
      "message, not silently ignored", died)

# -- ensure_ansible_control_node (added 2026-09-18) ------------------------
fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42, "name": "charon.mydemo.lab"}]))),
])
sc.ssh_run = fake
cfg = {"smlm_ansible_control_nodes": [{"system": "charon.mydemo.lab"}]}
sc.ensure_ansible_control_node("host1", "mgrctl exec --", cfg, "smlm")
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_ansible_control_node: resolves the target system's id first",
      any("system.getId" in c for c in cmds))
check("ensure_ansible_control_node: enables the real 'ansible_control_node' entitlement label",
      any("system.addEntitlements" in c and '[42, ["ansible_control_node"]]' in c for c in cmds))
check("ensure_ansible_control_node: schedules a highstate apply so 'ansible' actually gets installed",
      any("system.scheduleApplyHighstate" in c and '[[42], "' in c and ', false]' in c
          for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_ansible_control_node("host1", "mgrctl exec --", {}, "smlm")
check("ensure_ansible_control_node: no-op when the field is unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_ansible_control_node("host1", "mgrctl exec --", {"smlm_ansible_control_nodes": [{}]}, "smlm")
except SystemExit:
    died = True
check("ensure_ansible_control_node: entry missing 'system' dies", died)

fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42}]))),
    ("system.addEntitlements", FakeResult(returncode=1, stderr="not a salt-entitled system")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_ansible_control_node(
        "host1", "mgrctl exec --", {"smlm_ansible_control_nodes": [{"system": "charon.mydemo.lab"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_ansible_control_node: a real API failure dies with a clear message, not silently "
      "ignored", died)


# -- ensure_container_build_hosts (added 2026-09-24) ------------------------
fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42, "name": "mercury.mydemo.lab"}]))),
])
sc.ssh_run = fake
cfg = {"smlm_image_build_hosts": [{"system": "mercury.mydemo.lab"}]}
sc.ensure_container_build_hosts("host1", "mgrctl exec --", cfg, "smlm")
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_container_build_hosts: resolves the target system's id first",
      any("system.getId" in c for c in cmds))
check("ensure_container_build_hosts: enables the real 'container_build_host' entitlement label",
      any("system.addEntitlements" in c and '[42, ["container_build_host"]]' in c for c in cmds))
check("ensure_container_build_hosts: schedules a highstate apply so build tooling actually "
      "gets installed",
      any("system.scheduleApplyHighstate" in c and '[[42], "' in c and ', false]' in c
          for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_container_build_hosts("host1", "mgrctl exec --", {}, "smlm")
check("ensure_container_build_hosts: no-op when the field is unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_container_build_hosts("host1", "mgrctl exec --", {"smlm_image_build_hosts": [{}]}, "smlm")
except SystemExit:
    died = True
check("ensure_container_build_hosts: entry missing 'system' dies", died)

fake = FakeSSH(responses=[
    ("system.getId", FakeResult(returncode=0, stdout=json.dumps([{"id": 42}]))),
    ("system.addEntitlements", FakeResult(returncode=1, stderr="not a salt-entitled system")),
])
sc.ssh_run = fake
died = False
try:
    sc.ensure_container_build_hosts(
        "host1", "mgrctl exec --", {"smlm_image_build_hosts": [{"system": "mercury.mydemo.lab"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_container_build_hosts: a real API failure dies with a clear message, not silently "
      "ignored", died)


# -- run_provisioning_step (added 2026-09-23) -------------------------------
# Real bug: install_smlm.py's/install_uyuni.py's orchestration blocks used to
# call each ensure_* step bare, so one die() (SystemExit) silently aborted
# every step queued after it — confirmed live 2026-09-23,
# ensure_ansible_control_node()'s "no system named 'charon.mydemo.lab' found
# on the server" wiped out ensure_orgs() (the lab's "edge" org + 28 users)
# several steps later. These tests avoid real time.sleep() by monkeypatching
# sc.time.sleep.
_real_sleep = sc.time.sleep
_sleep_calls = []
sc.time.sleep = lambda s: _sleep_calls.append(s)

calls = []


def _ok(*a, **kw):
    calls.append(("ok", a, kw))


def _always_dies(*a, **kw):
    calls.append(("die", a, kw))
    lab_creation.die("simulated failure")


calls.clear()
sc.run_provisioning_step("succeeds", _ok, "host1", x=1)
check("run_provisioning_step: a successful step is called exactly once", len(calls) == 1)

calls.clear()
_sleep_calls.clear()
sc.run_provisioning_step("no retry by default", _always_dies, "host1")
check("run_provisioning_step: default retries=1 means the step is attempted exactly once",
      len(calls) == 1)
check("run_provisioning_step: default retries=1 never sleeps", _sleep_calls == [])

calls.clear()
_sleep_calls.clear()
sc.run_provisioning_step("retries then still fails", _always_dies, "host1", retries=3, retry_delay=15)
check("run_provisioning_step: retries=3 attempts the step exactly 3 times",
      len(calls) == 3)
check("run_provisioning_step: sleeps retry_delay between attempts, not after the last one",
      _sleep_calls == [15, 15])

_flaky_state = {"n": 0}


def _flaky(*a, **kw):
    _flaky_state["n"] += 1
    calls.append(("flaky", _flaky_state["n"]))
    if _flaky_state["n"] < 3:
        lab_creation.die("still not ready")


calls.clear()
_sleep_calls.clear()
_flaky_state["n"] = 0
sc.run_provisioning_step("succeeds on a later attempt", _flaky, "host1", retries=5, retry_delay=20)
check("run_provisioning_step: a step that fails twice then succeeds stops retrying once it succeeds",
      len(calls) == 3)
check("run_provisioning_step: only slept for the 2 failed attempts, not a 3rd time after success",
      _sleep_calls == [20, 20])

sc.time.sleep = _real_sleep

# -- Virtual Host Managers (added 2026-09-23) --------------------------------
fake = FakeSSH(responses=[
    ("virtualhostmanager.listVirtualHostManagers", FakeResult(returncode=0, stdout="[]")),
    ("virtualhostmanager.create", FakeResult(returncode=0, stdout="1")),
])
sc.ssh_run = fake
vhm = {"label": "aws-vhm", "access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cr3t",
       "region": "eu-central-1", "zone": "eu-central-1a"}
sc.ensure_virtual_host_manager_aws("host1", "mgrctl exec --", vhm)
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_virtual_host_manager_aws: checks for an existing VHM by label first",
      any("virtualhostmanager.listVirtualHostManagers" in c for c in cmds))
check("ensure_virtual_host_manager_aws: creates via the real moduleName 'AmazonEC2'",
      any("virtualhostmanager.create" in c and '"aws-vhm", "AmazonEC2"' in c for c in cmds))
check("ensure_virtual_host_manager_aws: sends the real 4 gatherer param keys",
      any("access_key_id" in c and "secret_access_key" in c and '"region": "eu-central-1"' in c
          and '"zone": "eu-central-1a"' in c for c in cmds))

fake = FakeSSH(responses=[
    ("virtualhostmanager.listVirtualHostManagers",
     FakeResult(returncode=0, stdout='[{"label": "aws-vhm"}]')),
])
sc.ssh_run = fake
sc.ensure_virtual_host_manager_aws("host1", "mgrctl exec --", vhm)
cmds = [unwrap(c[1]) for c in fake.calls]
check("ensure_virtual_host_manager_aws: an already-existing VHM is left alone, not re-created",
      not any("virtualhostmanager.create" in c for c in cmds))

fake = FakeSSH()
sc.ssh_run = fake
sc.ensure_virtual_host_managers("host1", "mgrctl exec --", {}, "smlm")
check("ensure_virtual_host_managers: no-op when the field is unset", len(fake.calls) == 0)

died = False
try:
    sc.ensure_virtual_host_managers(
        "host1", "mgrctl exec --", {"smlm_virtual_host_managers": [{"label": "x", "type": "vmware"}]}, "smlm")
except SystemExit:
    died = True
check("ensure_virtual_host_managers: an unsupported type dies with a clear message", died)

died = False
try:
    sc.ensure_virtual_host_manager_aws("host1", "mgrctl exec --", {"label": "incomplete"})
except SystemExit:
    died = True
check("ensure_virtual_host_manager_aws: missing credentials/region/zone dies", died)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all spacecmd_common checks passed")
