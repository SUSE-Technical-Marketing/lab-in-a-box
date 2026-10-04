#!/usr/bin/env python3
# Unit tests for install_smlm.py's traditional mgradm and podman deployment (smlm_deployment: "podman"). The install uses the
# shared helpers in libs/mgradm_common.py, which these tests mock rather than run. install_uyuni.py is not changed by it.
# Run from 49_smlm_baremetal.sh, in its own container (see tests/run_tests.sh).
import re
import sys
import types
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import install_smlm as ism  # noqa: E402
import mgradm_common  # noqa: E402

failures = []
original_die = ism.die


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── _validate(): deployment-mode-dependent required fields ─────────────────
podman_def = {
    "smlm": {
        "smlm_deployment": "podman",
        "smlm_scc_regcode": "REGCODE",
        "smlm_scc_product": "PRODUCT",
    }
}
v = ac.Validator(podman_def)
ism._validate(v)
check("_validate: podman mode with regcode+product set has no errors", v.errors == [])

podman_missing = {"smlm": {"smlm_deployment": "podman"}}
v2 = ac.Validator(podman_missing)
ism._validate(v2)
check("_validate: podman mode without smlm_scc_regcode reports it missing",
      any("smlm_scc_regcode" in e for e in v2.errors))
check("_validate: podman mode does NOT require smlm_scc_product (has a real default)",
      not any("smlm_scc_product" in e for e in v2.errors))
check("_validate: podman mode does NOT require smlm_fqdn/smlm_scc_user/smlm_scc_password",
      not any("smlm_fqdn" in e or "smlm_scc_user" in e or "smlm_scc_password" in e for e in v2.errors))

podman_channels_no_creds = {
    "smlm": {
        "smlm_deployment": "podman",
        "smlm_scc_regcode": "REGCODE",
        "smlm_channels": "some-channel",
    }
}
v2b = ac.Validator(podman_channels_no_creds)
ism._validate(v2b)
check("_validate: podman mode with smlm_channels set but no scc_user/password reports both missing",
      any("smlm_scc_user" in e for e in v2b.errors) and any("smlm_scc_password" in e for e in v2b.errors))

k8s_def = {"smlm": {"smlm_fqdn": "smlm.lab", "smlm_scc_user": "u", "smlm_scc_password": "p"}}
v3 = ac.Validator(k8s_def)
ism._validate(v3)
check("_validate: default (kubernetes) mode with fqdn/scc_user/scc_password set has no errors", v3.errors == [])

k8s_missing = ac.Validator({"smlm": {}})
ism._validate(k8s_missing)
check("_validate: default (kubernetes) mode without required fields reports smlm_fqdn missing",
      any("smlm_fqdn" in e for e in k8s_missing.errors))
check("_validate: default (kubernetes) mode does NOT require smlm_scc_regcode/product",
      not any("smlm_scc_regcode" in e or "smlm_scc_product" in e for e in k8s_missing.errors))


# ── setup_smlm_podman(): SCC registration + package-install branching ──────
def run_setup_smlm_podman(cfg, transactional, already_initialized=False):
    calls = []
    inputs = {}

    def fake_ssh_run(hostname, cmd, check=True, capture=False, input_text=None):
        calls.append(cmd)
        if input_text is not None:
            inputs[cmd] = input_text
        if "command -v transactional-update" in cmd:
            return FakeResult(returncode=0 if transactional else 1)
        if "mgr-sync list channels" in cmd:
            return FakeResult(returncode=0, stdout="some-channel\n")
        if "podman container exists uyuni-server" in cmd:
            return FakeResult(returncode=0 if already_initialized else 1)
        return FakeResult(returncode=0)

    ism.ssh_run = fake_ssh_run
    ism.reboot_vm = lambda virt_srv, hostname: calls.append("REBOOT:{}".format(hostname))
    ism.check_ssh_conn = lambda hostname: calls.append("CHECK_SSH:{}".format(hostname))
    ism.time.sleep = lambda s: None

    guard_calls = []
    active_calls = []
    mgradm_common.run_install_with_pg_hba_guard = lambda hostname, cmd: guard_calls.append((hostname, cmd))
    mgradm_common.ensure_server_container_active = lambda hostname: active_calls.append(hostname)

    sc_calls = []
    for name in ("ensure_spacecmd_config", "ensure_channels_synced", "ensure_config_channels",
                 "ensure_activation_key", "ensure_appstreams", "ensure_activation_key_packages",
                 "ensure_activation_keys", "ensure_access_groups", "ensure_ansible_paths",
                 "ensure_content_projects", "ensure_system_groups", "ensure_custom_info_keys",
                 "ensure_system_tags", "ensure_environments", "ensure_orgs",
                 "ensure_monitoring", "ensure_distributions", "ensure_image_stores",
                 "ensure_image_profiles", "ensure_kickstart_profiles", "ensure_users",
                 "ensure_ansible_control_node", "ensure_grafana_formula",
                 "ensure_virtual_host_managers", "ensure_snippets",
                 "ensure_container_build_hosts", "ensure_mcp_server",
                 "ensure_system_custom_values", "ensure_custom_channels", "ensure_patches",
                 "ensure_recurring_schedules", "ensure_maintenance_calendars",
                 "ensure_maintenance_schedules", "ensure_action_chains",
                 "ensure_system_profiles", "ensure_org_system_transfers"):
        setattr(ism.sc, name, (lambda n: lambda *a, **k: sc_calls.append((n, a, k)))(name))

    ism.setup_smlm_podman("sol.mydemo.lab", "hypervisor1", cfg)
    return calls, guard_calls, active_calls, sc_calls, inputs


cfg = {
    "smlm_scc_regcode": "REGCODE123",
    "smlm_scc_product": "SUSE-Manager-Server/5.2/x86_64",
    "smlm_scc_user": "sccuser",
    "smlm_scc_password": "sccpass",
    "smlm_email": "admin@lab.local",
    "smlm_admin": "admin",
    "smlm_password": "Smlm12345",
    "smlm_org": "lab",
}

calls, guard_calls, active_calls, sc_calls, _inputs = run_setup_smlm_podman(cfg, transactional=True)

check("setup_smlm_podman: registers the base product via SUSEConnect -r (no -e — not in the real docs)",
      any(c == "SUSEConnect -r REGCODE123" for c in calls))
check("setup_smlm_podman: registers the free containers module (needed as a prerequisite for every "
      "base OS, not just plain SLES) before the SMLM extension module",
      any(c == "SUSEConnect -p sle-module-containers/15.7/x86_64" for c in calls))
check("setup_smlm_podman: registers the SMLM extension module via SUSEConnect -p ... -r (both, per real docs)",
      any(c == "SUSEConnect -p SUSE-Manager-Server/5.2/x86_64 -r REGCODE123" for c in calls))
check("setup_smlm_podman: the containers module is registered BEFORE the SMLM extension module — "
      "confirmed live 2026-09-13: SCC's own server rejects the SMLM module (422, 'requires... "
      "Containers Module... to be activated first') if attempted in the other order",
      calls.index("SUSEConnect -p sle-module-containers/15.7/x86_64")
      < calls.index("SUSEConnect -p SUSE-Manager-Server/5.2/x86_64 -r REGCODE123"))
check("setup_smlm_podman: logs into registry.suse.com when smlm_scc_user/password are set",
      any("podman login -u sccuser --password-stdin registry.suse.com" in c for c in calls))
check("setup_smlm_podman: transactional host uses transactional-update pkg install",
      any(c.startswith("transactional-update --quiet pkg install") and "mgradm" in c for c in calls))
check("setup_smlm_podman: transactional host reboots to apply the new snapshot",
      any(c == "REBOOT:sol.mydemo.lab" for c in calls))
check("setup_smlm_podman: the mgradm install runs through mgradm_common's pg_hba-guard helper",
      len(guard_calls) == 1 and guard_calls[0][0] == "sol.mydemo.lab")
check("setup_smlm_podman: install command uses the flag-only mgradm form (no FQDN positional)",
      "mgradm install podman" in guard_calls[0][1]
      and "--admin-login admin" in guard_calls[0][1]
      and "--organization lab" in guard_calls[0][1])

# A previous run can leave a healthy server behind after mgradm install failed on an optional image. Retrying mgradm install
# is refused in that state, so setup_smlm_podman() detects an existing uyuni-server container and runs the health checks and
# post-install steps, without another install attempt.
calls_resume, guard_calls_resume, active_calls_resume, _, _ = run_setup_smlm_podman(
    cfg, transactional=True, already_initialized=True)
check("setup_smlm_podman: an already-initialized server (uyuni-server container exists) "
      "never re-attempts mgradm install", guard_calls_resume == [])
check("setup_smlm_podman: an already-initialized server skips the post-mgradm-install reboot "
      "too (the plain ssh 'reboot' call right after run_install_with_pg_hba_guard — distinct "
      "from the earlier transactional-update package-install reboot via reboot_vm(), which "
      "still runs regardless since installing the mgradm tooling itself is untouched by this fix)",
      "reboot" not in calls_resume)
check("setup_smlm_podman: an already-initialized server still runs the health-check/recovery step",
      active_calls_resume == ["sol.mydemo.lab"])
check("setup_smlm_podman: an already-initialized server still registers SCC/containers modules "
      "(idempotent, safe to repeat, and needed if THIS run is what's actually retrying after a "
      "transient SCC failure rather than the mgradm crash)",
      any(c == "SUSEConnect -r REGCODE123" for c in calls_resume))

# The --organization value is shell-quoted, so a value with spaces stays one argument.
cfg_org_with_space = dict(cfg, smlm_org="SUSE Test")
_, guard_calls_org, _, _, _ = run_setup_smlm_podman(cfg_org_with_space, transactional=True)
check("setup_smlm_podman: a multi-word smlm_org is shell-quoted as ONE argument, not split",
      "--organization 'SUSE Test'" in guard_calls_org[0][1])
check("setup_smlm_podman: waits for the server container via mgradm_common's own helper",
      active_calls == ["sol.mydemo.lab"])
check("setup_smlm_podman: with no activation keys configured, spacecmd config is never touched",
      sc_calls == [])
check("setup_smlm_podman: with no smlm_channels/activation_keys configured, "
      "mgr-sync credentials are never registered",
      not any("mgr-sync add credentials" in c for c in calls))

calls2, _, _, _, _ = run_setup_smlm_podman(cfg, transactional=False)
check("setup_smlm_podman: non-transactional (plain SLES) host uses zypper install, not transactional-update",
      any(c.startswith("zypper --non-interactive install") and "mgradm" in c for c in calls2)
      and not any(c.startswith("transactional-update") for c in calls2))
check("setup_smlm_podman: non-transactional host never reboots for the package install itself",
      calls2.count("REBOOT:sol.mydemo.lab") == 0)
check("setup_smlm_podman: non-transactional host explicitly installs podman itself",
      any(c == "zypper --non-interactive install -y podman" for c in calls2))
check("setup_smlm_podman: non-transactional host enables the podman socket",
      any("podman.socket" in c for c in calls2))

cfg_no_registry_creds = {k: v for k, v in cfg.items() if k not in ("smlm_scc_user", "smlm_scc_password")}
calls3, _, _, _, _ = run_setup_smlm_podman(cfg_no_registry_creds, transactional=True)
check("setup_smlm_podman: with no smlm_scc_user/password, skips the registry login (no podman login call)",
      not any("podman login" in c for c in calls3))

cfg_no_product = {k: v for k, v in cfg.items() if k != "smlm_scc_product"}
calls4, _, _, _, _ = run_setup_smlm_podman(cfg_no_product, transactional=True)
check("setup_smlm_podman: with smlm_scc_product unset, falls back to the real confirmed default identifier",
      any(c == "SUSEConnect -p Multi-Linux-Manager-Server-SLE/5.2/x86_64 -r REGCODE123" for c in calls4))

cfg_with_keys = dict(cfg)
cfg_with_keys["smlm_activation_keys"] = [{"smlm_activation_key": "k1", "smlm_activation_key_base_channel": "c1"}]
calls_keys, _, _, sc_calls_keys, inputs_keys = run_setup_smlm_podman(cfg_with_keys, transactional=True)
prefixes_used = set()
for name, args, kwargs in sc_calls_keys:
    if name == "ensure_activation_keys":
        prefixes_used.add(args[-1] if args else kwargs.get("prefix"))
check("setup_smlm_podman: activation keys present -> spacecmd config applied with prefix 'smlm'",
      any(name == "ensure_activation_keys" for name, _, _ in sc_calls_keys)
      and all(("smlm" in a) for name, a, k in sc_calls_keys if name == "ensure_activation_keys" for a in [a]))
check("setup_smlm_podman: activation keys present -> registers SCC organization credentials with "
      "mgr-sync, forwarding stdin via mgrctl's -i flag",
      any(c == "mgrctl exec -i -- mgr-sync add credentials" for c in calls_keys))
check("setup_smlm_podman: mgr-sync credentials command is fed the LOCAL admin login/password "
      "first, then the SCC user/password/password-confirmation — confirmed live 2026-09-14 "
      "that `mgr-sync add credentials` actually prompts for all five, in that order (a local "
      "admin Login/Password pair, then SCC \"User to add:\"/\"Password to add:\"/\"Confirm "
      "password:\"); feeding only 2 or 4 lines (both earlier guesses) left a later prompt "
      "waiting forever and the call died silently",
      inputs_keys.get("mgrctl exec -i -- mgr-sync add credentials")
      == "admin\nSmlm12345\nsccuser\nsccpass\nsccpass\n")

# smlm_channels is a JSON array in real lab definitions. The command must be built from the list, not from the list's repr.
# This test uses a list, so that case is covered.
cfg_with_channel_list = dict(cfg, smlm_channels=["chan-a", "chan-b"])
calls_chanlist, _, _, _, _ = run_setup_smlm_podman(cfg_with_channel_list, transactional=True)
check("setup_smlm_podman: a real (list-shaped) smlm_channels is space-joined into the mgr-sync "
      "add channels command, not stringified as a Python list repr",
      any(c == "mgrctl exec -- mgr-sync add channels chan-a chan-b" for c in calls_chanlist))
check("setup_smlm_podman: the malformed Python-list-repr form never appears",
      not any("['chan-a', 'chan-b']" in c for c in calls_chanlist))

# The mgr-sync credentials step does not fill the product and channel catalog. A separate refresh is needed, and the wait loop
# has a timeout, so the install does not wait for the server's own scheduled job.
check("setup_smlm_podman: explicitly refreshes mgr-sync's catalog before waiting on the "
      "channel list, rather than hoping the server's own background job already ran",
      "mgrctl exec -- mgr-sync refresh" in calls_chanlist
      and calls_chanlist.index("mgrctl exec -- mgr-sync refresh")
      < calls_chanlist.index("mgrctl exec -- mgr-sync list channels 2>/dev/null"))

# The wait loop has a bounded retry count, so a permanent entitlement problem cannot hang the addon.
def _run_setup_smlm_podman_channels_never_sync(cfg):
    def fake_ssh_run(hostname, cmd, check=True, capture=False, input_text=None):
        if "command -v transactional-update" in cmd:
            return FakeResult(returncode=0)
        if "mgr-sync list channels" in cmd:
            return FakeResult(returncode=0, stdout="No channels found.\n")
        if "podman container exists uyuni-server" in cmd:
            return FakeResult(returncode=1)
        return FakeResult(returncode=0)

    ism.ssh_run = fake_ssh_run
    ism.reboot_vm = lambda virt_srv, hostname: None
    ism.check_ssh_conn = lambda hostname: None
    ism.time.sleep = lambda s: None
    mgradm_common.run_install_with_pg_hba_guard = lambda hostname, cmd: None
    mgradm_common.ensure_server_container_active = lambda hostname: None
    ism.setup_smlm_podman("sol.mydemo.lab", "hypervisor1", cfg)


died_timeout = False
try:
    _run_setup_smlm_podman_channels_never_sync(cfg_with_channel_list)
except SystemExit:
    died_timeout = True
check("setup_smlm_podman: dies with a clear message instead of hanging forever when the "
      "channel list is STILL empty after the refresh and a bounded number of retries — a "
      "real permanent problem, not just first-refresh latency, must surface as an error",
      died_timeout)

# An activation key's child channels must be synced, not only referenced. A channel that is not on the server makes the link call
# fail, and clients get the wrong tooling package. The test checks that the child channels are added to the sync list.
cfg_with_child_channels = dict(
    cfg,
    smlm_channels=["chan-a"],
    smlm_activation_keys=[{
        "smlm_activation_key": "k1",
        "smlm_activation_key_base_channel": "chan-a",
        "smlm_activation_key_child_channels": "managertools-sle15-pool-x86_64-sp7 chan-a",
    }],
)
calls_childchan, _, _, _, _ = run_setup_smlm_podman(cfg_with_child_channels, transactional=True)
mgr_sync_call = next((c for c in calls_childchan if c.startswith("mgrctl exec -- mgr-sync add channels")), "")
check("setup_smlm_podman: an activation key's own child channels are folded into what actually "
      "gets synced, not just referenced by the key",
      "managertools-sle15-pool-x86_64-sp7" in mgr_sync_call)
check("setup_smlm_podman: a child channel already present in smlm_channels isn't duplicated",
      mgr_sync_call.count("chan-a") == 1)

# ensure_channel_sync_monitor is deployed whenever channels are configured. A restart can leave a reposync without a completion
# marker and without an error, so the monitor runs on its own schedule.
monitor_script_call = next(
    (c for c in calls_chanlist if c.startswith("cat > /usr/local/sbin/smlm-channel-sync-monitor.sh")), None)
check("setup_smlm_podman: deploys the channel-sync-monitor script when channels are configured",
      monitor_script_call is not None)
check("setup_smlm_podman: the monitor script is substituted with the real admin credentials, "
      "not left as a template placeholder",
      monitor_script_call is not None and "__ADMIN__" not in monitor_script_call
      and "__PASSWORD__" not in monitor_script_call
      and "ADMIN=admin" in monitor_script_call and "PASSWORD=Smlm12345" in monitor_script_call)
check("setup_smlm_podman: the monitor script re-triggers a sync via spacecmd softwarechannel_syncrepos "
      "(NOT `mgr-sync sync`, which needs the same fragile interactive multi-round prompt as "
      "`mgr-sync add credentials` — unsafe to script unattended)",
      monitor_script_call is not None and "softwarechannel_syncrepos" in monitor_script_call
      and "mgr-sync sync" not in monitor_script_call)
check("setup_smlm_podman: the monitor script treats a channel as healthy only when its reposync "
      "log actually ends with the real completion marker",
      monitor_script_call is not None and "Sync completed." in monitor_script_call)
check("setup_smlm_podman: deploys the systemd service unit for the monitor",
      any(c.startswith("cat > /etc/systemd/system/smlm-channel-sync-monitor.service") for c in calls_chanlist))
check("setup_smlm_podman: deploys the systemd timer unit, on a recurring (not one-shot) schedule",
      any(c.startswith("cat > /etc/systemd/system/smlm-channel-sync-monitor.timer") and "OnUnitActiveSec="
          in c for c in calls_chanlist))
check("setup_smlm_podman: enables and starts the timer (not just installs it inert)",
      any("systemctl enable --now smlm-channel-sync-monitor.timer" in c for c in calls_chanlist))

# spacewalk-repo-sync allows one instance on the server, and a second one fails. The monitor triggers at most one channel per run,
# and only when nothing is syncing, so each trigger has a real chance to run.
check("channel-sync-monitor script checks for an already-running reposync before "
      "triggering anything, to avoid colliding with itself",
      monitor_script_call is not None
      and "pgrep -f spacewalk-repo-sync" in monitor_script_call
      and monitor_script_call.index("pgrep -f spacewalk-repo-sync")
      < monitor_script_call.index("softwarechannel_syncrepos"))
check("channel-sync-monitor script triggers at most one channel per run (exits "
      "immediately after the first trigger and its own error check, inside the loop)",
      monitor_script_call is not None
      and monitor_script_call.count("softwarechannel_syncrepos \"$channel\"") == 1
      and "exit 0" in monitor_script_call.split("softwarechannel_syncrepos \"$channel\"")[1][:80])

# spacecmd_() captures stderr and fails loudly on a real error. A discarded stderr hides errors such as a stale session, and the
# monitor then reports an empty channel list.
check("channel-sync-monitor script no longer blindly discards spacecmd's own stderr — "
      "that's what let a real auth failure masquerade as 'no channels' for 6.5 hours live",
      monitor_script_call is not None and "2>/dev/null" not in monitor_script_call.split("spacecmd_()")[1][:200])

# ensure_bootstrap_repo_monitor is deployed with the channel-sync monitor. mgr-create-bootstrap-repo --auto never retries a
# distribution it has already attempted, so the monitor retries them.
import subprocess  # noqa: E402

bootstrap_script_call = next(
    (c for c in calls_chanlist if c.startswith("cat > /usr/local/sbin/smlm-bootstrap-repo-monitor.sh")), None)
check("setup_smlm_podman: deploys the bootstrap-repo-monitor script when channels are configured",
      bootstrap_script_call is not None)
check("setup_smlm_podman: deploys the systemd service unit for the bootstrap-repo monitor",
      any(c.startswith("cat > /etc/systemd/system/smlm-bootstrap-repo-monitor.service") for c in calls_chanlist))
check("setup_smlm_podman: deploys the systemd timer unit for the bootstrap-repo monitor, on a "
      "recurring (not one-shot) schedule",
      any(c.startswith("cat > /etc/systemd/system/smlm-bootstrap-repo-monitor.timer") and "OnUnitActiveSec="
          in c for c in calls_chanlist))
check("setup_smlm_podman: enables and starts the bootstrap-repo-monitor timer (not just installs it inert)",
      any("systemctl enable --now smlm-bootstrap-repo-monitor.timer" in c for c in calls_chanlist))

# The embedded monitor script is bash inside a Python string. Nothing else parses it, so this test runs bash -n on it. bash must be
# available in the test container for the check to mean anything.
_bash_check = subprocess.run(["bash", "-n", "-c", ism._BOOTSTRAP_REPO_MONITOR_SCRIPT],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
check("_BOOTSTRAP_REPO_MONITOR_SCRIPT: valid bash syntax (bash -n) — a real apostrophe-inside-"
      "${{var:-default}} bug broke this live 2026-09-23: {}".format(_bash_check.stderr.strip()),
      _bash_check.returncode == 0)

check("bootstrap-repo-monitor script explicitly builds/retries a distribution via --create "
      "(the only way to force a real retry once --auto would have given up on it)",
      bootstrap_script_call is not None and "mcbr --create \"$retry_label\"" in bootstrap_script_call)
check("bootstrap-repo-monitor script deliberately never invokes PLAIN --auto (mutating) — it "
      "targets labels directly via --list (discovery) + --create (build/retry) instead, since "
      "--auto's own 'changed products' tracking is exactly what causes it to forget a failed "
      "distribution. --auto --dryrun IS used, but only for discovering 'not connected to CDN' "
      "products --list hides forever (see the dedicated tests below) — never for a real build.",
      bootstrap_script_call is not None
      and re.search(r"mcbr --auto(?! --dryrun)", bootstrap_script_call) is None
      and "mcbr --list" in bootstrap_script_call)
check("bootstrap-repo-monitor script persists pending/failing distributions in a state dir "
      "that survives across timer runs (PENDING_DIR), not just in-memory for one run",
      bootstrap_script_call is not None and "PENDING_DIR=" in bootstrap_script_call)
check("bootstrap-repo-monitor script tracks successfully-built distributions separately "
      "(DONE_DIR) so a known-good one is never redundantly rebuilt on a later cycle",
      bootstrap_script_call is not None and "DONE_DIR=" in bootstrap_script_call)
check("bootstrap-repo-monitor script retries at most one pending distribution per run — same "
      "conservative, at-most-one-trigger caution as the channel-sync monitor",
      bootstrap_script_call is not None
      and bootstrap_script_call.count("mcbr --create \"$retry_label\"") == 1)

# --list omits products that are not connected to the CDN. --auto --dryrun still names them, so the script parses only that message
# pattern and does not treat --auto's build verdicts as authoritative.
check("bootstrap-repo-monitor script ALSO discovers 'not connected to CDN' distributions "
      "via --auto --dryrun, since --list hides them forever even once ready",
      bootstrap_script_call is not None and "mcbr --auto --dryrun" in bootstrap_script_call)
check("bootstrap-repo-monitor script's --auto --dryrun call is real dry-run (never mutates "
      "anything) — discovery only, the real build still goes through --create",
      bootstrap_script_call is not None
      and bootstrap_script_call.count("mcbr --auto --dryrun") == 1
      and "mcbr --auto\n" not in bootstrap_script_call)

import subprocess as _subprocess  # noqa: E402

_NOT_CONNECTED_SAMPLES = (
    "RHEL9-x86_64 not connected to CDN. Skipping",
    "WARNING: RHEL9-x86_64 not connected to CDN.",
)
for _sample in _NOT_CONNECTED_SAMPLES:
    _m = re.search(r"sed -nE '(s/.*not connected to CDN.*?)'", bootstrap_script_call) if bootstrap_script_call else None
    check("bootstrap-repo-monitor script: the 'not connected to CDN' sed pattern is present "
          "in the script (couldn't locate it to test against a real sample: {!r})".format(_sample),
          _m is not None)
    if _m:
        _r = _subprocess.run(["sed", "-nE", _m.group(1)], input=_sample,
                              stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, universal_newlines=True)
        check("bootstrap-repo-monitor script's 'not connected to CDN' pattern extracts the real "
              "label from: {!r}".format(_sample),
              _r.stdout.strip() == "RHEL9-x86_64")
check("channel-sync-monitor script captures spacecmd's stderr to a real file for inspection",
      monitor_script_call is not None and 'ERRFILE=$(mktemp)' in monitor_script_call
      and '2>"$ERRFILE"' in monitor_script_call)
check("channel-sync-monitor script cleans up its own temp error file on exit",
      monitor_script_call is not None and "trap 'rm -f \"$ERRFILE\"' EXIT" in monitor_script_call)
check("channel-sync-monitor script checks for a real spacecmd error after EVERY spacecmd_ call "
      "that matters (the channel list AND the resync trigger), not just one of them",
      monitor_script_call is not None
      and monitor_script_call.count("check_spacecmd_error") >= 3)  # def + 2 call sites
check("channel-sync-monitor script's error check logs loudly and exits non-zero on a real "
      "failure, rather than silently continuing as if nothing happened",
      monitor_script_call is not None
      and "log \"spacecmd call failed:" in monitor_script_call and "exit 1" in monitor_script_call)

cfg_keys_no_creds = {k: v for k, v in cfg_with_keys.items() if k not in ("smlm_scc_user", "smlm_scc_password")}
died = []
ism.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)
try:
    run_setup_smlm_podman(cfg_keys_no_creds, transactional=True)
except SystemExit:
    pass
check("setup_smlm_podman: activation_keys set but smlm_scc_user/password missing -> dies clearly "
      "instead of silently failing to sync any channels",
      any("smlm_scc_user" in m and "smlm_scc_password" in m for m in died))
ism.die = original_die


# ── main(): podman-mode dispatch bypasses Kubernetes setup entirely ────────
podman_definition = {
    "smlm": {
        "smlm_deployment": "podman",
        "smlm_scc_regcode": "REGCODE",
        "smlm_scc_product": "PRODUCT",
    },
    "nodes": {"sol.mydemo.lab": {"addons": ["smlm"]}},
}

# The VHM credential resolution runs through rps(), so a missing credential file skips only that step. This test uses the real
# function, because setup_smlm_podman is stubbed later for the main() tests.
cfg_vhm_bad_creds = dict(cfg)
cfg_vhm_bad_creds["smlm_vhm_aws_account"] = "does-not-exist"
cfg_vhm_bad_creds["smlm_virtual_host_managers"] = [
    {"label": "test-vhm", "region": "eu-central-1", "zone": "eu-central-1a"}]
cfg_vhm_bad_creds["smlm_config_channels"] = [{"label": "test-channel"}]
_, _, _, sc_calls_vhm, _ = run_setup_smlm_podman(cfg_vhm_bad_creds, transactional=True)
check("setup_smlm_podman: a missing VHM credential file does NOT prevent config channels (or any "
      "other later step) from still running",
      any(n == "ensure_config_channels" for n, a, k in sc_calls_vhm))
check("setup_smlm_podman: a missing VHM credential file does NOT prevent organizations (the LAST "
      "step in the sequence) from still running",
      any(n == "ensure_orgs" for n, a, k in sc_calls_vhm))

# smlm_snippets: wired in, and runs BEFORE distributions/kickstart profiles —
# a profile's own %pre/%post/partitioning can reference a snippet by name,
# so it needs to already exist first.
cfg_snippets = dict(cfg)
cfg_snippets["smlm_snippets"] = [{"name": "example-snippet", "content": "echo hi\n"}]
cfg_snippets["smlm_distributions"] = [{"name": "d1", "path": "/tmp/x", "base_channel": "c1",
                                        "install_type": "rhel_9"}]
_, _, _, sc_calls_snip, _ = run_setup_smlm_podman(cfg_snippets, transactional=True)
snippet_names = [n for n, a, k in sc_calls_snip]
check("setup_smlm_podman: calls ensure_snippets when smlm_snippets is set",
      "ensure_snippets" in snippet_names)
check("setup_smlm_podman: ensure_snippets runs BEFORE ensure_distributions",
      "ensure_distributions" in snippet_names
      and snippet_names.index("ensure_snippets") < snippet_names.index("ensure_distributions"))


# smlm_image_build_hosts: wired in, and runs BEFORE image imports would be
# scheduled — a build_host_id used by --import-images needs the entitlement
# already enabled.
cfg_build_hosts = dict(cfg)
cfg_build_hosts["smlm_image_build_hosts"] = [{"system": "mercury.mydemo.lab"}]
cfg_build_hosts["smlm_image_profiles"] = [{"label": "p1", "type": "dockerfile", "store": "s1",
                                            "path": "https://example.com/x.git#main:x",
                                            "activation_key": "1-x"}]
_, _, _, sc_calls_bh, _ = run_setup_smlm_podman(cfg_build_hosts, transactional=True)
bh_names = [n for n, a, k in sc_calls_bh]
check("setup_smlm_podman: calls ensure_container_build_hosts when smlm_image_build_hosts is set",
      "ensure_container_build_hosts" in bh_names)
check("setup_smlm_podman: ensure_container_build_hosts runs AFTER ensure_image_profiles",
      "ensure_image_profiles" in bh_names
      and bh_names.index("ensure_image_profiles") < bh_names.index("ensure_container_build_hosts"))


# smlm_mcp_server: wired in.
cfg_mcp = dict(cfg)
cfg_mcp["smlm_mcp_server"] = {"port": 8090}
_, _, _, sc_calls_mcp, _ = run_setup_smlm_podman(cfg_mcp, transactional=True)
mcp_names = [n for n, a, k in sc_calls_mcp]
check("setup_smlm_podman: calls ensure_mcp_server when smlm_mcp_server is set",
      "ensure_mcp_server" in mcp_names)


ism.ac.handle_common_args = lambda *a, **k: None
ism.primary.load_definition = lambda path: podman_definition
ism.primary.load_config = lambda: {"VIRT_SRV": "hypervisor1"}

podman_calls = []
ism.setup_smlm_podman = lambda vm_name, virt_srv, cfg: podman_calls.append((vm_name, virt_srv))

k8s_setup_touched = []
ism.setup_helm = lambda *a, **k: k8s_setup_touched.append("setup_helm")
ism.setup_smlm_traefik = lambda *a, **k: k8s_setup_touched.append("setup_smlm_traefik")
ism.setup_smlm_prereqs = lambda *a, **k: k8s_setup_touched.append("setup_smlm_prereqs")
ism.setup_smlm = lambda *a, **k: k8s_setup_touched.append("setup_smlm")
ism.k8s.first_server_node = lambda definition: k8s_setup_touched.append("first_server_node") or None

old_argv = sys.argv
sys.argv = ["install_smlm.py", "lab.json"]
try:
    ism.main()
finally:
    sys.argv = old_argv

check("main(): podman mode dispatches to setup_smlm_podman for the addon's node",
      podman_calls == [("sol.mydemo.lab", "hypervisor1")])
check("main(): podman mode never touches any Kubernetes/Helm-chart setup function",
      k8s_setup_touched == [])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all smlm baremetal checks passed")
