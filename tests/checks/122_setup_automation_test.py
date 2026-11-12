#!/usr/bin/env python3.11
# Unit tests for setup_demo_server/setup_automation.py — the automation-system-first entrypoint.
# No real network/subprocess calls. Run from 122_setup_automation.sh, in its own container —
# see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "setup_demo_server"))
sys.path.insert(0, str(_REPO / "scripts"))

import setup_automation as sa  # noqa: E402
import setup_kvm_node as skn  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── parse_args(): the one new flag, and that --help never mentions the other script ──────────
check("--kvm-host/--no-kvm-host default to None (decide later from _automation_node)",
      sa.parse_args([]).kvm_host is None)
check("--kvm-host sets it True", sa.parse_args(["--kvm-host"]).kvm_host is True)
check("--no-kvm-host sets it False", sa.parse_args(["--no-kvm-host"]).kvm_host is False)
check("the rest of setup_kvm_node.py's options still work unchanged",
      sa.parse_args(["--automation-node", "container"]).automation_node == "container")


def _tmp_cfgs(automation_text="", kvm_text=""):
    tmp = Path(tempfile.mkdtemp())
    if automation_text:
        (tmp / "automation.cfg").write_text(automation_text)
    if kvm_text:
        (tmp / "kvm_host.cfg").write_text(kvm_text)
    return tmp


def _run_main(argv, tmp, automation_values=None, kvm_values=None, ask_answers=None,
             input_answers=None, automation_cfg=None, kvm_cfg=None):
    """Runs setup_automation.main() with every external effect mocked, returns the mocks used."""
    prep_calls, deploy_calls, cred_calls, died = [], [], [], []
    ask_iter = iter(ask_answers or [])

    def fake_ask(question, default, choices=None):
        try:
            return next(ask_iter)
        except StopIteration:
            return default

    real_find = skn._find

    def fake_find(name):
        return tmp / name if name in ("automation.cfg", "kvm_host.cfg", "lab.cfg") else real_find(name)

    with mock.patch.object(skn, "_find", side_effect=fake_find), \
         mock.patch.object(sa.kvm_host_profiles, "detect_profile", return_value=None), \
         mock.patch.object(skn, "Asker", return_value=fake_ask), \
         mock.patch.object(skn, "build_values",
                           return_value=(automation_values or {}, kvm_values or {}, "")), \
         mock.patch.object(skn, "run_remote"), \
         mock.patch.object(skn, "prepare_kvm_host", side_effect=lambda cfg, d: prep_calls.append(cfg)), \
         mock.patch.object(skn, "deploy_automation_node", side_effect=lambda cfg, d: deploy_calls.append(cfg)), \
         mock.patch.object(skn, "die", side_effect=lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)), \
         mock.patch.object(sa.primary, "load_shell_vars",
                           side_effect=lambda p: (automation_cfg or {}) if p.name == "automation.cfg"
                           else (kvm_cfg or {})), \
         mock.patch.object(sa.primary, "credentials_dirs", return_value=["/etc/lab_creation/credentials"]), \
         mock.patch.object(sa.setup_credentials, "interactive_build", side_effect=lambda d: cred_calls.append(d)), \
         mock.patch("builtins.input", side_effect=input_answers or []):
        try:
            sa.main(argv)
        except SystemExit:
            pass
    return {"prep": prep_calls, "deploy": deploy_calls, "cred": cred_calls, "died": died}


# ── vm mode: KVM-host setup is mandatory, never a question ───────────────────────────────────
tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "vm"], tmp,
             automation_cfg={"_automation_node": "vm", "_myip": "1.2.3.4"})
check("vm mode: prepare_kvm_host() runs without ever asking", len(r["prep"]) == 1 and not r["died"])
check("vm mode: deploy_automation_node() still runs afterwards", len(r["deploy"]) == 1)

# ── container mode: KVM-host setup is a real optional question, default no ──────────────────
tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "container"], tmp,
             automation_cfg={"_automation_node": "container"})
check("container mode, non-interactive, no --kvm-host flag: KVM-host setup is skipped (default no)",
      r["prep"] == [] and len(r["deploy"]) == 1)

tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "container", "--kvm-host"], tmp,
             automation_cfg={"_automation_node": "container"})
check("container mode with --kvm-host: KVM-host setup runs too", len(r["prep"]) == 1)

# ── --no-kvm-host with vm mode: dies with a clear message instead of silently failing later ──
tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "vm", "--no-kvm-host"], tmp,
             automation_cfg={"_automation_node": "vm"})
check("--no-kvm-host + vm mode dies with a clear message naming both options, and never "
      "proceeds to deploy_automation_node() afterwards",
      r["prep"] == [] and r["died"] and "--kvm-host" in r["died"][0] and "--automation-node" in r["died"][0]
      and r["deploy"] == [])

# ── cfg merging: automation.cfg's own keys always present; kvm_host.cfg's only when it ran ───
tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "vm"], tmp,
             automation_cfg={"_automation_node": "vm", "_myip": "9.9.9.9"},
             kvm_cfg={"_bridge_nic": "eth0"})
check("vm mode: the merged cfg passed to deploy_automation_node has both automation and kvm_host keys",
      r["deploy"][0] == {"_bridge_nic": "eth0", "_automation_node": "vm", "_myip": "9.9.9.9"})

tmp = _tmp_cfgs()
r = _run_main(["-y", "--automation-node", "container"], tmp,
             automation_cfg={"_automation_node": "container", "_myip": "9.9.9.9"})
check("container mode with no KVM-host step: the merged cfg has only automation.cfg's keys",
      r["deploy"][0] == {"_automation_node": "container", "_myip": "9.9.9.9"})

# ── interactive: offers to add cloud credentials afterwards ──────────────────────────────────
tmp = _tmp_cfgs()
r = _run_main(["--interactive"], tmp, automation_cfg={"_automation_node": "container"},
             ask_answers=["no", "yes"], input_answers=["yes"])
check("interactive mode offers to add cloud credentials, and runs it when accepted",
      r["cred"] == ["/etc/lab_creation/credentials"])

tmp = _tmp_cfgs()
r = _run_main(["--interactive"], tmp, automation_cfg={"_automation_node": "container"},
             ask_answers=["no", "no"], input_answers=["yes"])
check("interactive mode does not add cloud credentials when declined", r["cred"] == [])

# ── interactive: reloads the config after confirming, so an edit during the pause is honoured ─
# (real file I/O here, not the mocked load_shell_vars _run_main uses above — that's the whole point)
tmp_reload = _tmp_cfgs()
automation_tpl = (_REPO / "setup_demo_server" / "automation.cfg.template").read_text()
(tmp_reload / "automation.cfg").write_text(automation_tpl)
reload_input_calls = []


def _reload_fake_input(prompt):
    reload_input_calls.append(prompt)
    if prompt.strip().startswith("Install with these settings"):
        (tmp_reload / "automation.cfg").write_text(
            skn.render_lab_cfg((tmp_reload / "automation.cfg").read_text(),
                               {"_mydomain": "edited-during-pause.lab"}))
    return "yes" if prompt.strip().startswith("Install with these settings") else "no"


deploy_calls_reload = []
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp_reload / name), \
     mock.patch.object(sa.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "run_remote"), \
     mock.patch.object(skn, "prepare_kvm_host"), \
     mock.patch.object(skn, "deploy_automation_node", side_effect=lambda cfg, d: deploy_calls_reload.append(cfg)), \
     mock.patch.object(sa.primary, "credentials_dirs", return_value=["/etc/lab_creation/credentials"]), \
     mock.patch("builtins.input", side_effect=_reload_fake_input):
    sa.main(["--interactive", "--automation-node", "container"])
check("setup_automation.py's main() reloads automation.cfg after confirming, so an edit made "
      "during the interactive pause is actually applied",
      deploy_calls_reload and deploy_calls_reload[0].get("_mydomain") == "edited-during-pause.lab")


# ── auto-split: a legacy lab.cfg is migrated on startup, not just a manual migrate_config.py chore ─
tmp_legacy = _tmp_cfgs()
(tmp_legacy / "lab.cfg").write_text(
    "_myip=192.168.8.99\n_mydomain=legacy.example\n_automation_node=\"vm\"\n"
    "_bridge_nic=\"\"\n_network_mode=\"bridge\"\n_QCOW_IMAGE=/x/leap.qcow2\n"
    "SUSE_regcode=\"OLD-REGCODE\"\nSUSE_email=\"\"\nSUSE_url=\"\"\n")
deploy_calls_legacy, prep_calls_legacy = [], []
with mock.patch.object(skn, "_find", side_effect=lambda name: tmp_legacy / name), \
     mock.patch.object(sa.kvm_host_profiles, "detect_profile", return_value=None), \
     mock.patch.object(skn, "build_values") as bv_legacy, \
     mock.patch.object(skn, "run_remote"), \
     mock.patch.object(skn, "prepare_kvm_host", side_effect=lambda cfg, d: prep_calls_legacy.append(cfg)), \
     mock.patch.object(skn, "deploy_automation_node", side_effect=lambda cfg, d: deploy_calls_legacy.append(cfg)), \
     mock.patch.object(sa.primary, "credentials_dirs", return_value=[str(tmp_legacy / "creds")]):
    sa.main(["-y"])
check("setup_automation.py also auto-splits a legacy lab.cfg instead of synthesizing fresh "
      "defaults (build_values is never called)",
      bv_legacy.call_count == 0 and (tmp_legacy / "automation.cfg").is_file()
      and (tmp_legacy / "kvm_host.cfg").is_file())
check("vm mode from the migrated automation.cfg still runs prepare_kvm_host() and deploys with "
      "the migrated values",
      len(prep_calls_legacy) == 1 and deploy_calls_legacy
      and deploy_calls_legacy[0]["_myip"] == "192.168.8.99"
      and deploy_calls_legacy[0]["_network_mode"] == "bridge")
check("lab.cfg ends the run as the regenerated derived file (from the migrated cfg), not the "
      "stale pre-split content — the split only promised not to touch it mid-split, not forever",
      "edit those, not this file" in (tmp_legacy / "lab.cfg").read_text()
      and "SUSE_regcode" not in (tmp_legacy / "lab.cfg").read_text())


# ── target: delegates straight to run_remote(), nothing else runs ────────────────────────────
remote_calls = []
with mock.patch.object(skn, "run_remote", side_effect=lambda t, a: remote_calls.append((t, a))), \
     mock.patch.object(skn, "build_values") as bv:
    sa.main(["nuc9.lab", "--automation-node", "container"])
check("a TARGET argument delegates to run_remote() and does nothing else locally",
      remote_calls == [("nuc9.lab", ["nuc9.lab", "--automation-node", "container"])] and not bv.called)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_automation checks passed")
