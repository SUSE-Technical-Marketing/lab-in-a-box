#!/usr/bin/env python3
# Unit tests for install_smlm.py's traditional mgradm/podman deployment mode
# (smlm_deployment: "podman", added 2026-09-11) — a genuine bare-metal/VM
# install path distinct from the addon's original Kubernetes/Helm-chart
# deployment, per documentation.suse.com/multi-linux-manager/5.2's own
# installation-and-upgrade guide. install_uyuni.py itself is NOT modified
# beyond importing its two shared helpers from libs/mgradm_common.py instead
# of defining them (per explicit user instruction: it stays scoped to the
# open-source Uyuni project only) — setup_smlm_podman() imports and reuses
# mgradm_common's run_install_with_pg_hba_guard/ensure_server_container_active,
# which are mocked here rather than exercised for real. Run from
# 49_smlm_baremetal.sh, in its own container — see tests/run_tests.sh.
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
def run_setup_smlm_podman(cfg, transactional):
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
                 "ensure_system_tags", "ensure_environments", "ensure_orgs"):
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

# Real bug found live 2026-09-13: an unquoted multi-word --organization
# ("SUSE Test") got split by the remote shell into two tokens — mgradm then
# misinterpreted the stray second word as its own optional FQDN positional
# argument ("Test is not a valid FQDN"), failing the entire install.
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

# Real bug found live 2026-09-14: every real lab definition's smlm_channels
# is a JSON array (matching solar-system-lab.json), but this code's own "or
# ''" default and a plain .format(channels) both assumed a pre-joined
# string — .format() on a Python list just stringifies its own repr
# ("['a', 'b']"), producing ONE malformed shell argument. Never caught by
# the older cfg fixture above (line ~67), which happened to use a plain
# string for smlm_channels and so never exercised the list case at all.
cfg_with_channel_list = dict(cfg, smlm_channels=["chan-a", "chan-b"])
calls_chanlist, _, _, _, _ = run_setup_smlm_podman(cfg_with_channel_list, transactional=True)
check("setup_smlm_podman: a real (list-shaped) smlm_channels is space-joined into the mgr-sync "
      "add channels command, not stringified as a Python list repr",
      any(c == "mgrctl exec -- mgr-sync add channels chan-a chan-b" for c in calls_chanlist))
check("setup_smlm_podman: the malformed Python-list-repr form never appears",
      not any("['chan-a', 'chan-b']" in c for c in calls_chanlist))

# ensure_channel_sync_monitor: deployed whenever channels are configured —
# confirmed live 2026-09-14 that a mid-flight server restart can orphan a
# reposync with no completion marker and no error, silently, so this must
# run on its own periodic schedule to ever notice and recover.
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
