#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Uyuni server on a dedicated host VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Uyuni is the open-source systems management server that SUSE Multi-Linux Manager is built on. This script installs it on a
# dedicated host VM with mgradm, as a podman container deployment. The target VM must run openSUSE Leap 15.6 or SLE Micro with
# podman available.
#
# Schema version: 1.1
#
# JSON section: "uyuni"
#   uyuni_admin         : admin username (default: admin)
#   uyuni_password      : admin password (default: Uyuni12345)
#   uyuni_email         : admin e-mail (default: admin@lab.local)
#   uyuni_org           : default organization (default: lab)
#   uyuni_ssl_password  : SSL certificate password; when unset, uyuni_password
#   uyuni_channels      : space-separated channels to sync after install
#   uyuni_extra_dsk     : extra disk to mount for storage, e.g. /dev/vdb,/srv/mirror
#
# Activation key (created after install; skipped when uyuni_activation_key is unset)
#   uyuni_activation_key                          : key name
#   uyuni_activation_key_desc                     : description; when unset, the key name
#   uyuni_activation_key_base_channel             : base channel label, required when uyuni_activation_key is set
#   uyuni_activation_key_child_channels           : space-separated child channel labels
#   uyuni_activation_key_universal_default        : "true" marks the key as the organization's default (default: false)
#   uyuni_activation_key_entitlements             : comma-separated, e.g. "enterprise_entitled,virtualization_host"
#   uyuni_activation_key_contact_method           : contact method
#   uyuni_activation_key_config_channels          : space-separated config channel labels
#   uyuni_activation_key_enable_config_deployment : "true" enables config-file deployment (default: false)
#   uyuni_activation_key_groups                   : space-separated system group names
#   uyuni_activation_key_appstreams               : space-separated "module:stream" pairs, e.g. "nodejs:20 postgresql:16".
#                                                   Applied on every run. An already-enabled module is detected and skipped.
#   uyuni_activation_key_packages                 : space-separated package names, added on every run. Names only.
#   uyuni_sync_channels                           : channels to ensure are synced (with mgr-sync add channel, when not already
#                                                   present) before the key is created. Independent of uyuni_channels.
#
# Config channels (created before the activation key, so uyuni_activation_key_config_channels can refer to them)
#   uyuni_config_channels : [{"label", "name", "description", "type": "normal" (default) | "state", "init_sls" (state channels only),
#                           "files": [{"path", "content", "owner", "group", "mode", "binary"}]}]
#                           Idempotent per channel and per file. The channel is not attached to a registered system here. Use
#                           uyuni_activation_key_config_channels for newly registered clients.
#
# Organizations (created after the above; each has its own admin session for scoped provisioning)
#   uyuni_orgs : [{"name", "admin_user", "admin_pass", "admin_email", "admin_first_name", "admin_last_name", "prefix", "pam" (false),
#               "trust_with": [...], "share_channels": [...], "share_channels_access": "protected" | "public" | "private",
#               the organization's own uyuni_activation_key*, uyuni_config_channels and uyuni_access_groups, with the same field names}]
#               admin_user, admin_pass and admin_email are required to create the organization. trust_with establishes channel-sharing
#               trust. share_channels marks channels this organization owns as shared, so a trusted organization's activation keys can use them.
#
# User accounts (top level, scoped to the default organization, or inside a uyuni_orgs entry)
#   uyuni_users : [{"username", "password", "first_name", "last_name", "email", "pam" (false), "roles": [...]}]
#               The first four are required for creation. "roles" are applied on every run with user_addrole, and only with the fixed
#               labels: activation_key_admin, channel_admin, config_admin, image_admin, org_admin, regular_user, satellite_admin,
#               system_group_admin. A custom access group label does not belong here. List the user in that group's "users" instead.
#
# RBAC: custom user access groups (API only)
#   uyuni_access_groups : [{"label", "description", "permissions_from": [role labels], "permissions": [{"namespace", "mode": "R" | "W"}],
#                        "users": [usernames]}]
#               Each user must already exist, from uyuni_users or an organization's admin account.
#
# Ansible integration (orchestration only; playbook and inventory files stay on the control node)
#   uyuni_ansible_control_nodes : [{"system": "hostname"}]. Enables the "Ansible Control Node" entitlement and applies highstate to
#                         install the ansible package. The system must be registered first.
#   uyuni_ansible_paths         : [{"system" (hostname) or "control_node_id" (numeric), "type": "playbook" | "inventory", "path"}].
#                         A playbook path is a directory. An inventory path is the inventory file or script.
#   uyuni_ansible_playbooks     : [{"control_node_id", "playbook_path", "inventory_path", "earliest" (optional, default now),
#                         "action_chain_label" (optional), "test_mode": false, "extra_vars": "...", "flush_cache": false}]
#                         Run with install_uyuni.py <lab.json> --run-ansible-playbooks. It never runs automatically.
#
# Content Lifecycle Management (CLM)
#   Projects, sources, filters and environments are defined automatically and idempotently, before activation keys, so the keys
#   can refer to environments. Builds and promotions run only with --run-clm-actions.
#   uyuni_content_projects : [{"label", "name", "description", "sources": [software channel labels],
#                        "filters": [{"name", "rule": "allow" | "deny", "entity_type": "package" | "erratum" | "module" | "ptf",
#                        "matcher", "field", "value"}], "environments": ["dev", "test", "prod"] or [{"label", "name", "description"}]}]
#                        Only software channels can be sources. Filters have no lookup by name, so the check is made at project level.
#   uyuni_content_lifecycle_actions : [{"project", "action": "build", "message", "wait": true, "wait_env", "wait_timeout"},
#                        {"project", "action": "promote", "from_env", "wait": true, "wait_env"}]
#                        For promote, from_env is the stage being promoted from. The server picks the next stage. "wait" polls the
#                        environment until it is built or failed. Run with install_uyuni.py <lab.json> --run-clm-actions.
#
# SCAP compliance auditing
#   uyuni_scap_scans    : [{"system", "xccdf_path", "profile"}]. The XCCDF path and the scanner must already exist on the target.
#                         Idempotent by system and xccdf_path. Run with install_uyuni.py <lab.json> --run-scap-scans.
#   uyuni_scap_policies : [{"policy_name", "scap_content_id" (a content id uploaded through the Web UI), "xccdf_profile_id"
#                         (read it from the uploaded document), "description", "earliest" (ISO local date-time), "tailoring_file",
#                         "tailoring_profile_id", "oval_files", "advanced_args", "fetch_remote_resources": false}]
#                         Automatic and idempotent by name. It logs in as uyuni_admin through the Web UI route. That route is internal
#                         and may change between releases. SCAP content and tailoring files are not uploaded by this script. Upload
#                         them through the Web UI, then use their ids here.
#
# CVE audit (read only, no JSON)
#   install_uyuni.py <lab.json> --cve-audit CVE-YYYY-NNNNN         systems' patch status for the CVE
#   install_uyuni.py <lab.json> --cve-audit-images CVE-YYYY-NNNNN  the same for container and OS images
#
# Environment topology (a composition of the primitives above; automatic except recurring_schedule)
#   uyuni_activation_keys  : [{...}], one dict per key, with the same fields as uyuni_activation_key*. One organization can define
#                            several named keys this way.
#   uyuni_system_groups    : [{"name", "description", "systems": [...]}]
#   uyuni_custom_info_keys : [{"name", "description"}]. Must be defined before uyuni_system_tags or an environment's custom_info_tags
#                            can set a value for that key.
#   uyuni_system_tags      : [{"system", "tags": {"key": "value"}}]. Uyuni has no tag object. These are custom-info values.
#   uyuni_environments     : [{"label", "system_group" (name of a uyuni_system_groups entry), "activation_key" (name of a key),
#                            "custom_info_tags": {...}, "recurring_schedule": {"type": "highstate" (default) | "custom", "cron",
#                            "states" (for custom), "group_id" (skips the name lookup), "extra": {...}}}]
#                            system_group and activation_key are references only. Run the schedules with
#                            install_uyuni.py <lab.json> --run-recurring-schedules.
#
# The target node must have "uyuni" in its addons[] list in the JSON definition:
#   "nodes": { "uyuni.lab": { "myip": "...", "addons": ["uyuni"] } }

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "uyuni",
    "targets": ["vm", "baremetal"],
    # This addon installs with mgradm and podman on the host, so its layer is standalone-container. It never uses kubectl or helm.
    "layers": ["standalone-container"],
    "requires_kubernetes": None,
    "aux_services": [],
}

import os
import shlex
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
import spacecmd_common as sc  # noqa: E402
from lab_creation import ssh_run, reboot_vm, check_ssh_conn, die  # noqa: E402
# The shared mgradm helpers are in libs/mgradm_common.py, and they are imported here under their original names.
from mgradm_common import (  # noqa: E402
    run_install_with_pg_hba_guard as _run_install_with_pg_hba_guard,
    ensure_server_container_active as _ensure_server_container_active,
)


def setup_uyuni(hostname, virt_srv, cfg):
    """
    Install Uyuni on a host VM with mgradm. This matches the bash setup_uyuni, with one addition.

    mgradm and mgrctl are not in the default repositories of an SL Micro image, so Uyuni's community OBS repository is added first.
    zypper ref can print a key-import error on the read-only RPM database and still exit 0. The exit code is the signal that counts.
    The transactional-update install imports the key inside its own snapshot.
    """
    repo_url = ("https://download.opensuse.org/repositories/systemsmanagement:/Uyuni:/Stable/"
                "images/repo/Uyuni-Server-POOL-$(arch)-Media1/")
    print("- Adding the Uyuni server package repository")
    ssh_run(hostname, "zypper --non-interactive ar --refresh {} uyuni-server-stable".format(repo_url),
            check=False)
    r = ssh_run(hostname, "zypper --non-interactive --gpg-auto-import-keys refresh uyuni-server-stable",
                check=False)
    if r.returncode != 0:
        die("could not add/refresh the Uyuni server repository on '{}'".format(hostname))

    print("- Installing mgradm tooling")
    ssh_run(hostname, "transactional-update --quiet pkg install -y mgradm mgradm-bash-completion "
                       "mgrctl mgrctl-bash-completion uyuni-storage-setup-server")
    reboot_vm(virt_srv, hostname)
    time.sleep(5)
    check_ssh_conn(hostname)

    extra_dsk = cfg.get("uyuni_extra_dsk") or ""
    if extra_dsk:
        for dsk in extra_dsk.split():
            print("- Mounting extra disk {}".format(dsk))
            device, mountpoint = dsk.split(",", 1)
            ssh_run(hostname, "echo '{} {} xfs defaults,nofail 1 2' >> /etc/fstab".format(device, mountpoint))
        reboot_vm(virt_srv, hostname)
        time.sleep(5)
        check_ssh_conn(hostname)

    print("- Installing Uyuni server")
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    # The admin e-mail is set with the top-level --email flag, because mgradm has no --admin-email flag.
    # Every value is shell-quoted, so a value with spaces stays one argument.
    install_cmd = (
        "mgradm install podman "
        "--admin-login {} "
        "--admin-password {} "
        "--email {} "
        "--ssl-password {} "
        "--organization {}".format(
            shlex.quote(admin), shlex.quote(password),
            shlex.quote(cfg.get("uyuni_email") or "admin@lab.local"),
            shlex.quote(cfg.get("uyuni_ssl_password") or password),
            shlex.quote(cfg.get("uyuni_org") or "lab")))
    _run_install_with_pg_hba_guard(hostname, install_cmd)

    time.sleep(60)
    ssh_run(hostname, "reboot", check=False)
    time.sleep(5)
    check_ssh_conn(hostname)
    _ensure_server_container_active(hostname)

    print("Uyuni available at: https://{}  ({} / {})".format(hostname, admin, password))

    channels = cfg.get("uyuni_channels") or ""
    if channels:
        count = 0
        print("- Waiting for channel list to sync")
        while True:
            time.sleep(10)
            count += 1
            print("Retry {}".format(count), end="\r")
            out = ssh_run(hostname, "mgrctl exec -- mgr-sync list channels 2>/dev/null",
                          check=False, capture=True).stdout or ""
            if any("no channels found." not in line.lower() for line in out.splitlines()):
                break
        time.sleep(300)
        ssh_run(hostname, "mgrctl exec -- mgr-sync add channels {}".format(channels))

    sync_channels = (cfg.get("uyuni_sync_channels") or "").split()
    config_channels = cfg.get("uyuni_config_channels") or []
    orgs = cfg.get("uyuni_orgs") or []
    access_groups = cfg.get("uyuni_access_groups") or []
    ansible_paths = cfg.get("uyuni_ansible_paths") or []
    content_projects = cfg.get("uyuni_content_projects") or []
    activation_keys = cfg.get("uyuni_activation_keys") or []
    system_groups = cfg.get("uyuni_system_groups") or []
    custom_info_keys = cfg.get("uyuni_custom_info_keys") or []
    system_tags = cfg.get("uyuni_system_tags") or []
    environments = cfg.get("uyuni_environments") or []
    if (cfg.get("uyuni_activation_key") or sync_channels or config_channels or orgs
            or access_groups or ansible_paths or content_projects or activation_keys
            or system_groups or custom_info_keys or system_tags or environments):
        exec_prefix = "mgrctl exec --"
        sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
        rps = sc.run_provisioning_step
        rps("channels sync", sc.ensure_channels_synced, hostname, exec_prefix, sync_channels)
        rps("config channels", sc.ensure_config_channels, hostname, exec_prefix, cfg, "uyuni")
        # System groups BEFORE any activation key — see install_smlm.py's
        # identical comment on the same reorder for why (activationkey_
        # addgroups dies if the named group doesn't exist yet server-side).
        rps("system groups", sc.ensure_system_groups, hostname, exec_prefix, cfg, "uyuni")
        # activation key(s) depend on the system groups step just above —
        # see install_smlm.py's identical comment on this same retry window.
        rps("activation key", sc.ensure_activation_key, hostname, exec_prefix, cfg, "uyuni",
            retries=3, retry_delay=15)
        rps("appstreams", sc.ensure_appstreams, hostname, exec_prefix, cfg, "uyuni")
        rps("activation key packages", sc.ensure_activation_key_packages, hostname, exec_prefix, cfg, "uyuni")
        rps("activation keys", sc.ensure_activation_keys, hostname, exec_prefix, cfg, "uyuni",
            retries=3, retry_delay=15)
        rps("users", sc.ensure_users, hostname, exec_prefix, cfg, "uyuni")
        rps("access groups", sc.ensure_access_groups, hostname, exec_prefix, cfg, "uyuni")
        # Each step runs through run_provisioning_step(), so a failure in one step does not skip the steps after it.
        # The Ansible control node and its paths get the largest retry window, because they depend on a client that registers on its own.
        rps("ansible control node", sc.ensure_ansible_control_node, hostname, exec_prefix, cfg, "uyuni",
            retries=10, retry_delay=60)
        rps("ansible paths", sc.ensure_ansible_paths, hostname, exec_prefix, cfg, "uyuni",
            retries=10, retry_delay=60)
        rps("content projects", sc.ensure_content_projects, hostname, exec_prefix, cfg, "uyuni")
        rps("SCAP policies", sc.ensure_scap_policies, hostname, exec_prefix, cfg, "uyuni", admin, password)
        rps("custom info keys", sc.ensure_custom_info_keys, hostname, exec_prefix, cfg, "uyuni")
        rps("system tags", sc.ensure_system_tags, hostname, exec_prefix, cfg, "uyuni")
        rps("environments", sc.ensure_environments, hostname, exec_prefix, cfg, "uyuni")
        rps("organizations", sc.ensure_orgs, hostname, exec_prefix, cfg, "uyuni", admin, password,
            retries=3, retry_delay=15)


def run_ansible_playbooks(hostname, cfg):
    """
    Schedules every entry in uyuni_ansible_playbooks (see the JSON section
    comment above) via sc.schedule_ansible_playbook, once per invocation —
    NOT idempotent, NOT part of the automatic setup_uyuni() flow (see
    libs/spacecmd_common.py for why). Prints each run's action id and how
    to check on it afterwards.
    """
    playbooks = cfg.get("uyuni_ansible_playbooks") or []
    if not playbooks:
        print("No uyuni_ansible_playbooks entries in the 'uyuni' JSON section — nothing to run.")
        return
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
    for pb in playbooks:
        control_node_id = pb.get("control_node_id")
        playbook_path = pb.get("playbook_path")
        inventory_path = pb.get("inventory_path")
        if control_node_id is None or not playbook_path or not inventory_path:
            print("ERROR: uyuni_ansible_playbooks entry missing 'control_node_id'/'playbook_path'/"
                  "'inventory_path'", file=sys.stderr)
            sys.exit(1)
        action_id = sc.schedule_ansible_playbook(
            hostname, exec_prefix, control_node_id, playbook_path, inventory_path,
            earliest=pb.get("earliest"), action_chain_label=pb.get("action_chain_label") or "",
            test_mode=bool(pb.get("test_mode")), extra_vars=pb.get("extra_vars"),
            flush_cache=bool(pb.get("flush_cache")))
        print("  Check status later with: spacecmd schedule_details {a} / schedule_getoutput {a}".format(
            a=action_id))


def run_clm_actions(hostname, cfg):
    """
    Run every entry in uyuni_content_lifecycle_actions. The actions are not idempotent, and the automatic setup_uyuni() flow does
    not run them.

    Each wait is wrapped in a stuck-build recovery (see _wait_for_clm_with_restart_retry). Uyuni's asynchronous CLM align worker can
    wedge after a few builds, and then every later build or promote stays in "building". The recovery restarts uyuni-server.service and
    triggers the action again. It is specific to this deployment, which has host-level systemctl, so it lives here and not in
    spacecmd_common.py. It is best effort: the wedge is an upstream issue that this script works around.
    """
    actions = cfg.get("uyuni_content_lifecycle_actions") or []
    if not actions:
        print("No uyuni_content_lifecycle_actions entries in the 'uyuni' JSON section — nothing to run.")
        return
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)

    for a in actions:
        project = a.get("project")
        action = a.get("action")
        if not project or action not in ("build", "promote"):
            die("uyuni_content_lifecycle_actions: an entry needs 'project' and "
                "action 'build' or 'promote'")

        _trigger_clm_action(hostname, exec_prefix, project, action, a)

        if a.get("wait"):
            wait_env = a.get("wait_env")
            if not wait_env:
                die("content_lifecycle_actions: 'wait' requires 'wait_env' (the environment to poll — "
                    "the first stage for a build, the successor stage for a promote)")
            status = _wait_for_clm_with_restart_retry(hostname, exec_prefix, project, action, a, wait_env,
                                                       timeout=a.get("wait_timeout") or 1800)
            print("  Environment '{}/{}' reached status '{}'".format(project, wait_env, status))


def _trigger_clm_action(hostname, exec_prefix, project, action, action_cfg):
    """Shared helper for run_clm_actions / _wait_for_clm_with_restart_retry: issues one build or promote call."""
    if action == "build":
        sc.build_content_project(hostname, exec_prefix, project, action_cfg.get("message"))
    else:
        from_env = action_cfg.get("from_env")
        if not from_env:
            die("content_lifecycle_actions: a 'promote' entry requires 'from_env'")
        sc.promote_content_project(hostname, exec_prefix, project, from_env)


def _wait_for_clm_with_restart_retry(hostname, exec_prefix, project, action, action_cfg, wait_env,
                                      timeout=1800, stall_timeout=300, max_restarts=1):
    """
    Wraps sc.wait_for_content_environment with the confirmed-live recovery
    for Uyuni's own CLM async-align-worker wedge (see run_clm_actions'
    docstring for the full story). Polls for up to `stall_timeout` seconds
    first — short, to catch a wedge quickly rather than burning the whole
    `timeout` budget on a build that was never going to finish on its own.
    If the environment hasn't reached a terminal status by then: restarts
    uyuni-server.service, waits for it to come back healthy (reusing
    _ensure_server_container_active's own retry logic), re-triggers the
    SAME action, then polls again for whatever time remains (up to
    `timeout` total). Does this at most `max_restarts` times before
    finally dying for real via wait_for_content_environment's own
    die_on_timeout=True path on the last attempt, so a genuinely-broken
    build still fails loudly rather than retrying forever.
    """
    remaining = timeout
    restarts = 0
    while True:
        final_attempt = restarts >= max_restarts
        this_wait = remaining if final_attempt else min(stall_timeout, remaining)
        status = sc.wait_for_content_environment(hostname, exec_prefix, project, wait_env,
                                                  timeout=this_wait, die_on_timeout=final_attempt)
        if status in ("built", "failed"):
            return status

        remaining -= this_wait
        restarts += 1
        print("  Environment '{}/{}' still '{}' after {}s — restarting uyuni-server and retrying "
              "the {} (recovery attempt {}/{})".format(
                  project, wait_env, status, this_wait, action, restarts, max_restarts))
        ssh_run(hostname, "systemctl restart uyuni-server.service", check=False)
        _ensure_server_container_active(hostname)
        _trigger_clm_action(hostname, exec_prefix, project, action, action_cfg)


def run_scap_scans(hostname, cfg):
    """
    Runs every entry in uyuni_scap_scans (see the JSON section comment
    above) via sc.run_scap_scans — heuristically idempotent per-scan, but
    NOT part of the automatic setup_uyuni() flow (see
    libs/spacecmd_common.py for why).
    """
    scans = cfg.get("uyuni_scap_scans") or []
    if not scans:
        print("No uyuni_scap_scans entries in the 'uyuni' JSON section — nothing to run.")
        return
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
    sc.run_scap_scans(hostname, exec_prefix, cfg, "uyuni")


def cve_audit(hostname, cfg, cve_id):
    """Prints audit.listSystemsByPatchStatus's raw result for `cve_id` — a
    pure read-only query, see libs/spacecmd_common.py."""
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
    print(sc.list_systems_by_patch_status(hostname, exec_prefix, cve_id))


def cve_audit_images(hostname, cfg, cve_id):
    """Prints audit.listImagesByPatchStatus's raw result for `cve_id` — the
    container/OS-image counterpart of cve_audit() above, same real 'audit'
    namespace, see libs/spacecmd_common.py's list_images_by_patch_status()."""
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
    print(sc.list_images_by_patch_status(hostname, exec_prefix, cve_id))


def run_recurring_schedules(hostname, cfg):
    """
    Runs every uyuni_environments entry's recurring_schedule (see the JSON
    section comment above) via sc.run_environment_schedules — NOT
    idempotent, NOT part of the automatic setup_uyuni() flow (see
    libs/spacecmd_common.py for why).
    """
    environments = cfg.get("uyuni_environments") or []
    if not any(e.get("recurring_schedule") for e in environments):
        print("No uyuni_environments entries with a recurring_schedule — nothing to run.")
        return
    exec_prefix = "mgrctl exec --"
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
    sc.run_environment_schedules(hostname, exec_prefix, cfg, "uyuni")


def main():
    # bash's --validate block here defines the usual helpers but never calls
    # any of them — always exits 0.
    ac.handle_common_args(__file__, __version__, validate_fn=None, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()

    cfg = definition.get("uyuni", {}) or {}
    virt_srv = config.get("VIRT_SRV", "")

    # on_addon_nodes semantics: respect an inherited _vm_name, else scan for
    # nodes with "uyuni" in their addons[] list.
    env_vm_name = os.environ.get("_vm_name") or None

    # Schedule uyuni_ansible_playbooks instead of installing when requested —
    # deliberately a separate, explicit trigger rather than part of the
    # automatic flow below, since scheduling a playbook run is not
    # idempotent (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-ansible-playbooks":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            run_ansible_playbooks(vm_name, cfg)
        return

    # Run uyuni_content_lifecycle_actions instead of installing when
    # requested — same reasoning as --run-ansible-playbooks: build/promote
    # are not idempotent (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-clm-actions":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            run_clm_actions(vm_name, cfg)
        return

    # Schedule uyuni_scap_scans instead of installing when requested — same
    # reasoning as --run-ansible-playbooks/--run-clm-actions.
    if len(sys.argv) > 2 and sys.argv[2] == "--run-scap-scans":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            run_scap_scans(vm_name, cfg)
        return

    # Ad-hoc CVE/OVAL patch-status audit — read-only, takes the CVE id as a
    # third argument.
    if len(sys.argv) > 3 and sys.argv[2] == "--cve-audit":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            cve_audit(vm_name, cfg, sys.argv[3])
        return

    # audit.listImagesByPatchStatus's own CLI entry point — same shape as
    # --cve-audit above, for container/OS images instead of systems.
    if len(sys.argv) > 3 and sys.argv[2] == "--cve-audit-images":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            cve_audit_images(vm_name, cfg, sys.argv[3])
        return

    # Create uyuni_environments' recurring_schedule entries instead of
    # installing when requested — same reasoning as
    # --run-ansible-playbooks/--run-clm-actions/--run-scap-scans: recurring
    # action idempotency was never confirmed (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-recurring-schedules":
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
            run_recurring_schedules(vm_name, cfg)
        return

    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
        setup_uyuni(vm_name, virt_srv, cfg)


if __name__ == "__main__":
    main()
