#!/usr/bin/env python3
# Mocked unit tests for libs/spacecmd_common.py's orchestrators — each <prefix>_<feature> list in an add-on's section is
# validated and handed, entry by entry, to its ensure_* helper — and for the custom-channel package push (rhnpush),
# package id lookup and image build scheduling. Run from 09_spacecmd_common.sh, in its own container.
import subprocess
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import spacecmd_common as sc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def done(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def dies(fn, *args):
    """True when fn(*args) calls sc.die()."""
    with mock.patch.object(sc, "die", side_effect=SystemExit("die")):
        try:
            fn(*args)
        except SystemExit:
            return True
    return False


H, X = "smlm1", "mgrctl exec --"

# ── <prefix>_recurring_actions ─────────────────────────────────────────────────────────────────────────────────────
cfg = {"smlm_recurring_actions": [
    {"name": "nightly", "entity_type": "minion", "entity": "web1", "cron_expr": "0 0 2 * * ?"},
    {"name": "grp", "entity_type": "group", "entity": "webs", "cron_expr": "0 0 3 * * ?", "schedule_type": "custom",
     "states": ["s1"]},
    {"name": "org", "entity_type": "org", "entity": "1", "cron_expr": "0 0 4 * * ?"}]}
with mock.patch.object(sc, "_system_id", return_value=1001), mock.patch.object(sc, "group_id_for", return_value=7), \
        mock.patch.object(sc, "ensure_recurring_schedule") as ers:
    sc.ensure_recurring_schedules(H, X, cfg, "smlm")
check("ensure_recurring_schedules: resolves minion, group and org ids and passes each schedule on",
      [(c[0][2], c[0][3], c[0][4], c[0][5]) for c in ers.call_args_list] ==
      [("minion", 1001, "0 0 2 * * ?", "nightly"), ("group", 7, "0 0 3 * * ?", "grp"), ("org", 1, "0 0 4 * * ?", "org")]
      and ers.call_args_list[1][1] == {"schedule_type": "custom", "states": ["s1"]})
check("ensure_recurring_schedules: dies on an entry without cron_expr",
      dies(sc.ensure_recurring_schedules, H, X, {"smlm_recurring_actions": [{"name": "n", "entity_type": "org", "entity": "1"}]},
           "smlm"))
check("ensure_recurring_schedules: dies on an unknown entity_type",
      dies(sc.ensure_recurring_schedules, H, X, {"smlm_recurring_actions": [
          {"name": "n", "entity_type": "planet", "entity": "1", "cron_expr": "x"}]}, "smlm"))
with mock.patch.object(sc, "group_id_for", return_value=None):
    check("ensure_recurring_schedules: dies on a group that does not exist",
          dies(sc.ensure_recurring_schedules, H, X, {"smlm_recurring_actions": [
              {"name": "n", "entity_type": "group", "entity": "nope", "cron_expr": "x"}]}, "smlm"))

# ── maintenance schedules, action chains, system profiles, custom values, org transfers ────────────────────────────
with mock.patch.object(sc, "ensure_maintenance_schedule") as ems, \
        mock.patch.object(sc, "ensure_maintenance_schedule_systems") as emss:
    sc.ensure_maintenance_schedules(H, X, {"uyuni_maintenance_schedules": [
        {"name": "w", "type": "single", "calendar": "cal1", "systems": ["a", "b"]}, {"name": "m", "type": "multi"}]}, "uyuni")
check("ensure_maintenance_schedules: creates each schedule and assigns its systems",
      [c[0][2:4] for c in ems.call_args_list] == [("w", "single"), ("m", "multi")]
      and ems.call_args_list[0][1] == {"calendar": "cal1"} and emss.call_args[0][2:] == ("w", ["a", "b"])
      and emss.call_count == 1)
check("ensure_maintenance_schedules: dies on an entry without type",
      dies(sc.ensure_maintenance_schedules, H, X, {"uyuni_maintenance_schedules": [{"name": "w"}]}, "uyuni"))
with mock.patch.object(sc, "ensure_action_chain") as eac:
    sc.ensure_action_chains(H, X, {"smlm_action_chains": [{"label": "c1", "actions": [{"type": "reboot"}]}]}, "smlm")
check("ensure_action_chains: each chain with its actions", eac.call_args[0][2:] == ("c1", [{"type": "reboot"}]))
check("ensure_action_chains: dies on a chain without actions",
      dies(sc.ensure_action_chains, H, X, {"smlm_action_chains": [{"label": "c1"}]}, "smlm"))
with mock.patch.object(sc, "ensure_system_profile") as esp:
    sc.ensure_system_profiles(H, X, {"smlm_system_profiles": [{"system": "web1", "label": "p1"}]}, "smlm")
check("ensure_system_profiles: the label is the description when none is given", esp.call_args[0][2:] == ("web1", "p1", "p1"))
check("ensure_system_profiles: dies without a label",
      dies(sc.ensure_system_profiles, H, X, {"smlm_system_profiles": [{"system": "web1"}]}, "smlm"))
with mock.patch.object(sc, "ensure_system_custom_value") as escv:
    sc.ensure_system_custom_values(H, X, {"smlm_system_custom_values": [{"system": "web1", "key": "rack", "value": 4}]}, "smlm")
check("ensure_system_custom_values: the value as text", escv.call_args[0][2:] == ("web1", "rack", "4"))
check("ensure_system_custom_values: dies without a value",
      dies(sc.ensure_system_custom_values, H, X, {"smlm_system_custom_values": [{"system": "w", "key": "k"}]}, "smlm"))
with mock.patch.object(sc, "ensure_org_system_transfer") as eost:
    sc.ensure_org_system_transfers(H, X, {"smlm_org_system_transfers": [{"org": "o2", "systems": ["web1"]}]}, "smlm")
check("ensure_org_system_transfers: each org with its systems", eost.call_args[0][2:] == ("o2", ["web1"]))
check("ensure_org_system_transfers: dies on an empty systems list",
      dies(sc.ensure_org_system_transfers, H, X, {"smlm_org_system_transfers": [{"org": "o2", "systems": []}]}, "smlm"))
with mock.patch.object(sc, "ensure_maintenance_schedule") as ems, mock.patch.object(sc, "ensure_action_chain") as eac:
    for fn in (sc.ensure_maintenance_schedules, sc.ensure_action_chains, sc.ensure_recurring_schedules,
               sc.ensure_patches, sc.build_images, sc.ensure_system_profiles, sc.ensure_custom_channels):
        fn(H, X, {}, "smlm")
check("the orchestrators do nothing when their list is unset", not ems.called and not eac.called)

# ── patches: each channel allow-listed once, then each patch created ──────────────────────────────────────────────
with mock.patch.object(sc, "ensure_patch_api_allowlisted") as allow, mock.patch.object(sc, "ensure_errata") as errata:
    sc.ensure_patches(H, X, {"smlm_patches": [{"advisory_name": "A-1", "channel": "c1"}, {"advisory_name": "A-2", "channel": "c1"},
                                              {"advisory_name": "A-3", "channel": "c2"}]}, "smlm")
check("ensure_patches: allow-lists each channel once, creates every patch",
      [c[0][2] for c in allow.call_args_list] == ["c1", "c2"] and [c[0][3] for c in errata.call_args_list] == ["A-1", "A-2", "A-3"])
check("ensure_patches: dies on a patch without a channel",
      dies(sc.ensure_patches, H, X, {"smlm_patches": [{"advisory_name": "A-1"}]}, "smlm"))

# ── image builds ──────────────────────────────────────────────────────────────────────────────────────────────────
with mock.patch.object(sc, "_system_id", return_value=1002), mock.patch.object(sc, "ensure_image_build") as eib:
    sc.build_images(H, X, {"smlm_image_builds": [{"profile": "p1", "build_host": "bh1"}]}, "smlm")
check("build_images: resolves the build host and schedules each profile",
      eib.call_args[0][2:] == ("p1", 1002) and eib.call_args[1] == {"version": "latest"})
with mock.patch.object(sc, "_api_call", return_value=done()) as api:
    sc.ensure_image_build(H, X, "p1", 1002, version="2.0")
check("ensure_image_build: image.scheduleImageBuild with profile, version, build host and a start time",
      api.call_args[0][2] == "image.scheduleImageBuild" and api.call_args[0][3][:3] == ["p1", "2.0", 1002])
with mock.patch.object(sc, "_api_call", return_value=done(rc=1)):
    check("ensure_image_build: dies when the API refuses", dies(sc.ensure_image_build, H, X, "p1", 1002))

# ── package_id_for ────────────────────────────────────────────────────────────────────────────────────────────────
listing = "lab-tools-1.0-1.x86_64 (4242)\nother-2.0-1.noarch (17)\n"
with mock.patch.object(sc, "_spacecmd", return_value=done(listing)):
    check("package_id_for: the id of the named package", sc.package_id_for(H, X, "c1", "lab-tools") == 4242)
    check("package_id_for: None when the package is not there", sc.package_id_for(H, X, "c1", "absent") is None)
with mock.patch.object(sc, "_spacecmd", return_value=done(rc=1)):
    check("package_id_for: None when spacecmd fails", sc.package_id_for(H, X, "c1", "lab-tools") is None)

# ── rhnpush and the package push ──────────────────────────────────────────────────────────────────────────────────
with mock.patch.object(sc, "_run", side_effect=[done(rc=1), done()]) as run:
    check("ensure_rhnpush_available: installs rhnpush when missing", sc.ensure_rhnpush_available(H, X) is True
          and run.call_args[0][2] == "zypper --non-interactive install rhnpush")
with mock.patch.object(sc, "_run", side_effect=[done(rc=1), done(rc=1)]), mock.patch.object(sc, "warn") as warned:
    check("ensure_rhnpush_available: warns and returns False when it cannot be installed",
          sc.ensure_rhnpush_available(H, X) is False and warned.called)
rpm = "/build/lab-tools-1.0-1.x86_64.rpm"
with mock.patch.object(sc, "channel_package_exists", return_value=False) as exists, \
        mock.patch.object(sc, "ensure_rhnpush_available", return_value=True), \
        mock.patch.object(sc, "scp_to", return_value=done()) as scp, \
        mock.patch.object(sc, "ssh_run", return_value=done()) as ssh, \
        mock.patch.object(sc, "_run", return_value=done()) as run:
    sc.ensure_channel_package(H, X, "c1", rpm, "admin", "p w")
check("ensure_channel_package: checks the package name, copies it to the host and into the container, pushes it",
      exists.call_args[0][3] == "lab-tools" and scp.call_args[0] == (H, rpm, "/tmp/lab-tools-1.0-1.x86_64.rpm")
      and ssh.call_args[0][1] == "podman cp /tmp/lab-tools-1.0-1.x86_64.rpm uyuni-server:/tmp/lab-tools-1.0-1.x86_64.rpm"
      and "rhnpush --server=localhost --channel=c1 --username=admin --password='p w' --nosig" in run.call_args[0][2])
with mock.patch.object(sc, "channel_package_exists", return_value=True), mock.patch.object(sc, "scp_to") as scp:
    sc.ensure_channel_package(H, X, "c1", rpm, "admin", "pw")
check("ensure_channel_package: leaves a package already in the channel alone", not scp.called)

# ── custom channels: create each, push packages only on a podman deployment ───────────────────────────────────────
entries = [{"label": "c1", "packages": [rpm]}]
with mock.patch.object(sc, "ensure_custom_channel") as ecc, mock.patch.object(sc, "ensure_channel_package") as ecp:
    sc.ensure_custom_channels(H, X, {"smlm_custom_channels": entries, "smlm_deployment": "podman"}, "smlm")
    check("ensure_custom_channels: creates the channel with defaults, pushes its packages as the admin",
          ecc.call_args[0][2:] == ("c1", "c1", "c1", "channel-x86_64", "", "sha256")
          and ecp.call_args[0][2:] == ("c1", rpm, "admin", "Smlm12345"))
    ecp.reset_mock()
    with mock.patch.object(sc, "warn") as warned:
        sc.ensure_custom_channels(H, X, {"smlm_custom_channels": entries}, "smlm")
    check("ensure_custom_channels: on Kubernetes, creates the channel but skips the push with a warning",
          not ecp.called and warned.called)
check("ensure_custom_channels: dies on an entry without a label",
      dies(sc.ensure_custom_channels, H, X, {"smlm_custom_channels": [{"name": "x"}]}, "smlm"))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all spacecmd orchestration checks passed")
