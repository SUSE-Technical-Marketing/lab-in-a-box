#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Uyuni server on a dedicated host VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Uyuni is an open-source systems management solution (upstream of SUSE Multi-Linux Manager).
# This script installs it on a dedicated host VM using mgradm (container-based install).
# The target VM must run openSUSE Leap 15.6 / SLE Micro with podman available.
#
# JSON section: "uyuni" — configurable keys:
#   uyuni_admin         : [OPTIONAL] admin username (default: admin)
#   uyuni_password      : [OPTIONAL] admin password (default: Uyuni12345)
#   uyuni_email         : [OPTIONAL] admin email (default: admin@lab.local)
#   uyuni_org           : [OPTIONAL] default organisation name (default: lab)
#   uyuni_ssl_password  : [OPTIONAL] SSL certificate password (default: same as uyuni_password)
#   uyuni_channels      : [OPTIONAL] space-separated list of channels to sync after install
#   uyuni_extra_dsk     : [OPTIONAL] extra disk to mount for storage (e.g. /dev/vdb,/srv/mirror)
#
# OPTIONAL – activation key (created after install, once the server is up;
# skipped entirely if uyuni_activation_key is unset). Command syntax verified
# against uyuni-project.org live docs (2026-08-27); NOT live-tested against a
# real server — see libs/spacecmd_common.py.
#   uyuni_activation_key       : Activation key name                (default: unset — skipped)
#   uyuni_activation_key_desc  : Description                        (default: same as key name)
#   uyuni_activation_key_base_channel : Base channel label — required if uyuni_activation_key is set
#   uyuni_activation_key_child_channels : Space-separated child channel labels to add
#   uyuni_activation_key_universal_default : "true" to mark this key as the org's universal
#                             default                               (default: false)
#   uyuni_activation_key_entitlements : Comma-separated entitlements, e.g.
#                             "enterprise_entitled,virtualization_host"
#   uyuni_activation_key_contact_method : Contact method to set on the key
#   uyuni_activation_key_config_channels : Space-separated config channel labels to add
#   uyuni_activation_key_enable_config_deployment : "true" to enable config-file deployment
#                             on the key                            (default: false)
#   uyuni_activation_key_groups : Space-separated system group names to add
#   uyuni_activation_key_appstreams : Space-separated "module:stream" pairs to enable on the key
#                             (e.g. "nodejs:20 postgresql:16"), via spacecmd's 'api' passthrough
#                             calling activationkey.addAppStreams — applied on every run, not just
#                             at key-creation time (idempotent: an already-enabled module is
#                             detected from the server's own error and skipped, since there is no
#                             list API for this — see libs/spacecmd_common.py)
#   uyuni_activation_key_packages : Space-separated package names to add to the key (name-only, no
#                             arch qualification — see libs/spacecmd_common.py), via spacecmd's
#                             native activationkey_addpackages — applied on every run, not just at
#                             key-creation time (idempotent: diffs against
#                             activationkey_listpackages first and only adds what's missing)
#   uyuni_sync_channels        : Space-separated software channel labels to ensure are synced
#                             (each via 'mgr-sync add channel <label>' if not already present in
#                             'spacecmd softwarechannel_list') before the activation key is created
#                             — independent of, and safe to use alongside, uyuni_channels above
#
# OPTIONAL – config channels (created/updated before the activation key above, so
# uyuni_activation_key_config_channels can reference them). List of objects:
#   uyuni_config_channels     : [{
#                                 "label": "...", "name": "...", "description": "...",
#                                 "type": "normal" | "state"  (default: "normal"),
#                                 "init_sls": "..."            (state channels only),
#                                 "files": [{"path": "...", "content": "...",
#                                            "owner": "root", "group": "root",
#                                            "mode": "0644", "binary": false}, ...]
#                               }, ...]
#                             Idempotent per-channel and per-file (a file already matching its
#                             content's sha256 is skipped) — see libs/spacecmd_common.py. Does NOT
#                             associate the channel with any already-registered system directly
#                             (that's client-side, out of scope here); use
#                             uyuni_activation_key_config_channels for newly-registered clients.
#
# OPTIONAL – organizations (created after the above; each org gets its own admin session
# for its own scoped provisioning). List of objects:
#   uyuni_orgs                : [{
#                                 "name": "...", "admin_user": "...", "admin_pass": "...",
#                                 "admin_email": "...", "admin_first_name": "...",
#                                 "admin_last_name": "...", "prefix": "...", "pam": false,
#                                 "trust_with": ["other-org-name", ...],
#                                 "share_channels": ["channel-label", ...],
#                                 "share_channels_access": "protected" | "public" | "private",
#                                 # plus this org's OWN uyuni_activation_key*/uyuni_config_channels
#                                 # keys, same field names as above — reused as-is since once
#                                 # this org's admin session is active, activation keys/config
#                                 # channels are automatically scoped to it (hard-partitioned
#                                 # per org server-side)
#                                 "uyuni_activation_key": "...", "uyuni_config_channels": [...],
#                                 "uyuni_access_groups": [...]   # see below — also reused per-org
#                               }, ...]
#                             admin_user/admin_pass/admin_email are required to create the org
#                             (skipped — not idempotent-creatable — otherwise). trust_with names
#                             other orgs to establish channel-sharing trust with — e.g. the value
#                             of uyuni_org above, the default org this server bootstrapped with.
#                             share_channels additionally marks channels THIS org owns as shared
#                             (via the raw channel.access.setOrgSharing API — spacecmd has no
#                             subcommand for it) so a trusted org's activation keys can reference
#                             them. See libs/spacecmd_common.py for what's confirmed vs. inferred
#                             here (trust's bidirectionality in particular).
#
# OPTIONAL – user accounts. List of objects, usable at the top level (scoped to the default
# org) or nested inside a uyuni_orgs entry (scoped to that org — same field name either way).
# Runs automatically on every install (idempotent), BEFORE uyuni_access_groups below so its own
# "users" list can reference an account defined here:
#   uyuni_users                : [{
#                                 "username": "...", "password": "...", "first_name": "...",
#                                 "last_name": "...", "email": "...", "pam": false,
#                                 "roles": ["channel_admin", ...]   # optional, see below
#                               }, ...]
#                             password/first_name/last_name/email are required to create the
#                             account (skipped — not idempotent-creatable — otherwise, same
#                             convention as an org's own admin_user/admin_pass/admin_email).
#                             "roles" are applied on every run via spacecmd's native user_addrole
#                             (idempotent — diffed against the user's current roles first), but
#                             ONLY use it for one of the fixed labels from 'spacecmd
#                             user_listavailableroles' (activation_key_admin, channel_admin,
#                             config_admin, image_admin, org_admin, regular_user, satellite_admin,
#                             system_group_admin) — those always exist. Do NOT put a custom access
#                             group's own label here: uyuni_users runs BEFORE uyuni_access_groups
#                             below (an access group's own "users" list needs the account to
#                             already exist), so the custom role wouldn't exist yet and
#                             user_addrole would fail. Attach a user to a custom group the other
#                             way instead — list their username in that group's own "users" field
#                             below, which runs in the correct order.
#
# OPTIONAL – RBAC / custom "User Access Groups" (API-only feature, Uyuni 2025.05+ / SMLM 5.1+).
# List of objects, usable at the top level (scoped to the default org) or nested inside a
# uyuni_orgs entry (scoped to that org — same field name either way):
#   uyuni_access_groups       : [{
#                                 "label": "...", "description": "...",
#                                 "permissions_from": ["existing-role-label", ...],
#                                 "permissions": [{"namespace": "...", "mode": "R" | "W"}, ...],
#                                 "users": ["username", ...]
#                               }, ...]
#                             Each username must exist by the time this runs — defined above via
#                             uyuni_users, or an org's own admin_user, or attaching the role fails
#                             with a clear error. Every access_* operation goes through the raw
#                             'api' passthrough (spacecmd has no native subcommand for this
#                             namespace at all) — see libs/spacecmd_common.py.
#
# OPTIONAL – Ansible integration (API-only, orchestration only — does NOT push playbook/inventory
# content; the control node must already be a registered system with the "Ansible Control Node"
# add-on entitlement enabled, with playbook/inventory files already on its filesystem, managed
# out-of-band e.g. via git). Path registration runs automatically on every install (idempotent);
# playbook execution is a SEPARATE, explicit trigger — see "--run-ansible-playbooks" below —
# since scheduling a run is not idempotent (each call creates a brand-new run):
#   uyuni_ansible_paths       : [{"control_node_id": 1000010001, "type": "playbook" | "inventory",
#                                  "path": "/srv/ansible/playbooks"}, ...]
#                             control_node_id is the target's NUMERIC Uyuni system ID (findable via
#                             'spacecmd system_list' or the Web UI) — no name-based resolution is
#                             provided here.
#   uyuni_ansible_playbooks   : [{"control_node_id": 1000010001,
#                                  "playbook_path": "/srv/ansible/playbooks/site.yml",
#                                  "inventory_path": "/srv/ansible/inventory/hosts",
#                                  "earliest": "2026-08-27T12:00:00",   # optional, default: now
#                                  "action_chain_label": "...",         # optional
#                                  "test_mode": false, "extra_vars": "...", "flush_cache": false
#                                }, ...]
#                             Run with:  install_uyuni.py <lab.json> --run-ansible-playbooks
#                             (never runs automatically). See libs/spacecmd_common.py for the
#                             dateTime-encoding and orchestration-only-model details.
#
# OPTIONAL – Content Lifecycle Management (CLM). Project/source/filter/environment DEFINITION
# runs automatically on every install (idempotent, in this order so activation keys etc. can
# reference the environments); BUILD/PROMOTE are a SEPARATE, explicit trigger — see
# "--run-clm-actions" below — since each call triggers real, non-idempotent background work:
#   uyuni_content_projects    : [{
#                                 "label": "...", "name": "...", "description": "...",
#                                 "sources": ["software-channel-label", ...],
#                                 "filters": [{"name": "...", "rule": "allow" | "deny",
#                                              "entity_type": "package" | "erratum" | "module" | "ptf",
#                                              "matcher": "...", "field": "...", "value": "..."}, ...],
#                                 "environments": ["dev", "test", "prod"]
#                                 # or [{"label": "...", "name": "...", "description": "..."}, ...]
#                               }, ...]
#                             "sources" only supports software-channel sources (the only Source
#                             type that exists server-side). Filters have no lookup-by-name API —
#                             idempotency is checked at the project level (does it already have a
#                             filter by this name), and a freshly-created filter's id is parsed
#                             heuristically from spacecmd's own printed output — see
#                             libs/spacecmd_common.py for that caveat in detail.
#   uyuni_content_lifecycle_actions : [
#                                 {"project": "...", "action": "build", "message": "...",
#                                  "wait": true, "wait_env": "dev", "wait_timeout": 1800},
#                                 {"project": "...", "action": "promote", "from_env": "dev",
#                                  "wait": true, "wait_env": "test"}
#                               ]
#                             "from_env" on a promote is the stage being promoted FROM, not the
#                             destination — the server determines the successor itself (confirmed
#                             from source; the admin-guide prose is ambiguous about this). "wait"
#                             polls the named environment's status until built/failed. Run with:
#                             install_uyuni.py <lab.json> --run-clm-actions (never automatic).
#
# OPTIONAL – SCAP compliance auditing (legacy pre-staged-file model only — spacecmd's native
# scap_* commands don't cover Uyuni/SMLM 5.2's newer "centralized policies" Technology Preview
# layer, deliberately not automated here, see libs/spacecmd_common.py). Orchestration only:
# xccdf_path (and the OpenSCAP scanner + SCAP Security Guide content) must already exist on the
# target system. Explicit trigger only — see "--run-scap-scans" below:
#   uyuni_scap_scans          : [{"system": "web1.mydemo.lab",
#                                  "xccdf_path": "/usr/share/openscap/scap-security-xccdf.xml",
#                                  "profile": "Web-Default"}, ...]
#                             Heuristically idempotent (skips a system already scanned against the
#                             same xccdf_path — path only, not path+profile). Run with:
#                             install_uyuni.py <lab.json> --run-scap-scans (never automatic).
#
# OPTIONAL – CVE/OVAL audit (fully supported since SMLM 5.2 / stable in Uyuni). Pure read-only
# query, no JSON config — run with:
#   install_uyuni.py <lab.json> --cve-audit CVE-YYYY-NNNNN
# prints every system's patch status for that CVE (AFFECTED_PATCH_INAPPLICABLE/
# AFFECTED_PATCH_APPLICABLE/NOT_AFFECTED/PATCHED).
#
# OPTIONAL – dev/QA/prod environment topology. A THIN COMPOSITION layer over the primitives
# above plus system groups/tags — Uyuni itself has no native "environment" or "release" object
# tying these together (see libs/spacecmd_common.py). All idempotent and automatic EXCEPT
# recurring_schedule (explicit trigger only — see "--run-recurring-schedules" below):
#   uyuni_activation_keys     : [{...}, ...]   # same field names as uyuni_activation_key* above,
#                             one dict per key — lets you define MULTIPLE named keys (e.g. one per
#                             environment) without needing a separate org per key
#   uyuni_system_groups       : [{"name": "...", "description": "...",
#                                  "systems": ["existing-system-name", ...]}, ...]
#   uyuni_custom_info_keys    : [{"name": "...", "description": "..."}, ...]
#                             Org-level key definitions — required before uyuni_system_tags/
#                             environments' custom_info_tags can set any value for that key.
#   uyuni_system_tags         : [{"system": "...", "tags": {"key": "value", ...}}, ...]
#                             Uyuni has no first-class "tag" object — this sets
#                             system.custominfo key/value pairs, the closest real mechanism.
#   uyuni_environments        : [{
#                                 "label": "dev",
#                                 "system_group": "dev-systems",     # name ref into system_groups
#                                 "activation_key": "1-dev-key",     # name ref into activation_keys
#                                 "custom_info_tags": {"tier": "dev"},
#                                 "recurring_schedule": {
#                                   "type": "highstate" | "custom",   # default: "highstate"
#                                   "cron": "0 2 * * 2",
#                                   "states": ["..."],                # required if type is "custom"
#                                   "group_id": 123,                  # optional: skip name->id lookup
#                                   "extra": {...}                    # merged into the API struct as-is
#                                 }
#                               }, ...]
#                             system_group/activation_key are NAME REFERENCES only — define them via
#                             the fields above, not inline here. recurring_schedule needs a NUMERIC
#                             group id; by default it's resolved heuristically from the group's name
#                             (see libs/spacecmd_common.py's group_id_for) — supply "group_id"
#                             directly if that resolution fails. Run schedules with:
#                             install_uyuni.py <lab.json> --run-recurring-schedules (never automatic
#                             — recurring-action idempotency was never confirmed).
#
# The target node must have "uyuni" in its addons[] list in the JSON definition:
#   "nodes": { "uyuni.lab": { "myip": "...", "addons": ["uyuni"] } }

__version__ = "ca2d2d5"

PLUGIN = {
    "name": "uyuni",
    "targets": ["vm", "baremetal"],
    # standalone-container, not kubernetes: confirmed live 2026-08-29 — this
    # script's own install() runs `mgradm ... install podman` (see its
    # docstring: "Installed on a dedicated host VM using mgradm
    # (container-based install)"), a real podman container directly on the
    # host, never touching kubectl/helm at all (requires_kubernetes below
    # is already None, which this "kubernetes" layer value contradicted —
    # a leftover DEFAULT_PLUGIN artifact from the mechanical sweep, never
    # actually corrected for this addon specifically).
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
# Moved to libs/mgradm_common.py 2026-09-12 — see that module's own docstring
# for why (in short: `from install_uyuni import ...` breaks once this script
# is deployed without its .py suffix, confirmed live). Aliased back to their
# original leading-underscore names here so nothing else in this file, or in
# tests/checks/18_live_bugfixes_test.py, needs to change.
from mgradm_common import (  # noqa: E402
    run_install_with_pg_hba_guard as _run_install_with_pg_hba_guard,
    ensure_server_container_active as _ensure_server_container_active,
)


def setup_uyuni(hostname, virt_srv, cfg):
    """
    Install Uyuni on a host VM via mgradm. Mirrors setup_uyuni (bash) — with
    one addition: bash's own version (libs/lab_creation.bash, pre-existing,
    not a python-port regression) assumed mgradm/mgrctl were already
    installable from whatever repos the target image ships with. Confirmed
    live (2026-08-28, disposable VM on nuc6.mydemo.lab) that a stock
    SL-Micro image does NOT have them — "No provider of 'mgradm' found" —
    they ship from Uyuni's own community OBS repo, not any SCC
    module/product. Fixed by adding that repo first, per
    documentation.suse.com/multi-linux-manager's own Micro-variant
    deployment guide (verified live 2026-08-28): `zypper ar` writes to
    /etc/zypp (writable), but the key-trust import a `zypper ref` on a
    brand-new repo triggers touches the RPM database under
    /usr/lib/sysimage/rpm — read-only on SLE Micro outside a
    transactional-update snapshot. Confirmed live that this makes `zypper
    ref` itself report an internal "Failed to import public key" error yet
    still exit 0, and the subsequent transactional-update install (which
    imports the key for real, inside its own writable snapshot) succeeds
    regardless — so ref's own exit code is checked, but its own stderr
    warning about the failed key import is expected noise, not a real
    failure signal.
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
    # --admin-email doesn't exist on mgradm's actual CLI (confirmed live,
    # 2026-08-28, via `mgradm install podman --help`: admin email is the
    # top-level `--email` flag, not one of the "First User" admin-* flags —
    # `mgradm install` rejected it outright with "unknown flag: --admin-email".
    # bash's own version (libs/lab_creation.bash) has this identical bug,
    # pre-existing, not a python-port regression.
    # Every value shell-quoted — confirmed live 2026-09-13 (install_smlm.py,
    # which mirrors this exact command shape): an unquoted multi-word
    # --organization got split by the remote shell into two tokens, the
    # second of which mgradm misinterpreted as its own optional FQDN
    # positional argument. Same latent bug here — uyuni_org just never
    # happened to contain a space in practice, so it was never hit live.
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
        sc.ensure_channels_synced(hostname, exec_prefix, sync_channels)
        sc.ensure_config_channels(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_activation_key(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_appstreams(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_activation_key_packages(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_activation_keys(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_users(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_access_groups(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_ansible_paths(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_content_projects(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_system_groups(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_custom_info_keys(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_system_tags(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_environments(hostname, exec_prefix, cfg, "uyuni")
        sc.ensure_orgs(hostname, exec_prefix, cfg, "uyuni", admin, password)


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
    Runs every entry in uyuni_content_lifecycle_actions (see the JSON
    section comment above) — NOT idempotent, NOT part of the automatic
    setup_uyuni() flow (see libs/spacecmd_common.py for why).

    Unlike a plain delegation to sc.run_content_lifecycle_actions, each
    'wait' is wrapped with a stuck-build recovery (see
    _wait_for_clm_with_restart_retry): confirmed live (2026-08-28, round 4
    of the CLM stuck-build investigation — see MIGRATION_TODO.md, "the Web
    UI 'Build' button theory, tested and disproven") that Uyuni's own
    async CLM align worker can get itself permanently wedged after a
    small, non-deterministic number of builds, independent of which API
    triggers them — once wedged, EVERY subsequent CLM build/promote hangs
    in "building" forever, and the ONLY confirmed mitigation is
    restarting uyuni-server.service (clears the server's in-process async
    message queue) followed by re-triggering the same action. This
    recovery is deployment-specific (host-level systemctl, "uyuni"/mgrctl
    only — no equivalent for "smlm"/kubectl pods), which is why it lives
    here rather than in the shared spacecmd_common.py module. Best-effort,
    not a guarantee — the underlying wedge is a genuine, unexplained
    upstream bug this repo cannot fix, only work around.
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
