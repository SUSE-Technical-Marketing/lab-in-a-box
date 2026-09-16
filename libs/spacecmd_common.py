"""
spacecmd_common.py — shared spacecmd/mgr-sync activation-key + channel-sync
helpers for SMLM (install_smlm.py, Kubernetes deployment, kubectl exec) and
Uyuni (install_uyuni.py, single-VM podman deployment via mgradm, mgrctl
exec) — same underlying server tooling (SUSE Multi-Linux Manager/Uyuni server), reached
through a different exec wrapper depending on how each product is deployed
in this project. Callers pass that wrapper as `exec_prefix` (e.g.
"kubectl exec -n {ns} deploy/uyuni -c uyuni --" or "mgrctl exec --").

Command syntax verified against live documentation.suse.com/multi-linux-manager
and uyuni-project.org docs (2026-08-27, current SMLM 5.0/5.1/5.2 and Uyuni
2024.08-2026.08). NOT live-tested against a real server — no SMLM/Uyuni
instance is available in this project to test against (same constraint as
the rest of this migration). Specific caveats from that research, flagged
inline below:
  - activationkey_details' exit code on a missing key is undocumented, so
    existence is checked by grepping activationkey_list's output instead.
  - mgr-sync's plural "add channels <label1> <label2>" form (already used,
    unrelated to this module, by install_uyuni.py's existing uyuni_channels
    handling) could not be confirmed against current docs — this module
    deliberately uses the singular, one-channel-per-call form instead
    (confirmed from uyuni-project.org's own examples).
  - AppStream selection (ensure_appstreams, added 2026-08-27): verified
    directly against Uyuni's Java source (ActivationKeyHandler.java,
    ActivationKeyManager.java, commit 44859e6), not just docs — spacecmd has
    no native subcommand for this, only the generic 'api' passthrough calling
    activationkey.addAppStreams/removeAppStreams. There is no list/get API
    for currently-enabled AppStreams (confirmed absent from source), and
    addAppStreams is NOT idempotent server-side: re-adding an already-enabled
    module faults with XML-RPC code -309 ("duplicateStream"), message
    "App stream '<name>' already exists in the activation key." — that exact
    message substring is what this module checks for instead of pre-listing.
    Confirmed present with an identical signature in SMLM 5.1 as well as
    Uyuni (corrects an earlier assumption in TODO that SMLM lacked this).
  - Package assignment (activation_key_packages/ensure_activation_key_packages,
    added 2026-08-27): corrects a DIFFERENT earlier mistake in this project's
    own research — a prior pass concluded "no activationkey_* command for
    package profile" was found, but that had searched for the wrong term.
    The real, spacecmd-NATIVE mechanism is activationkey_addpackages/
    activationkey_removepackages/activationkey_listpackages (wrapping
    activationkey.addPackages/removePackages/getDetails' packages array),
    confirmed by reading ActivationKeyHandler.java and spacecmd's own
    activationkey.py directly. "Package profile" is unrelated Uyuni
    terminology for a completely different feature —
    system.createPackageProfile/comparePackageProfile — a saved snapshot of
    an already-REGISTERED system's installed packages for later diffing,
    nothing to do with activation keys. Since activationkey_listpackages
    gives a reliable list to diff against (unlike AppStreams above, which
    has no list API at all), this is genuinely idempotent — not a
    fault-string heuristic — and, like ensure_appstreams, runs
    unconditionally rather than only at key-creation time. Name-only: the
    underlying API struct supports an optional per-package 'arch' field,
    but spacecmd's own CLI wrapper never surfaces it, so neither does this
    module.
  - Config channels (ensure_config_channels and friends, added 2026-08-27):
    verified directly against spacecmd's own source
    (spacecmd/src/spacecmd/configchannel.py, GitHub master), since the
    published Uyuni/SMLM 5.1 doc pages both lag behind it (missing the
    'normal'/'state' -t/--type flag on configchannel_create and the
    configchannel_listgroups subcommand entirely — neither is documented on
    either site as of this check). File content is passed to
    configchannel_addfile/configchannel_updateinitsls via a LOCAL file path
    (-f), not inline text or stdin, so this module stages content to a
    remote temp file first (cat > via input_text, same idiom as
    ensure_spacecmd_config), then removes it. There's no dedicated
    channel-existence or single-file-diff command; existence is checked by
    grepping configchannel_list's output (same idiom as
    activation_key_exists), and file-level idempotency by looking for a
    locally-computed sha256 substring in configchannel_filedetails' output
    (a heuristic — a trailing-newline/encoding mismatch would just cause a
    harmless redundant push, not corruption). Salt "state" channels are
    fully wrapped by the same configchannel_* commands (only the type
    differs) — no separate Salt file_roots/SSH mechanism is needed. Directly
    associating a channel with an already-registered system
    (system_addconfigchannels et al.) is deliberately NOT covered here —
    that's client-targeting and belongs with the separate, not-yet-designed
    client-registration addon; this module only manages channels/content on
    the server itself, same scope as the rest of this file.
  - Organizations (ensure_orgs and friends, added 2026-08-27): verified
    directly against spacecmd's org.py source (GitHub master) plus the
    org/org.trusts/channel.access XML-RPC API references. The single fact
    that drives this design: org-scoping in spacecmd is 100% a function of
    WHICH USER a session is authenticated as — grepped do_login() and every
    other module in spacecmd's source, there is no -o/--org flag or
    session-switch command anywhere. Activation keys and config channels are
    hard-partitioned per org (confirmed in the activationkey.create API doc:
    a key named "foo" becomes literally "100-foo" for org 100), so once
    ensure_spacecmd_config() re-authenticates as a given org's own admin,
    the existing ensure_activation_key/ensure_config_channels/
    ensure_appstreams functions work completely unchanged, scoped
    automatically by whichever session is active — no new provisioning
    logic needed for org-scoped keys/channels, only session-switching around
    the existing calls. Software channels are different: shared storage,
    access-gated per org via org.trusts (org_addtrust) PLUS the owning org
    marking the channel 'protected'/'public' via channel.access.setOrgSharing
    — which has no spacecmd subcommand at all, only the generic 'api'
    passthrough (confirmed absent from source). org_list prints one org name
    per line with no header, so existence is checked by an exact-line match
    (stricter than the substring checks used for activation
    keys/channels above, since org names are shorter and more collision-prone).
    Trust's bidirectionality is inferred from the docs' own phrasing
    ("establishing trust... allow them to share content between them"), not
    independently re-verified — there is no live server to test either
    direction against. channel.access.getOrgSharing's exact output shape
    wasn't in the fetched docs either, so ensure_channel_sharing's
    idempotency check is a substring-match heuristic, same spirit as the
    AppStream fault-string check above.
  - RBAC / custom "User Access Groups" (ensure_access_groups and friends,
    added 2026-08-27): verified directly against AccessHandler.java (GitHub
    master) and spacecmd's actual source-tree file listing, not just docs.
    Confirmed this is a genuinely separate 'access' XML-RPC namespace from
    'user' (a custom group is an AccessGroup, not a user attribute), and
    that spacecmd has NO 'access.py' module at all — zero native
    access_* subcommands exist, so every operation here (createRole,
    grantAccess, listRoles, listPermissions) goes through the generic 'api'
    passthrough, same as AppStreams/channel-sharing above. The older, fixed
    roles (org_admin, channel_admin, etc.) remain a separate, pre-existing
    mechanism unaffected by this: user_addrole/user_removerole/user_details
    are real spacecmd-native subcommands, confirmed present in
    spacecmd/src/spacecmd/user.py, and this module reuses them as-is to
    attach a user to a custom group's label too, since a created access
    group becomes an ordinary role label server-side once it exists — no
    separate "add user to access group" API exists or is needed. Deliberate
    CORRECTION (2026-09-15, confirmed live against a real SMLM 5.2 server):
    the original research here missed that spacecmd DOES have a native
    user_create subcommand (spacecmd/src/spacecmd/user.py) — `spacecmd --
    help`'s two-column output lists it, easy to miss by eye, and the
    earlier docs-only research pass didn't catch it. ensure_users()/
    ensure_user() below now create user accounts directly, so a group's
    `users` list no longer requires the account to already exist
    elsewhere. grantAccess's own
    idempotency on a repeat call for an already-granted namespace wasn't
    confirmed, so ensure_access_group_permissions checks
    access.listPermissions first rather than assuming a repeat call is a
    safe no-op — same "check first" caution used throughout this module
    wherever idempotency wasn't independently confirmed. Feature confirmed
    present (API-only) since Uyuni 2025.05 and SMLM 5.1; a 2026.01 Web UI
    was added on top of the SAME API — no method-signature change to track.
  - Ansible integration (ensure_ansible_paths/schedule_ansible_playbook and
    friends, added 2026-08-27): verified directly against
    AnsibleHandler.java (GitHub master) and, again, a direct listing of
    spacecmd's source tree — no 'ansible.py' module exists there either
    (confirmed by a direct 404 on the raw file URL), so every ansible.*
    operation goes through the generic 'api' passthrough, same as RBAC
    above. Confirmed from the handler itself that this is
    ORCHESTRATION-ONLY, not a content-push model like config channels:
    there is no method anywhere to upload/write playbook or inventory
    content — only discoverPlaybooks/fetchPlaybookContents/introspectInventory
    (all read-only) and createAnsiblePath/schedulePlaybook. A "control node"
    is a pre-existing REGISTERED system with the "Ansible Control Node"
    add-on entitlement already enabled; playbook/inventory files already
    live on its filesystem, managed out-of-band (e.g. git) — this module
    has no way to enable that entitlement itself (no matching method was
    found in the ansible.* or system.* namespaces during research), so it's
    a documented prerequisite, not something ensure_ansible_paths can set
    up for you. createAnsiblePath/schedulePlaybook both need the control
    node's NUMERIC Uyuni system ID (not a hostname) — no name-to-ID
    resolution is provided here; stacking another unverified guess on top
    of an already-multi-step feature wasn't worth it, so the JSON just
    takes the ID directly (findable via 'spacecmd system_list' or the Web
    UI). schedulePlaybook needs an XML-RPC dateTime argument
    ("earliestOccurrence") — confirmed by reading spacecmd's
    parse_api_args/datetime_parser_lst source directly that the 'api'
    passthrough auto-converts any top-level ISO-8601-looking string
    argument into a real Python datetime before the XML-RPC call, which the
    stock (unmodified) xmlrpclib marshaller then sends as a proper
    dateTime.iso8601 — no manual DateTime construction needed on our end.
    Deliberately NOT wired into the automatic install flow the way every
    other ensure_* function in this module is: scheduling a playbook is a
    one-shot action (each call creates a brand-new scheduled run, there is
    nothing to check for idempotency against), so silently re-triggering it
    on every re-run of setup_lab.py would be a real hazard if the playbook
    itself isn't idempotent — see the install script's --run-ansible-playbooks
    flag instead of the normal automatic ensure_* wiring. Status/output of a
    scheduled run IS available through spacecmd-native commands
    (schedule_details/schedule_getoutput — the 'schedule' namespace, unlike
    'ansible', is fully wrapped natively), reused as-is by
    ansible_playbook_status(). Confirmed present in SMLM since 5.1; the
    2026.01 "playbook variables in the Web UI" release note is UI-only —
    confirmed via commit history that the underlying extraVars API field
    predates it (added 2025-03), so there was no signature change to track.
  - Content Lifecycle Management / CLM (ensure_content_projects and
    friends, added 2026-08-27): verified directly against
    ContentManagementHandler.java (GitHub master) plus ContentManager.java,
    FilterCriteria.java, ProjectSource.java and EnvironmentTarget.java for
    the underlying semantics — not docs prose alone, which turned out to be
    ambiguous about promoteProject's direction (see below). Same pattern as
    RBAC/Ansible: the 'contentmanagement' namespace has ZERO native spacecmd
    subcommands (confirmed absent from spacecmd's source tree) — every
    operation goes through the generic 'api' passthrough. Confirmed only
    "software" is a valid Source type in current source (config-channel
    sources were in CLM's original design but no such ProjectSource subclass
    was ever built) — no scoping loss from supporting only software-channel
    sources. createProject/createFilter/createEnvironment/attachSource all
    THROW on a duplicate rather than being upsert-safe, so idempotency needs
    an explicit lookup-first for each, same as everywhere else in this
    module — done via listProjects/listProjectSources/listProjectEnvironments
    substring checks (deliberately NOT lookupProject/lookupEnvironment's own
    fault/exit-code behavior on a miss, which — exactly like
    activationkey_details earlier in this file — was not confirmed, so the
    same list+grep idiom is used instead). FILTERS are a genuine gap: there
    is no lookup-by-name API for them, only by numeric ID, and that ID is
    only ever returned by createFilter itself. ensure_content_filter works
    around this in two ways, both flagged explicitly as heuristics: (1)
    idempotency is checked at the PROJECT level — does this project's
    listProjectFilters output already mention this filter name — rather
    than a true global existence check; (2) when creating fresh, the new
    filter's numeric id is extracted by regexing createFilter's raw printed
    return struct (spacecmd's passthrough prints the unmarshalled Python
    object — most likely 'id': 123 dict-repr style; the exact print format
    was NOT independently confirmed). If that extraction fails,
    ensure_content_filter dies with the literal manual 'spacecmd api'
    command to run instead of silently giving up. promoteProject(project,
    envLabel) — confirmed directly in ContentManager.java, NOT from the
    (ambiguous) admin-guide prose — takes the environment being promoted
    FROM, not the destination; it looks up envLabel then calls
    getNextEnvironmentOpt(). Build/promote are async with NO action id (an
    internal JVM message queue, not a Taskomatic action — the 'schedule'
    namespace used for Ansible's status polling doesn't apply here);
    progress is polled via lookupEnvironment's own "status" field
    (new/building/generating_repodata/built/failed), same regex-extraction
    caveat as the filter id above. Like Ansible's schedulePlaybook, build/
    promote are deliberately NOT wired into the automatic install flow —
    each call triggers real background work with no dedup to check against,
    so re-running setup_lab.py would otherwise silently re-trigger rebuilds/
    re-promotions — see the install scripts' --run-clm-actions flag instead.
    Confirmed present in SMLM (core, not optional); confirmed (not just
    repeated from the TODO) that spawalk-manage-channel-lifecycle's removal
    is stated explicitly in SMLM 5.2's OWN release notes, though upstream
    Uyuni's public notes as of this research only say "deprecated" (2025.05)
    — flagged as a real, checked distinction, not an assumption.
  - SCAP / CVE auditing (ensure_scap_scan/list_systems_by_patch_status and
    friends, added 2026-08-27): a genuinely DIFFERENT situation from
    RBAC/Ansible/CLM above — spacecmd/src/spacecmd/scap.py DOES exist and
    DOES wrap 4 native commands (scap_schedulexccdfscan/scap_listxccdfscans/
    scap_getxccdfscandetails/scap_getxccdfscanruleresults), confirmed by
    reading it directly. But that only covers the LEGACY, pre-staged-file
    XCCDF/OVAL scan model (system.scap.scheduleXccdfScan et al.) —
    orchestration-only, same control-node-content idiom as Ansible
    integration: the XCCDF document (and OpenSCAP/SCAP-Security-Guide
    packages) must already be installed on the TARGET system, this module
    pushes nothing. SMLM 5.2 also introduced a "centralized policies /
    automated remediation" layer bolted onto the SAME system.scap namespace
    (listPolicies/listScapContent/listTailoringFiles/
    scheduleBetaXccdfScanCustom/scheduleBetaXccdfScanWithPolicy) —
    confirmed to exist at the API-doc level, explicitly Technology
    Preview/Beta, with ZERO spacecmd coverage (no compliance.py/policy.py
    module exists either) — and research this round could not even
    independently confirm its Java handler source. Deliberately NOT
    implemented here: building automation against an API surface this
    unverified, on top of an explicitly Beta feature with no spacecmd
    precedent to imitate, was judged too risky — flagged as still-open in
    TODO rather than guessed at. There is also no built-in dedup for
    scheduleXccdfScan, so ensure_scap_scan() checks scap_listxccdfscans'
    raw output for the target xccdf_path first (a heuristic — it matches on
    path only, not path+profile, since listXccdfScans doesn't surface the
    profile without a further per-scan getXccdfScanDetails round trip).
    Deliberately NOT wired into the automatic install flow — scheduling a
    scan is one-shot, real work — see the install scripts' --run-scap-scans
    flag instead, same reasoning as Ansible/CLM. CVE/OVAL auditing
    (audit.listSystemsByPatchStatus) is a SEPARATE, unrelated mechanism from
    either SCAP path above — confirmed absent from spacecmd entirely (no
    audit.py; errata.py's CVE-related commands only look up published
    ERRATA, an older channel-metadata-based mechanism, not the OVAL-based
    per-system patch-status audit) — so it goes through the generic 'api'
    passthrough. It's a pure read-only query with nothing to schedule and no
    idempotency concern. Confirmed (matches the TODO exactly): CVE/OVAL
    audit was Technology Preview in SMLM 5.1, fully supported since 5.2.
  - dev/QA/prod environment topology (ensure_environments and friends,
    added 2026-08-27): a THIN COMPOSITION layer, not a new Uyuni concept —
    confirmed by research that Uyuni has no first-class "environment" or
    "release" object tying system groups, activation keys, CLM environments,
    and tags together; the only REAL native links found are
    activation-key<->system-group (activationkey.addServerGroups,
    spacecmd-native as activationkey_addgroups/listgroups/removegroups) and
    activation-key<->CLM-environment (indirect, via base-channel selection —
    no direct field). "Releases" specifically: confirmed NO release.*
    namespace exists anywhere in Uyuni's API — the CLM environment chain
    already built above (ensure_content_projects) IS the real mechanism,
    Uyuni just never calls it that. System groups (ensure_system_group and
    friends) are spacecmd-native via the systemgroup namespace
    (group_create/group_addsystems/group_listsystems/etc., confirmed by
    reading spacecmd/src/spacecmd/group.py directly). "Tags" have no
    first-class object either (confirmed: no tag.*/system.tag* namespace
    anywhere in the full API index) — the two real mechanisms are group
    membership (already covered) and system.custominfo key/value pairs,
    confirmed spacecmd-native via custominfo_createkey (org-level key
    definition, required before any value can be set) plus
    system_addcustomvalue (per-system value — this one lives in system.py,
    not custominfo.py, despite being conceptually the same feature).
    system_addcustomvalue is treated as safely upsert-able without a
    pre-check: system_updatecustomvalue is documented as a literal alias of
    the same call, implying setCustomValues itself doesn't distinguish
    create-vs-update — an inference, not independently confirmed against a
    live server. Multiple named activation keys (one per environment) are
    supported via a new ensure_activation_keys() list-orchestrator reusing
    ensure_activation_key/ensure_appstreams/ensure_activation_key_packages
    verbatim per list entry — the exact same reuse trick ensure_orgs()
    already uses for org-scoped keys, just applied to a plain list instead
    of one-key-per-org. ensure_activation_key_groups() generalizes the
    package-assignment pattern (genuinely idempotent, called
    unconditionally) to system-group linkage too, alongside
    ensure_activation_key's own pre-existing creation-time-only 'groups'
    follow-up (same field, harmless to have both). Patching schedules:
    confirmed THREE distinct real mechanisms exist —
    recurring.highstate/recurring.custom (cron-based, targets a group
    directly by NUMERIC id), maintenance.* (a gate on already-scheduled
    actions, not a scheduler itself, and only assignable to system IDs, not
    groups, at the API level), and systemgroup.scheduleApplyErrataToActive
    (one-shot "patch this group now"). Only the first is implemented here
    (ensure_recurring_schedule via the generic 'api' passthrough — no
    recurring.py exists in spacecmd's source tree); maintenance.* and
    scheduleApplyErrataToActive are DELIBERATELY DEFERRED — the former
    needs group-to-system-ID resolution plus ical calendar handling, the
    latter needs an unconfirmed errata-ID format, and stacking more
    unverified specifics on top of an already-multi-part feature wasn't
    worth it this round. group_id_for()'s numeric-id resolution (needed
    because recurring.* takes an id, not a name, unlike systemgroup.* which
    takes names throughout) is a heuristic regex against
    'group_details'' human-readable output, whose exact display format
    wasn't independently confirmed — an explicit 'group_id' override in the
    JSON is always available as a fallback, same precedent as Ansible
    integration's control_node_id. Recurring-action idempotency (does
    creating the same schedule twice fault, upsert, or duplicate?) was
    never confirmed by research — no list/exists method was found for
    recurring actions — so, like Ansible/CLM/SCAP scheduling,
    ensure_recurring_schedule is deliberately NOT wired into the automatic
    install flow; see the install scripts' --run-recurring-schedules flag
    (run_environment_schedules) instead. Everything else in this feature
    (system groups, activation-key/group linking, custom-info tags) IS
    idempotent and automatic, same as the rest of this module.
  - Client registration (ensure_client_registered and friends, added
    2026-08-28): for install_client_registration.py, a NEW VM-level addon
    (not install_smlm.py/install_uyuni.py, which install the SERVER) that
    registers some OTHER host as a Salt client of an existing Uyuni/SMLM
    server. Confirmed via live doc research (uyuni-project.org and
    documentation.suse.com/multi-linux-manager 5.1/5.2, 2026-08-28): the
    registration mechanism itself is identical across Uyuni and every SMLM
    version — `ACTIVATION_KEYS="<key>" curl -Sks
    https://<server>/pub/bootstrap/bootstrap.sh | /bin/bash` on the client,
    with REACTIVATION_KEY as the only other commonly-used override. Bootstrap
    alone is NOT sufficient: the minion's key lands in a "pending" state and
    is never auto-accepted by that flow (confirmed — auto-accept exists only
    via a separate, server-side autosign_grains file this project doesn't
    set up) — actually registering the client needs saltkey.accept, and
    there is no native spacecmd subcommand for the saltkey namespace at all
    (confirmed against the spacecmd command reference index — activationkey,
    system, group, etc. all appear there, saltkey does not), so this is
    reached the same way as Ansible/RBAC/CLM above: the generic 'api'
    passthrough, calling saltkey.pendingList/acceptedList/accept directly.
    Minion ID is assumed to equal the client's own FQDN (this project's
    convention for every VM already) — Salt's default when no minion_id
    file is pre-seeded, not independently verified against every possible
    base image. The "ensure the server has what registration needs"
    half of the TODO needed zero new code: ensure_activation_key/
    ensure_channels_synced (existing) work unchanged against a new
    client_registration_* field prefix, exactly like every other
    prefix-parameterized function in this module. NOT live-tested.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import hashlib
import json
import re
import shlex
import time
from datetime import datetime, timezone

from lab_creation import ssh_run, die, warn


def _run(hostname, exec_prefix, remote_cmd, **kwargs):
    """
    Run `remote_cmd` on the server reached via `exec_prefix`, over SSH to
    `hostname`. exec_prefix is one of two shapes, and they are NOT
    interchangeable string prefixes:

    - kubectl (SMLM): "kubectl exec -n {ns} deploy/uyuni -c uyuni --" — `--`
      is kubectl's real "rest is a verbatim argv for the container" marker,
      so flat concatenation (exec_prefix + " " + remote_cmd, then the whole
      thing shipped as ONE ssh argv element) works correctly: the remote
      login shell's own word-splitting of the combined string reconstructs
      exactly the argv kubectl passes through.

    - mgrctl (Uyuni): confirmed live (2026-08-28, disposable VM on
      nuc6.mydemo.lab) that `mgrctl exec` does NOT work this way despite
      this module having assumed "mgrctl exec --" was an equivalent
      ---terminated argv prefix since it was first introduced. mgrctl's own
      usage is `mgrctl exec '[command-to-run --with-args]'` — the ENTIRE
      remote command as ONE quoted string argument. Flat concatenation
      silently corrupts any remote_cmd with more than one shell token: the
      outer ssh-invoked login shell strips remote_cmd's own quoting before
      mgrctl ever sees argv, so e.g. `sh -c 'a && b'` arrives at mgrctl as
      five separate arguments (sh, -c, a, &&, b) instead of the two (sh,
      -c, "a && b") mgrctl's single-string design expects — `sh -c`'s
      script becomes just the bare first word ("a"), and "&& b" ends up
      running as a SEPARATE command on the remote host's own shell,
      outside the container entirely. Confirmed fix: re-quote the whole
      remote_cmd as mgrctl's one true argument (`mgrctl exec '<remote_cmd>'`)
      instead of relying on the outer shell's own word-splitting to
      reconstruct it.

    A second, separate confirmed-live bug (2026-08-28): NEITHER `mgrctl
    exec` NOR `kubectl exec` forward stdin to the container by default —
    each needs its own explicit flag (`-i`/`--interactive` for mgrctl,
    `-i`/`--stdin` for kubectl). Every caller here that pipes `input_text`
    — starting with ensure_spacecmd_config itself, writing spacecmd's own
    ~/.spacecmd/config — was silently writing into a container process that
    was never actually reading its stdin, producing a 0-byte file instead
    of a real error (confirmed live: found ~/.spacecmd/config sitting at
    0 bytes on a freshly-installed server). This went unnoticed through
    every 2026-08-27 feature increment because manual same-session
    `spacecmd -u/-p` testing during development left a valid CACHED SESSION
    behind that masked the missing config file entirely on servers tested
    that way — it only surfaced once a feature (config channels) ran
    against a server that had never been manually spacecmd-logged-into by
    hand first, and spacecmd fell back to an interactive password prompt
    with no TTY to answer it. `_run()` now adds the needed stdin flag
    automatically whenever `input_text` is given, for either exec_prefix
    shape — every caller is fixed at once, same as the quoting fix above.
    """
    prefix = exec_prefix.strip()
    needs_stdin = bool(kwargs.get("input_text"))
    if prefix.startswith("mgrctl exec"):
        stdin_flag = "-i " if needs_stdin else ""
        full_cmd = "mgrctl exec {}{}".format(stdin_flag, shlex.quote(remote_cmd))
    else:
        if needs_stdin and prefix.startswith("kubectl exec") and " -i " not in " {} ".format(prefix):
            prefix = prefix.replace("kubectl exec ", "kubectl exec -i ", 1)
        full_cmd = "{} {}".format(prefix, remote_cmd)
    return ssh_run(hostname, full_cmd, **kwargs)


def _spacecmd(hostname, exec_prefix, args):
    return _run(hostname, exec_prefix, "spacecmd -- {}".format(args), check=False, capture=True)


def ensure_spacecmd_config(hostname, exec_prefix, username, password):
    """
    Writes ~/.spacecmd/config (mode 700 dir / 600 file) inside the server
    container/host so every subsequent spacecmd call authenticates without a
    prompt and without a password ever appearing in argv/`ps` output — see
    documentation.suse.com/multi-linux-manager/5.1/docs/reference/spacecmd/configuring-spacecmd.html
    (verified 2026-08-27). Idempotent: overwrites unconditionally on every
    call, so a changed admin password never leaves a stale cached credential.

    `server` is always written as "localhost", never the caller's externally
    routable FQDN — confirmed live (2026-08-28) as a real bug: every one of
    ensure_config_channels/ensure_org*/ensure_access_groups/
    ensure_content_projects/ensure_activation_key/ensure_environments
    previously ran with `server=<the VM's own FQDN>` written into this file
    (both install_uyuni.py's mgrctl-exec path and install_smlm.py's
    kubectl-exec path passed their own hostname/smlm_fqdn straight through),
    and every one of those feature calls failed with "Failed to connect to
    http://<fqdn>/rpc/api" — `exec_prefix` always drops the caller INSIDE the
    very container/pod running the Uyuni server itself (that's what mgrctl
    exec / kubectl exec do), and from in there, connecting back out to the
    host's own externally-routed hostname/IP over HTTP does not work (no
    hairpin NAT back through the container network to itself — confirmed via
    `curl http://<fqdn>/rpc/api` returning connection-failed from inside the
    container while `curl http://localhost/rpc/api` succeeded immediately).
    Since spacecmd always runs alongside the server it's configuring here,
    "localhost" is the only value that was ever going to work, for either
    deployment shape — there was no legitimate use for a caller-supplied
    value in the first place.
    """
    config_text = "[spacecmd]\nserver=localhost\nusername={}\npassword={}\n".format(username, password)
    cmd = ("sh -c 'mkdir -p ~/.spacecmd && chmod 700 ~/.spacecmd && "
           "cat > ~/.spacecmd/config && chmod 600 ~/.spacecmd/config'")
    r = _run(hostname, exec_prefix, cmd, input_text=config_text, check=False)
    if r.returncode != 0:
        die("could not write spacecmd credentials on {}".format(hostname))


def activation_key_exists(hostname, exec_prefix, key_name):
    """
    Whether `key_name` already appears in `spacecmd activationkey_list`'s
    output. Deliberately not using activationkey_details' exit code —
    undocumented behavior on a missing key per live doc research
    (2026-08-27); grepping the list output is the only behavior actually
    confirmed from a source.
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_list")
    return key_name in (r.stdout or "")


def resolve_activation_key_name(hostname, exec_prefix, key_name):
    """
    Resolves a caller-given activation key name to the name Uyuni actually
    stored it under. Confirmed live 2026-08-28: activationkey_create's -n
    flag does NOT use the given name verbatim — Uyuni always auto-prepends
    the current org's numeric id (e.g. "-n dev-key" is stored as
    "1-dev-key"), and this happens even when the given name already LOOKED
    pre-prefixed ("-n 1-dev-key" was stored as "1-1-dev-key", not
    "1-dev-key"). activation_key_exists()'s substring-match check tolerates
    this silently (the given name is always a substring of the real one),
    so key creation/existence checks never surfaced this — but every
    EXACT-match follow-up command (activationkey_addgroups,
    activationkey_addpackages, activationkey.addAppStreams, etc.) needs the
    real name, and fails outright ("Activation Key [...] Not Found!")
    against the caller's original, un-resolved value. Matches the first
    `activationkey_list` line that IS `key_name` or ends with
    "-" + key_name; falls back to `key_name` unchanged if no line matches
    (e.g. called before the key exists at all — the caller's own
    activation_key_exists()/creation-error handling covers that case).
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_list")
    lines = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
    if key_name in lines:
        return key_name
    for line in lines:
        if line.endswith("-" + key_name):
            return line
    return key_name


def ensure_activation_key(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently create the activation key described by
    <prefix>_activation_key* config keys (prefix is "smlm" or "uyuni"), then
    apply whatever follow-up commands the optional fields need. Per live doc
    research (2026-08-27): spacecmd's activationkey_create only accepts
    name/description/base-channel/universal-default/entitlements at creation
    time — child channels, config channels/deployment, groups, and contact
    method are each a separate activationkey_* call, applied here in that
    order. No-op if <prefix>_activation_key isn't set in cfg. Requires
    ensure_spacecmd_config() to have been called first. NOT live-tested.
    """
    def k(suffix):
        return cfg.get("{}_activation_key{}".format(prefix, suffix))

    key_name = k("")
    if not key_name:
        return

    if activation_key_exists(hostname, exec_prefix, key_name):
        print("  Activation key '{}' already exists — leaving it alone".format(key_name))
        return

    base_channel = k("_base_channel")
    if not base_channel:
        die("{p}_activation_key_base_channel is required to create activation key '{k}'".format(
            p=prefix, k=key_name))

    desc = k("_desc") or key_name
    create_args = "activationkey_create -n {n} -d {d} -b {b}".format(
        n=shlex.quote(key_name), d=shlex.quote(desc), b=shlex.quote(base_channel))
    if (k("_universal_default") or "false") == "true":
        create_args += " -u"
    entitlements = k("_entitlements")
    if entitlements:
        create_args += " -e {}".format(shlex.quote(entitlements))

    r = _spacecmd(hostname, exec_prefix, create_args)
    if r.returncode != 0:
        die("could not create activation key '{}': {}".format(key_name, (r.stderr or r.stdout or "").strip()))
    print("  Created activation key '{}'".format(key_name))

    # Uyuni auto-prepends the org id to whatever name was just given (e.g.
    # "1-dev-key" -> stored as "1-1-dev-key") — resolve to the real name
    # now so every follow-up command below (which needs an exact match,
    # unlike the substring-tolerant existence check above) targets the key
    # that actually exists. See resolve_activation_key_name()'s docstring.
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    child_channels = (k("_child_channels") or "").split()
    if child_channels:
        # Confirmed live 2026-09-14: this genuinely fails ("Invalid channel")
        # whenever a listed child channel isn't actually on the server yet —
        # e.g. the "managertools-*" channels that provide venv-salt-minion,
        # easy to reference here without ever having added them via
        # mgr-sync (they're not implied by their own base product channel).
        # _spacecmd()'s own return code used to be silently discarded, so
        # this failure never surfaced anywhere — activation keys looked
        # created and fine, but clients bootstrapped against them got the
        # wrong (unlinked) channel set with no visible error at all.
        r = _spacecmd(hostname, exec_prefix, "activationkey_addchildchannels {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(c) for c in child_channels)))
        if r.returncode != 0:
            warn("could not link child channels ({}) to activation key '{}' — check they're "
                 "actually synced on the server (`mgr-sync add channels`), not just referenced "
                 "in {}_activation_key_child_channels: {}".format(
                     ", ".join(child_channels), key_name, prefix,
                     (r.stderr or r.stdout or "").strip()))

    config_channels = (k("_config_channels") or "").split()
    if config_channels:
        _spacecmd(hostname, exec_prefix, "activationkey_addconfigchannels {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(c) for c in config_channels)))

    if (k("_enable_config_deployment") or "false") == "true":
        _spacecmd(hostname, exec_prefix, "activationkey_enableconfigdeployment {}".format(shlex.quote(key_name)))

    groups = (k("_groups") or "").split()
    if groups:
        _spacecmd(hostname, exec_prefix, "activationkey_addgroups {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(g) for g in groups)))

    contact_method = k("_contact_method")
    if contact_method:
        _spacecmd(hostname, exec_prefix, "activationkey_setcontactmethod {} {}".format(
            shlex.quote(key_name), shlex.quote(contact_method)))


def ensure_appstreams(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently enable each "module:stream" pair listed in
    <prefix>_activation_key_appstreams (space-separated) on
    <prefix>_activation_key, via spacecmd's generic 'api' passthrough calling
    activationkey.addAppStreams (spacecmd has no dedicated subcommand for
    this — see module docstring). No-op if either the key or the appstreams
    field is unset. Unlike ensure_activation_key's other follow-ups, this is
    called unconditionally (not only at key-creation time): since there's no
    list API to pre-check against, idempotency comes from treating the
    server's own "already exists in the activation key" fault as success,
    which makes it safe to call on an already-existing key too — e.g. to add
    AppStreams on a later run without recreating the key. NOT live-tested.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = cfg.get("{}_activation_key_appstreams".format(prefix)) or ""
    if not key_name or not spec:
        return
    pairs = spec.split()
    for pair in pairs:
        if ":" not in pair:
            die("invalid {}_activation_key_appstreams entry '{}': expected 'module:stream'".format(prefix, pair))
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    for pair in pairs:
        module, stream = pair.split(":", 1)
        r = _api_call(hostname, exec_prefix, "activationkey.addAppStreams",
                      [key_name, [{"module": module, "stream": stream}]])
        out = "{}\n{}".format(r.stdout or "", r.stderr or "").lower()
        if r.returncode == 0:
            print("  AppStream '{}' enabled on '{}'".format(pair, key_name))
        elif "already exists in the activation key" in out:
            print("  AppStream '{}' already enabled on '{}' — leaving it alone".format(pair, key_name))
        else:
            die("could not add appstream '{}' to activation key '{}': {}".format(
                pair, key_name, (r.stderr or r.stdout or "").strip()))


def activation_key_packages(hostname, exec_prefix, key_name):
    """
    Returns the set of package names currently on activation key
    `key_name`, via spacecmd's native activationkey_listpackages (wraps
    activationkey.getDetails' packages array). Name-only — spacecmd's own
    listing/adding commands don't surface the underlying API's optional
    per-package 'arch' field, so this module doesn't attempt arch-qualified
    package matching either (see ensure_activation_key_packages).
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_listpackages {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_packages(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently ensures every package name listed in
    <prefix>_activation_key_packages (space-separated) is present on
    <prefix>_activation_key, via spacecmd's native activationkey_addpackages
    (wraps activationkey.addPackages — confirmed a real, spacecmd-native
    mechanism, correcting an earlier research note in this project that
    conflated it with the unrelated system.createPackageProfile/
    comparePackageProfile mechanism, which snapshots an already-REGISTERED
    system's installed packages for later comparison and has nothing to do
    with activation keys). Unlike ensure_activation_key's OTHER follow-ups
    (child channels, config channels, groups — creation-time only), this is
    called unconditionally, like ensure_appstreams, since
    activationkey_listpackages gives a reliable way to check what's already
    there — diffs against the current list and only adds what's missing, so
    packages can be added to an already-existing key on a later run too.
    Name-only, no arch-qualification (see activation_key_packages). No-op
    if either the key or the packages field is unset. NOT live-tested.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_packages".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_packages(hostname, exec_prefix, key_name)
    missing = [p for p in spec if p not in existing]
    if not missing:
        print("  Activation key '{}' already has all requested packages — leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addpackages {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(p) for p in missing)))
    if r.returncode != 0:
        die("could not add packages to activation key '{}': {}".format(
            key_name, (r.stderr or r.stdout or "").strip()))
    print("  Added {} package(s) to activation key '{}': {}".format(len(missing), key_name, ", ".join(missing)))


def ensure_channels_synced(hostname, exec_prefix, channels):
    """
    For each channel label in `channels`, trigger 'mgr-sync add channel
    <label>' if it isn't already listed in 'spacecmd softwarechannel_list' —
    one channel per invocation (see module docstring for why not the plural
    form). No-op if `channels` is empty. Requires ensure_spacecmd_config() to
    have been called first (mgr-sync itself needs SCC credentials already
    configured at install time, not spacecmd's — this only affects the
    softwarechannel_list existence check). NOT live-tested.
    """
    if not channels:
        return
    existing = _spacecmd(hostname, exec_prefix, "softwarechannel_list").stdout or ""
    for ch in channels:
        if ch in existing:
            continue
        print("  Syncing channel '{}' (not yet present) …".format(ch))
        r = _run(hostname, exec_prefix, "mgr-sync add channel {}".format(shlex.quote(ch)), check=False)
        if r.returncode != 0:
            die("could not sync channel '{}'".format(ch))


def _stage_remote_file(hostname, exec_prefix, remote_path, content):
    """Writes `content` to `remote_path` (inside exec_prefix's target) via
    stdin — avoids ever embedding file content as a shell-quoted argv
    string. Caller is responsible for removing `remote_path` afterwards."""
    r = _run(hostname, exec_prefix, "cat > {}".format(shlex.quote(remote_path)), input_text=content, check=False)
    return r.returncode == 0


def config_channel_exists(hostname, exec_prefix, label):
    """
    Whether `label` already appears in `spacecmd configchannel_list`'s
    output. No dedicated existence-check subcommand exists (the underlying
    configchannel.channelExists XML-RPC method has no spacecmd wrapper) —
    same grep-the-list idiom as activation_key_exists.
    """
    r = _spacecmd(hostname, exec_prefix, "configchannel_list")
    return label in (r.stdout or "")


def ensure_config_channel_exists(hostname, exec_prefix, label, name, desc, chan_type="normal"):
    """
    Idempotently create a config channel. NOTE: -t/--type ('normal' or
    'state') is implemented in spacecmd's source but undocumented on both
    the Uyuni and SMLM 5.1 doc pages as of the 2026-08-27 research behind
    this module — verify with 'spacecmd help configchannel_create' on the
    actual target before relying on this in production.
    """
    if config_channel_exists(hostname, exec_prefix, label):
        print("  Config channel '{}' already exists — leaving it alone".format(label))
        return
    r = _spacecmd(hostname, exec_prefix, "configchannel_create -n {n} -l {l} -d {d} -t {t}".format(
        n=shlex.quote(name), l=shlex.quote(label), d=shlex.quote(desc), t=shlex.quote(chan_type)))
    if r.returncode != 0:
        die("could not create config channel '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created config channel '{}' (type: {})".format(label, chan_type))


def ensure_config_file(hostname, exec_prefix, label, path, content,
                        owner=None, group=None, mode=None, binary=False):
    """
    Idempotently ensure `path` exists with `content` inside config channel
    `label`, via configchannel_addfile (createOrUpdatePath under the hood,
    so "add" and "update" are the same call). Content is staged to a remote
    temp file first since spacecmd's -f flag reads a local file path, not
    inline text/stdin. Idempotency is a heuristic: skips the push if a
    locally-computed sha256 of `content` already appears in
    configchannel_filedetails' output for that path — there's no dedicated
    single-file diff command. NOT live-tested.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    details = _spacecmd(hostname, exec_prefix, "configchannel_filedetails {} {}".format(
        shlex.quote(label), shlex.quote(path)))
    if details.returncode == 0 and digest in (details.stdout or ""):
        print("  Config file '{}' on '{}' already up to date — skipping".format(path, label))
        return

    remote_tmp = "/tmp/.lab-cfgfile-{}".format(hashlib.sha1("{}:{}".format(label, path).encode()).hexdigest()[:12])
    if not _stage_remote_file(hostname, exec_prefix, remote_tmp, content):
        die("could not stage config file content for '{}' on channel '{}'".format(path, label))

    add_args = "configchannel_addfile -c {c} -p {p} -f {f} -y".format(
        c=shlex.quote(label), p=shlex.quote(path), f=shlex.quote(remote_tmp))
    if owner:
        add_args += " -o {}".format(shlex.quote(owner))
    if group:
        add_args += " -g {}".format(shlex.quote(group))
    if mode:
        add_args += " -m {}".format(shlex.quote(mode))
    if binary:
        add_args += " -b"

    r = _spacecmd(hostname, exec_prefix, add_args)
    _run(hostname, exec_prefix, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
    if r.returncode != 0:
        die("could not push config file '{}' to channel '{}': {}".format(
            path, label, (r.stderr or r.stdout or "").strip()))
    print("  Pushed config file '{}' to channel '{}'".format(path, label))


def ensure_init_sls(hostname, exec_prefix, label, content):
    """
    Same idea as ensure_config_file but for a state channel's init.sls,
    which spacecmd manages via a dedicated command
    (configchannel_updateinitsls) rather than configchannel_addfile — its
    path is always /init.sls server-side. NOT live-tested.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    details = _spacecmd(hostname, exec_prefix, "configchannel_filedetails {} /init.sls".format(shlex.quote(label)))
    if details.returncode == 0 and digest in (details.stdout or ""):
        print("  init.sls on '{}' already up to date — skipping".format(label))
        return

    remote_tmp = "/tmp/.lab-cfgfile-{}-initsls".format(hashlib.sha1(label.encode()).hexdigest()[:12])
    if not _stage_remote_file(hostname, exec_prefix, remote_tmp, content):
        die("could not stage init.sls content for state channel '{}'".format(label))

    r = _spacecmd(hostname, exec_prefix, "configchannel_updateinitsls -c {} -f {} -y".format(
        shlex.quote(label), shlex.quote(remote_tmp)))
    _run(hostname, exec_prefix, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
    if r.returncode != 0:
        die("could not push init.sls to state channel '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Pushed init.sls to state channel '{}'".format(label))


def ensure_config_channels(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_config_channels: a list of
    {label, name, description, type, files: [{path, content, owner, group,
    mode, binary}], init_sls} dicts. `type` defaults to "normal"; "state"
    channels use `init_sls` instead of (or alongside) `files` for content
    besides the auxiliary files under them. No-op if the field is unset or
    empty. Requires ensure_spacecmd_config() to have been called first.
    NOT live-tested.
    """
    channels = cfg.get("{}_config_channels".format(prefix)) or []
    for chan in channels:
        label = chan.get("label")
        if not label:
            die("{}_config_channels: an entry is missing required 'label'".format(prefix))
        name = chan.get("name") or label
        desc = chan.get("description") or name
        chan_type = chan.get("type") or "normal"

        ensure_config_channel_exists(hostname, exec_prefix, label, name, desc, chan_type)

        if chan_type == "state" and chan.get("init_sls"):
            ensure_init_sls(hostname, exec_prefix, label, chan["init_sls"])

        for f in chan.get("files") or []:
            path = f.get("path")
            if not path:
                die("config channel '{}': a file entry is missing required 'path'".format(label))
            content = f.get("content")
            if content is None:
                die("config channel '{}': file '{}' is missing required 'content'".format(label, path))
            ensure_config_file(hostname, exec_prefix, label, path, content,
                                owner=f.get("owner"), group=f.get("group"), mode=f.get("mode"),
                                binary=bool(f.get("binary")))


def org_exists(hostname, exec_prefix, org_name):
    """
    Whether `org_name` already appears as an exact line in `spacecmd
    org_list`'s output (one org name per line, no header, per source). Exact
    match rather than the substring check used elsewhere in this module
    (activation_key_exists, config_channel_exists), since org names are
    typically short and more collision-prone (e.g. "Lab" vs "Lab2").
    """
    r = _spacecmd(hostname, exec_prefix, "org_list")
    return org_name in [line.strip() for line in (r.stdout or "").splitlines()]


def ensure_org(hostname, exec_prefix, org):
    """
    Idempotently create the organization described by one entry of
    <prefix>_orgs. Must be called under the DEFAULT admin's already-
    authenticated spacecmd session (via ensure_spacecmd_config) — org
    creation itself is not org-scoped; it's the org.create API call made by
    whatever session is already logged in. NOT live-tested.
    """
    name = org.get("name")
    if not name:
        die("orgs: an entry is missing required 'name'")
    if org_exists(hostname, exec_prefix, name):
        print("  Org '{}' already exists — leaving it alone".format(name))
        return

    admin_user = org.get("admin_user")
    admin_pass = org.get("admin_pass")
    admin_email = org.get("admin_email")
    if not (admin_user and admin_pass and admin_email):
        die("org '{}': admin_user, admin_pass and admin_email are all required to create it".format(name))
    first = org.get("admin_first_name") or admin_user
    last = org.get("admin_last_name") or name

    cmd = "org_create -n {n} -u {u} -f {f} -l {l} -e {e} -p {p}".format(
        n=shlex.quote(name), u=shlex.quote(admin_user), f=shlex.quote(first),
        l=shlex.quote(last), e=shlex.quote(admin_email), p=shlex.quote(admin_pass))
    if org.get("prefix"):
        cmd += " -P {}".format(shlex.quote(org["prefix"]))
    if org.get("pam"):
        cmd += " --pam"

    r = _spacecmd(hostname, exec_prefix, cmd)
    if r.returncode != 0:
        die("could not create org '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created org '{}' (admin: {})".format(name, admin_user))


def ensure_org_trust(hostname, exec_prefix, org_a, org_b):
    """
    Idempotently establish trust between org_a and org_b via org_addtrust,
    skipping if org_listtrusts already lists org_b for org_a. NOTE: trust
    alone does not make a channel visible across orgs — the channel's
    owning org must also mark it shared via ensure_channel_sharing(). Must
    be called under a session with rights to both orgs (the default admin,
    typically) — see module docstring for what's confirmed vs. inferred
    about trust's bidirectionality. NOT live-tested.
    """
    existing = _spacecmd(hostname, exec_prefix, "org_listtrusts {}".format(shlex.quote(org_a))).stdout or ""
    if org_b in existing:
        print("  Trust {} <-> {} already exists — leaving it alone".format(org_a, org_b))
        return
    r = _spacecmd(hostname, exec_prefix, "org_addtrust {} {}".format(shlex.quote(org_a), shlex.quote(org_b)))
    if r.returncode != 0:
        die("could not add trust between '{}' and '{}': {}".format(org_a, org_b, (r.stderr or r.stdout or "").strip()))
    print("  Trust established: {} <-> {}".format(org_a, org_b))


def ensure_channel_sharing(hostname, exec_prefix, channel_label, access="protected"):
    """
    Marks a software channel's org-sharing level via the raw
    channel.access.setOrgSharing XML-RPC method — no spacecmd subcommand
    exists for this (confirmed absent from source, 2026-08-27 research).
    access must be "public", "private", or "protected" ("protected" =
    visible to trusted orgs only). Idempotent via
    channel.access.getOrgSharing, though its exact output shape wasn't
    confirmed from docs — this is a substring-match heuristic, same spirit
    as ensure_appstreams' fault-string check. Must be called under a
    session belonging to the channel's OWNING org. NOT live-tested.
    """
    if access not in ("public", "private", "protected"):
        die("invalid channel access level '{}': expected public, private, or protected".format(access))

    current = _api_call(hostname, exec_prefix, "channel.access.getOrgSharing", [channel_label])
    if current.returncode == 0 and access in (current.stdout or ""):
        print("  Channel '{}' sharing already '{}' — leaving it alone".format(channel_label, access))
        return

    r = _api_call(hostname, exec_prefix, "channel.access.setOrgSharing", [channel_label, access])
    if r.returncode != 0:
        die("could not set channel '{}' sharing to '{}': {}".format(
            channel_label, access, (r.stderr or r.stdout or "").strip()))
    print("  Channel '{}' sharing set to '{}'".format(channel_label, access))


def ensure_orgs(hostname, exec_prefix, cfg, prefix, default_admin_user, default_admin_pass):
    """
    Orchestrates <prefix>_orgs: a list of org dicts, each carrying
    {name, admin_user, admin_pass, admin_email, admin_first_name,
    admin_last_name, prefix, pam, trust_with: [...], share_channels: [...],
    share_channels_access}, PLUS whatever <prefix>_activation_key*/
    <prefix>_config_channels/<prefix>_access_groups/<prefix>_system_groups/
    <prefix>_users keys that org itself needs — reusing the exact same
    field names as the top-level config, since once this function
    re-authenticates as that org's own admin, ensure_activation_key/
    ensure_config_channels/ensure_appstreams/ensure_access_groups/
    ensure_system_groups/ensure_users work completely unchanged
    (org-scoping is entirely a function of which session is active — see
    module docstring). For each
    org, in list order (so a later org can trust_with an earlier one):
      1. re-authenticate as the DEFAULT admin, then create the org if it
         doesn't exist yet
      2. establish any requested trust_with relationships (still under the
         default admin)
      3. re-authenticate as THIS org's own admin and run its
         share_channels/config-channels/activation-key/appstreams/
         access-groups provisioning, scoped automatically to this org
    Restores the default admin session before returning, so nothing
    downstream is left authenticated as the last org processed. No-op if
    <prefix>_orgs is unset or empty. If an org entry has no admin_user/
    admin_pass (e.g. it already exists and this run only needs its
    trust_with/share_channels applied, not its own activation
    key/config channels re-provisioned), step 3 is skipped entirely for
    that org. NOT live-tested.
    """
    orgs = cfg.get("{}_orgs".format(prefix)) or []
    if not orgs:
        return

    for org in orgs:
        name = org.get("name")
        if not name:
            die("{}_orgs: an entry is missing required 'name'".format(prefix))

        ensure_spacecmd_config(hostname, exec_prefix, default_admin_user, default_admin_pass)
        ensure_org(hostname, exec_prefix, org)

        for other in org.get("trust_with") or []:
            ensure_org_trust(hostname, exec_prefix, name, other)

        admin_user = org.get("admin_user")
        admin_pass = org.get("admin_pass")
        if not (admin_user and admin_pass):
            continue
        ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)

        share_access = org.get("share_channels_access") or "protected"
        for ch in org.get("share_channels") or []:
            ensure_channel_sharing(hostname, exec_prefix, ch, share_access)

        ensure_config_channels(hostname, exec_prefix, org, prefix)
        # System groups BEFORE any activation key — same reorder, same
        # reason, as the top-level orchestration in install_smlm.py/
        # install_uyuni.py: ensure_activation_key()'s own group-linking dies
        # if the named group doesn't exist yet server-side.
        ensure_system_groups(hostname, exec_prefix, org, prefix)
        ensure_activation_key(hostname, exec_prefix, org, prefix)
        ensure_appstreams(hostname, exec_prefix, org, prefix)
        ensure_activation_key_packages(hostname, exec_prefix, org, prefix)
        ensure_users(hostname, exec_prefix, org, prefix)
        ensure_access_groups(hostname, exec_prefix, org, prefix)

    ensure_spacecmd_config(hostname, exec_prefix, default_admin_user, default_admin_pass)


def access_group_exists(hostname, exec_prefix, label):
    """
    Whether `label` appears in access.listRoles' raw output. There is no
    spacecmd subcommand for the 'access' namespace at all (confirmed absent
    from spacecmd's source tree, 2026-08-27 research) — every access_*
    operation in this module goes through the generic 'api' passthrough.
    Substring match, same heuristic as the AppStream/channel-sharing checks
    above, since the passthrough's raw print format for a list of
    AccessGroup structs wasn't confirmed from docs.
    """
    r = _api_call(hostname, exec_prefix, "access.listRoles", [])
    return label in (r.stdout or "")


def ensure_access_group(hostname, exec_prefix, label, description, permissions_from=None):
    """
    Idempotently create a custom RBAC access group ("User Access Group") via
    access.createRole. `permissions_from` (optional list of existing role
    labels to copy permissions from) matches createRole's optional third
    argument. NOT live-tested.
    """
    if access_group_exists(hostname, exec_prefix, label):
        print("  Access group '{}' already exists — leaving it alone".format(label))
        return
    args = [label, description]
    if permissions_from:
        args.append(list(permissions_from))
    r = _api_call(hostname, exec_prefix, "access.createRole", args)
    if r.returncode != 0:
        die("could not create access group '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created access group '{}'".format(label))


def access_group_has_namespace(hostname, exec_prefix, label, namespace):
    """Whether `namespace` already appears in access.listPermissions(label)'s
    raw output — same substring-match heuristic as access_group_exists."""
    r = _api_call(hostname, exec_prefix, "access.listPermissions", [label])
    return r.returncode == 0 and namespace in (r.stdout or "")


def ensure_access_group_permissions(hostname, exec_prefix, label, permissions):
    """
    Grants each not-yet-present namespace in `permissions` (a list of
    {"namespace": "...", "mode": "R"|"W"}, mode optional) to access group
    `label` via a single access.grantAccess call. Checks
    access_group_has_namespace() first and skips already-granted ones,
    since grantAccess's own idempotency on a repeat call for the same
    namespace wasn't confirmed by research. No-op if every namespace is
    already granted or `permissions` is empty. NOT live-tested.
    """
    to_grant = []
    modes = []
    have_modes = False
    for p in permissions or []:
        namespace = p.get("namespace")
        if not namespace:
            die("access group '{}': a permission entry is missing required 'namespace'".format(label))
        if access_group_has_namespace(hostname, exec_prefix, label, namespace):
            print("  Access group '{}' already has namespace '{}' — leaving it alone".format(label, namespace))
            continue
        to_grant.append(namespace)
        mode = p.get("mode")
        if mode:
            have_modes = True
        modes.append(mode or "R")

    if not to_grant:
        return
    args = [label, to_grant]
    if have_modes:
        args.append(modes)
    r = _api_call(hostname, exec_prefix, "access.grantAccess", args)
    if r.returncode != 0:
        die("could not grant namespace(s) {} to access group '{}': {}".format(
            to_grant, label, (r.stderr or r.stdout or "").strip()))
    print("  Granted {} namespace(s) to access group '{}'".format(len(to_grant), label))


def user_has_role(hostname, exec_prefix, username, role):
    """Whether `role` appears in 'spacecmd user_details USERNAME''s raw
    output (wraps user.listRoles server-side, per source)."""
    r = _spacecmd(hostname, exec_prefix, "user_details {}".format(shlex.quote(username)))
    return r.returncode == 0 and role in (r.stdout or "")


def ensure_user_role(hostname, exec_prefix, username, role):
    """
    Idempotently attach `role` to an ALREADY-EXISTING user via spacecmd's
    native user_addrole. Does not create the user — a missing username is
    reported the same way as any other user_addrole failure (see below).

    CONFIRMED LIVE 2026-09-15 against a real SMLM 5.2 server, contradicting
    this module's own earlier assumption: user.addRole (user_addrole)
    ONLY accepts the fixed/builtin role labels (activation_key_admin,
    channel_admin, config_admin, image_admin, org_admin, regular_user,
    satellite_admin, system_group_admin) — a custom access group's own
    label is REJECTED outright ("Role with the label [X] cannot be
    assigned/revoked from the user"), and this is unconditional: even the
    default satellite_admin session gets the identical rejection, not just
    a less-privileged org admin. The real XML-RPC method for attaching a
    user to a custom Access Group was not found among the reasonable
    candidates probed live (access.setUserAccessGroups/addUserAccessGroup/
    setUsers/grantAccessGroup, user.setAccessGroups/addAssignedRoles — all
    404 "Could not find method"), so ensure_access_groups()'s own `users`
    field is currently UNIMPLEMENTABLE via any spacecmd/XML-RPC call this
    module could locate — warn(), don't die(), so a lab with a mix of
    builtin-role and custom-group user assignments still gets the builtin
    ones applied and every other orchestration step still runs. Attach
    users to a custom access group by hand via the Web UI until the real
    API is found.
    """
    if user_has_role(hostname, exec_prefix, username, role):
        print("  User '{}' already has role '{}' — leaving it alone".format(username, role))
        return
    r = _spacecmd(hostname, exec_prefix, "user_addrole {} {}".format(shlex.quote(username), shlex.quote(role)))
    if r.returncode != 0:
        warn("could not add role '{}' to user '{}' — if '{}' is a custom access group's own "
             "label, this is a known, currently-unresolved API gap (see this function's own "
             "docstring), not a config mistake: {}".format(
                 role, username, role, (r.stderr or r.stdout or "").strip()))
        return
    print("  Added role '{}' to user '{}'".format(role, username))


def user_exists(hostname, exec_prefix, username):
    """
    Whether `username` already appears as an exact line in `spacecmd
    user_list`'s output (one username per line, no header, confirmed live
    2026-09-15 — same shape as org_list). Exact match, same reasoning as
    org_exists.
    """
    r = _spacecmd(hostname, exec_prefix, "user_list")
    return username in [line.strip() for line in (r.stdout or "").splitlines()]


def ensure_user(hostname, exec_prefix, user):
    """
    Idempotently create the user account described by one entry of
    <prefix>_users, via spacecmd's native user_create (confirmed live
    2026-09-15 against a real SMLM 5.2 server — see this module's own
    top-of-file notes on ensure_access_groups for the research correction).
    Builtin roles (fixed labels from `spacecmd user_listavailableroles`:
    activation_key_admin, channel_admin, config_admin, image_admin,
    org_admin, regular_user, satellite_admin, system_group_admin — confirmed
    live against the same server) are applied via `roles`, reusing
    ensure_user_role() so a repeat run never re-grants an already-held role.

    A custom access group's own label is an ordinary role label too once the
    group exists, BUT ensure_users() runs BEFORE ensure_access_groups() at
    every call site in this module (an access group's own `users` list needs
    the account to already exist) — so a custom label put in `roles` here
    would try to attach a role that doesn't exist yet and fail. Grant custom
    labels the other way instead: list the username in that access group's
    own `users` field, which runs in the correct order already. Reserve
    `roles` here for the fixed builtin labels, which have no such ordering
    dependency.
    """
    username = user.get("username")
    if not username:
        die("users: an entry is missing required 'username'")

    if user_exists(hostname, exec_prefix, username):
        print("  User '{}' already exists — leaving it alone".format(username))
    else:
        password = user.get("password")
        first_name = user.get("first_name")
        last_name = user.get("last_name")
        email = user.get("email")
        if not (password and first_name and last_name and email):
            die("user '{}': password, first_name, last_name and email are all required to "
                "create it".format(username))

        cmd = "user_create -u {u} -p {p} -f {f} -l {l} -e {e}".format(
            u=shlex.quote(username), p=shlex.quote(password),
            f=shlex.quote(first_name), l=shlex.quote(last_name), e=shlex.quote(email))
        if user.get("pam"):
            cmd += " --pam"
        r = _spacecmd(hostname, exec_prefix, cmd)
        if r.returncode != 0:
            die("could not create user '{}': {}".format(username, (r.stderr or r.stdout or "").strip()))
        print("  Created user '{}'".format(username))

    for role in user.get("roles") or []:
        ensure_user_role(hostname, exec_prefix, username, role)


def ensure_users(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_users: a list of {username, password, first_name,
    last_name, email, pam: false, roles: [...]} dicts — see ensure_user()
    for per-entry behavior. No-op if <prefix>_users is unset or empty.
    Called both at the top level (default-org users) and per-org from
    ensure_orgs (org-scoped users), same pattern as ensure_access_groups —
    and deliberately BEFORE ensure_access_groups() at each of those call
    sites, since an access group's own `users` list needs the account to
    already exist.
    """
    for user in cfg.get("{}_users".format(prefix)) or []:
        ensure_user(hostname, exec_prefix, user)


def ensure_access_groups(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_access_groups: a list of {label, description,
    permissions_from: [...], permissions: [{namespace, mode}], users: [...]}
    dicts. Each entry: create the access group (or skip if it exists), grant
    its requested namespaces, then TRY to attach it as a role to every
    already-existing username in `users` via ensure_user_role. No-op if
    <prefix>_access_groups is unset or empty. Called both at the top level
    (default-org users) and per-org from ensure_orgs (org-scoped users) —
    the 'access' namespace is reached through the same session-is-org-
    scoping mechanism as everything else in this module.

    Group creation and permission-granting are confirmed live (2026-09-15,
    real SMLM 5.2 server) and work correctly. The `users` attachment step is
    ALSO confirmed live — and confirmed BROKEN: see ensure_user_role()'s own
    docstring for the real, reproducible API rejection this hits for every
    custom label, regardless of caller privilege. That call now warns
    instead of dying, so this orchestrator still finishes (and every other
    <prefix>_access_groups entry, plus every step after it in the caller's
    own orchestration, still runs) even though `users` currently has no
    working effect.
    """
    groups = cfg.get("{}_access_groups".format(prefix)) or []
    for group in groups:
        label = group.get("label")
        if not label:
            die("{}_access_groups: an entry is missing required 'label'".format(prefix))
        description = group.get("description") or label

        ensure_access_group(hostname, exec_prefix, label, description, group.get("permissions_from"))
        ensure_access_group_permissions(hostname, exec_prefix, label, group.get("permissions"))

        for username in group.get("users") or []:
            ensure_user_role(hostname, exec_prefix, username, label)


def ansible_path_exists(hostname, exec_prefix, control_node_id, path):
    """
    Whether `path` already appears in ansible.listAnsiblePaths(control_node_id)'s
    raw output — no dedicated existence check exists (same substring-match
    heuristic used throughout this module wherever the raw print format of
    a struct/list wasn't confirmed from docs).
    """
    r = _api_call(hostname, exec_prefix, "ansible.listAnsiblePaths", [control_node_id])
    return r.returncode == 0 and path in (r.stdout or "")


def ensure_ansible_path(hostname, exec_prefix, control_node_id, path_type, path):
    """
    Idempotently register `path` (a directory on the control node's own
    filesystem — this does NOT create or upload anything there) as an
    ansible.AnsiblePath of `path_type` ("inventory" or "playbook") for
    control-node system `control_node_id`. The control node must already be
    a registered system with the "Ansible Control Node" add-on entitlement
    enabled — this module has no way to enable that itself (see module
    docstring). NOT live-tested.
    """
    if path_type not in ("inventory", "playbook"):
        die("invalid ansible path type '{}': expected 'inventory' or 'playbook'".format(path_type))
    if ansible_path_exists(hostname, exec_prefix, control_node_id, path):
        print("  Ansible {} path '{}' already registered on control node {} — leaving it alone".format(
            path_type, path, control_node_id))
        return
    r = _api_call(hostname, exec_prefix, "ansible.createAnsiblePath",
                  [{"type": path_type, "server_id": control_node_id, "path": path}])
    if r.returncode != 0:
        die("could not register ansible {} path '{}' on control node {}: {}".format(
            path_type, path, control_node_id, (r.stderr or r.stdout or "").strip()))
    print("  Registered ansible {} path '{}' on control node {}".format(path_type, path, control_node_id))


def ensure_ansible_paths(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_ansible_paths: a list of {control_node_id, type,
    path} dicts. Idempotent, safe to call on every run — unlike
    schedule_ansible_playbook below, which is NOT (see module docstring for
    why the two are treated differently). No-op if the field is unset or
    empty. NOT live-tested.
    """
    paths = cfg.get("{}_ansible_paths".format(prefix)) or []
    for p in paths:
        control_node_id = p.get("control_node_id")
        path = p.get("path")
        path_type = p.get("type")
        if control_node_id is None or not path or not path_type:
            die("{}_ansible_paths: an entry is missing required "
                "'control_node_id'/'type'/'path'".format(prefix))
        ensure_ansible_path(hostname, exec_prefix, control_node_id, path_type, path)


def schedule_ansible_playbook(hostname, exec_prefix, control_node_id, playbook_path, inventory_path,
                               earliest=None, action_chain_label="", test_mode=False,
                               extra_vars=None, flush_cache=False):
    """
    Schedules an Ansible playbook run via ansible.schedulePlaybook (no
    spacecmd subcommand exists for the 'ansible' namespace at all — see
    module docstring). `earliest` is an ISO-8601 string (default: the
    current UTC time, i.e. "run as soon as possible") — spacecmd's own
    'api' passthrough argument parser auto-converts a top-level
    ISO-8601-looking string into a real datetime before the XML-RPC call,
    confirmed by reading its source directly; no manual DateTime
    construction is needed here. Returns the scheduled action id (a string)
    on success — pass it to ansible_playbook_status() to check on the run
    later. NOT IDEMPOTENT: each call schedules a brand-new run, so this is
    meant to be invoked once per intended run (see the install scripts'
    --run-ansible-playbooks flag), never as part of the normal automatic
    ensure_* flow. NOT live-tested.
    """
    earliest = earliest or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    args = [playbook_path, inventory_path, control_node_id, earliest, action_chain_label or ""]
    if extra_vars or flush_cache:
        ansible_args = {}
        if extra_vars:
            ansible_args["extraVars"] = extra_vars
        if flush_cache:
            ansible_args["flushCache"] = True
        args.append(bool(test_mode))
        args.append(ansible_args)
    elif test_mode:
        args.append(True)

    r = _api_call(hostname, exec_prefix, "ansible.schedulePlaybook", args)
    if r.returncode != 0:
        die("could not schedule playbook '{}' on control node {}: {}".format(
            playbook_path, control_node_id, (r.stderr or r.stdout or "").strip()))
    action_id = (r.stdout or "").strip()
    print("  Scheduled playbook '{}' on control node {} (inventory: {}) — action id: {}".format(
        playbook_path, control_node_id, inventory_path, action_id))
    return action_id


def ansible_playbook_status(hostname, exec_prefix, action_id):
    """
    Returns (details, output) — the raw text of spacecmd's native (not
    passthrough) schedule_details/schedule_getoutput commands for a
    previously scheduled action id. The 'schedule' namespace, unlike
    'ansible', IS wrapped natively by spacecmd. Read-only; callers decide
    what to do with the text. NOT live-tested.
    """
    details = _spacecmd(hostname, exec_prefix, "schedule_details {}".format(shlex.quote(str(action_id))))
    output = _spacecmd(hostname, exec_prefix, "schedule_getoutput {}".format(shlex.quote(str(action_id))))
    return (details.stdout or ""), (output.stdout or "")


def _api_call(hostname, exec_prefix, method, args):
    """Shared helper: JSON-encodes `args` and runs it through spacecmd's
    generic 'api' passthrough against `method` (e.g.
    "contentmanagement.createProject"). Every ansible.*/access.*/
    contentmanagement.*/saltkey.* call in this module goes through this
    same passthrough, since none of those namespaces have a native spacecmd
    subcommand — see module docstring.

    `args` is always the caller's plain list of positional args beyond the
    (auto-injected) session key — [] for a zero-arg method, [x] for a
    one-arg method, [x, y] for two, etc. Confirmed live (2026-08-28, real
    saltkey.accept(sessionKey, minionId) call): spacecmd's `-A` JSON is NOT
    "the positional args, spread" — it's passed through as ONE value bound
    directly to the method's next parameter. For a one-arg method this
    means the JSON must be that single value on its own (`-A
    "minionId-string"`), NOT a one-element array (`-A '["minionId-string"]'`
    binds the whole list as the arg, which is a different value the server
    then reports as not found — confirmed by the exact "[[value]]"-nested
    error text a wrapped list produces). This is special-cased ONLY for the
    confirmed len(args) == 1 case; 0-arg (confirmed: plain `[]` works,
    e.g. saltkey.pendingList) case is left as-is.

    A second bug, confirmed live 2026-08-28 (channel.access.setOrgSharing,
    a genuine 2-arg call): the command string built here was missing the
    `--` separator `_spacecmd()` already correctly uses before its own
    subcommand — `spacecmd api -A ... method` is rejected outright by
    spacecmd's own argument parser ("unrecognized arguments: -A [...]
    method"), reproduced identically for BOTH a 1-arg and a 2-arg call once
    tested directly; `spacecmd -- api -A ... method` (with `--`) works for
    both. This means every previous "2+-arg calls are genuinely unverified"
    caveat here was moot — the command never had a chance to reach the
    argument-shape question at all before this fix, regardless of arg
    count. Fixed by adding `--` before `api`, matching `_spacecmd()`.
    """
    args_json = json.dumps(args[0] if len(args) == 1 else args)
    return _run(hostname, exec_prefix, "spacecmd -- api -A {} {}".format(shlex.quote(args_json), method),
                check=False, capture=True)


def content_project_exists(hostname, exec_prefix, label):
    """Whether `label` appears in contentmanagement.listProjects' raw
    output. Deliberately not using lookupProject's fault/exit-code behavior
    on a miss — unconfirmed, same caution as activation_key_exists earlier
    in this module."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjects", [])
    return r.returncode == 0 and label in (r.stdout or "")


def ensure_content_project(hostname, exec_prefix, label, name, description):
    """Idempotently create a CLM project via contentmanagement.createProject.
    NOT live-tested."""
    if content_project_exists(hostname, exec_prefix, label):
        print("  Content project '{}' already exists — leaving it alone".format(label))
        return
    r = _api_call(hostname, exec_prefix, "contentmanagement.createProject", [label, name, description])
    if r.returncode != 0:
        die("could not create content project '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created content project '{}'".format(label))


def content_source_exists(hostname, exec_prefix, project_label, source_label):
    """Whether `source_label` appears in
    contentmanagement.listProjectSources(project_label)'s raw output."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjectSources", [project_label])
    return r.returncode == 0 and source_label in (r.stdout or "")


def ensure_content_source(hostname, exec_prefix, project_label, source_label):
    """
    Idempotently attach `source_label` (a software channel label — "software"
    is the only Source type that exists in current source, despite CLM's
    original design mentioning others) to CLM project `project_label` via
    contentmanagement.attachSource. NOT live-tested.
    """
    if content_source_exists(hostname, exec_prefix, project_label, source_label):
        print("  Content project '{}' already has source '{}' — leaving it alone".format(
            project_label, source_label))
        return
    r = _api_call(hostname, exec_prefix, "contentmanagement.attachSource",
                  [project_label, "software", source_label])
    if r.returncode != 0:
        die("could not attach source '{}' to content project '{}': {}".format(
            source_label, project_label, (r.stderr or r.stdout or "").strip()))
    print("  Attached source '{}' to content project '{}'".format(source_label, project_label))


def ensure_content_filter(hostname, exec_prefix, project_label, filt):
    """
    Idempotently create-and-attach a filter (a
    {"name", "rule": "allow"|"deny", "entity_type": "package"|"erratum"|
    "module"|"ptf", "matcher", "field", "value"} dict) to CLM project
    `project_label`. Filters have NO lookup-by-name API (only by numeric
    id, which only createFilter's own return value ever gives you) — so
    this checks idempotency at the PROJECT level (does
    contentmanagement.listProjectFilters(project_label) already mention
    this filter's name), not a true global existence check, and extracts
    the freshly-created filter's id by regexing createFilter's raw printed
    return struct (a heuristic — the exact print format of a struct through
    spacecmd's 'api' passthrough was not independently confirmed). If that
    extraction fails, dies with the literal manual command to run instead
    of silently leaving the filter unattached. NOT live-tested.
    """
    name = filt.get("name")
    rule = filt.get("rule")
    entity_type = filt.get("entity_type")
    matcher = filt.get("matcher")
    field = filt.get("field")
    value = filt.get("value")
    if not (name and rule and entity_type and matcher and field is not None and value is not None):
        die("content project '{}': a filter entry needs name/rule/entity_type/matcher/field/value".format(
            project_label))

    existing = _api_call(hostname, exec_prefix, "contentmanagement.listProjectFilters", [project_label])
    if existing.returncode == 0 and name in (existing.stdout or ""):
        print("  Content project '{}' already has filter '{}' — leaving it alone".format(project_label, name))
        return

    criteria = {"matcher": matcher, "field": field, "value": value}
    r = _api_call(hostname, exec_prefix, "contentmanagement.createFilter", [name, rule, entity_type, criteria])
    if r.returncode != 0:
        die("could not create filter '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))

    m = re.search(r"['\"]id['\"]\s*:\s*(\d+)", r.stdout or "")
    if not m:
        die("filter '{}' was created but its numeric id could not be parsed from spacecmd's output "
            "to attach it — attach it manually: spacecmd api -A '[\"{}\", <filter_id>]' "
            "contentmanagement.attachFilter (raw output was: {})".format(
                name, project_label, (r.stdout or "").strip()))
    filter_id = int(m.group(1))

    r = _api_call(hostname, exec_prefix, "contentmanagement.attachFilter", [project_label, filter_id])
    if r.returncode != 0:
        die("could not attach filter '{}' (id {}) to content project '{}': {}".format(
            name, filter_id, project_label, (r.stderr or r.stdout or "").strip()))
    print("  Created and attached filter '{}' (id {}) to content project '{}'".format(
        name, filter_id, project_label))


def content_environment_exists(hostname, exec_prefix, project_label, env_label):
    """Whether `env_label` appears in
    contentmanagement.listProjectEnvironments(project_label)'s raw output."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjectEnvironments", [project_label])
    return r.returncode == 0 and env_label in (r.stdout or "")


def ensure_content_environments(hostname, exec_prefix, project_label, environments):
    """
    Idempotently creates the ordered lifecycle chain described by
    `environments` — each entry either a plain label string (name/description
    default to the label) or a {"label", "name", "description"} dict — via
    one contentmanagement.createEnvironment call per stage, threading each
    stage's predecessorLabel from the PREVIOUS entry in the list ("" for
    the first stage). No-op if `environments` is empty.

    Bug fixed here, confirmed live 2026-08-28: `predecessor` was never
    advanced past the first iteration — every stage after the first was
    created with predecessorLabel="" instead of the previous stage's
    label, silently building a set of disconnected "first" environments
    rather than the intended chain.

    Separately confirmed live: creating (or building) any environment can
    get stuck in "building" forever if the server's own async align worker
    has already wedged itself from an earlier build in the same server
    session — this is independent of which API call triggers it (a
    confirmed-live A/B test that initially looked like "the Web UI's REST
    endpoint works, this XML-RPC call doesn't" turned out to be confounded
    by call order; a clean-queue retest showed this exact XML-RPC call
    completing normally). See build_content_project's docstring and
    MIGRATION_TODO.md's "chasing the CLM stuck-build bug" writeups for the
    full account — flagged as a likely upstream bug, not fixed here.
    """
    predecessor = ""
    for env in environments or []:
        if isinstance(env, str):
            label, name, description = env, env, env
        else:
            label = env.get("label")
            name = env.get("name") or label
            description = env.get("description") or name
        if not label:
            die("content project '{}': an environment entry is missing required 'label'".format(project_label))

        if content_environment_exists(hostname, exec_prefix, project_label, label):
            print("  Content project '{}' already has environment '{}' — leaving it alone".format(
                project_label, label))
        else:
            r = _api_call(hostname, exec_prefix, "contentmanagement.createEnvironment",
                          [project_label, predecessor, label, name, description])
            if r.returncode != 0:
                die("could not create environment '{}' in content project '{}': {}".format(
                    label, project_label, (r.stderr or r.stdout or "").strip()))
            print("  Created environment '{}' in content project '{}'".format(label, project_label))

        predecessor = label


def ensure_content_projects(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_content_projects: a list of {label, name,
    description, sources: [...], filters: [...], environments: [...]}
    dicts — see module docstring for the CLM data model and its known
    gaps (filters especially). No-op if the field is unset or empty. This
    only defines the project/sources/filters/environment chain — it does
    NOT build or promote anything (see run_content_lifecycle_actions /
    the install scripts' --run-clm-actions flag for that, deliberately
    kept separate since those are one-shot, non-idempotent actions).
    NOT live-tested.
    """
    projects = cfg.get("{}_content_projects".format(prefix)) or []
    for proj in projects:
        label = proj.get("label")
        if not label:
            die("{}_content_projects: an entry is missing required 'label'".format(prefix))
        name = proj.get("name") or label
        description = proj.get("description") or name

        ensure_content_project(hostname, exec_prefix, label, name, description)
        for source_label in proj.get("sources") or []:
            ensure_content_source(hostname, exec_prefix, label, source_label)
        for filt in proj.get("filters") or []:
            ensure_content_filter(hostname, exec_prefix, label, filt)
        ensure_content_environments(hostname, exec_prefix, label, proj.get("environments") or [])


def build_content_project(hostname, exec_prefix, project_label, message=None):
    """
    Triggers a build of CLM project `project_label` via
    contentmanagement.buildProject — populates its first environment's
    channels from its sources+filters. Async: returns immediately, the real
    work continues server-side (see content_environment_status/
    wait_for_content_environment to poll it). NOT IDEMPOTENT: each call
    triggers a fresh build — see module docstring for why this is not part
    of the automatic ensure_* flow.

    Confirmed live 2026-08-28: this XML-RPC path and the Web UI's own REST
    endpoint (POST .../projects/{label}/build) call the exact same
    underlying Java method (ContentManager.buildProject(label, message,
    async=true, user)) — an initial controlled A/B test seemed to show the
    REST path succeeding while this one hung forever, but a clean-queue
    retest disproved that: with the server's async message queue freshly
    cleared (systemctl restart uyuni-server), THIS SAME XML-RPC call
    completed normally in ~1 minute. The original "REST works, XML-RPC
    doesn't" result was confounded by call ORDER, not the API surface — the
    real symptom (see MIGRATION_TODO.md's "chasing the CLM stuck-build bug"
    writeups for the fuller account) is that the server's async align
    worker can get itself
    wedged after a small number of builds, and once wedged, EVERY
    subsequent CLM build/environment-creation hangs in "building" forever
    regardless of which API triggered it — only a service restart has been
    confirmed to clear it. Not root-caused further than that; flagged as a
    likely upstream bug, not something fixable from here.
    """
    args = [project_label, message] if message else [project_label]
    r = _api_call(hostname, exec_prefix, "contentmanagement.buildProject", args)
    if r.returncode != 0:
        die("could not build content project '{}': {}".format(project_label, (r.stderr or r.stdout or "").strip()))
    print("  Build triggered for content project '{}'".format(project_label))


def promote_content_project(hostname, exec_prefix, project_label, from_env):
    """
    Triggers promotion of CLM project `project_label` from environment
    `from_env` to its successor via contentmanagement.promoteProject.
    IMPORTANT (confirmed directly in ContentManager.java, not from the
    admin-guide prose, which is ambiguous about this): `from_env` is the
    stage being promoted FROM, not the destination — the server looks up
    its successor itself. Async, same caveats as build_content_project
    (including the confirmed-live "queue can get wedged after a few builds,
    independent of API surface" finding — see its docstring).
    """
    r = _api_call(hostname, exec_prefix, "contentmanagement.promoteProject", [project_label, from_env])
    if r.returncode != 0:
        die("could not promote content project '{}' from environment '{}': {}".format(
            project_label, from_env, (r.stderr or r.stdout or "").strip()))
    print("  Promotion triggered for content project '{}' from environment '{}'".format(project_label, from_env))


def content_environment_status(hostname, exec_prefix, project_label, env_label):
    """
    Returns the environment's status field ("new"/"building"/
    "generating_repodata"/"built"/"failed") by regexing
    contentmanagement.lookupEnvironment's raw printed return struct — same
    print-format heuristic/caveat as ensure_content_filter's id extraction.
    Returns None if the status couldn't be parsed. NOT live-tested.
    """
    r = _api_call(hostname, exec_prefix, "contentmanagement.lookupEnvironment", [project_label, env_label])
    if r.returncode != 0:
        return None
    m = re.search(r"['\"]status['\"]\s*:\s*['\"]([a-zA-Z_]+)['\"]", r.stdout or "")
    return m.group(1) if m else None


def wait_for_content_environment(hostname, exec_prefix, project_label, env_label,
                                  target_statuses=("built", "failed"), timeout=1800, interval=15,
                                  die_on_timeout=True):
    """
    Polls content_environment_status() every `interval` seconds until it
    reaches one of `target_statuses` (default: "built" or "failed" — i.e.
    "done, either way"), or times out after `timeout` seconds. Returns the
    terminal status string on success.

    `die_on_timeout` (default True, matching this function's original
    behavior): on timeout, die() with a clear message. Pass False to
    instead return the last-seen status (possibly None, if the very first
    poll never returned one) so a caller can decide what to do — e.g.
    install_uyuni.py's CLM restart-and-retry wrapper, which needs to catch
    a timeout itself to attempt recovery rather than have the whole
    process die outright (see that wrapper's docstring, and this module's
    build_content_project docstring, for why: Uyuni's own async CLM align
    worker can get itself wedged after a small number of builds,
    confirmed live 2026-08-28 round 4, and a server restart is the only
    confirmed mitigation).
    """
    waited = 0
    status = None
    while waited < timeout:
        status = content_environment_status(hostname, exec_prefix, project_label, env_label)
        if status in target_statuses:
            return status
        time.sleep(interval)
        waited += interval
    if not die_on_timeout:
        return status
    die("timed out after {}s waiting for content environment '{}/{}' to reach {} (last seen: {})".format(
        timeout, project_label, env_label, target_statuses, status))


def run_content_lifecycle_actions(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_content_lifecycle_actions: a list of
    {"project", "action": "build"|"promote", "message" (build only),
    "from_env" (promote only), "wait": bool, "wait_env", "wait_timeout"}
    dicts, run in order. NOT idempotent (see build_content_project/
    promote_content_project) — meant to be invoked via the install scripts'
    --run-clm-actions flag, never as part of the automatic ensure_* flow.
    NOT live-tested (build/promote themselves have been — see their own
    docstrings — but not through this specific orchestration wrapper).
    """
    actions = cfg.get("{}_content_lifecycle_actions".format(prefix)) or []
    for a in actions:
        project = a.get("project")
        action = a.get("action")
        if not project or action not in ("build", "promote"):
            die("{}_content_lifecycle_actions: an entry needs 'project' and "
                "action 'build' or 'promote'".format(prefix))

        if action == "build":
            build_content_project(hostname, exec_prefix, project, a.get("message"))
        else:
            from_env = a.get("from_env")
            if not from_env:
                die("content_lifecycle_actions: a 'promote' entry requires 'from_env'")
            promote_content_project(hostname, exec_prefix, project, from_env)

        if a.get("wait"):
            wait_env = a.get("wait_env")
            if not wait_env:
                die("content_lifecycle_actions: 'wait' requires 'wait_env' (the environment to poll — "
                    "the first stage for a build, the successor stage for a promote)")
            status = wait_for_content_environment(hostname, exec_prefix, project, wait_env,
                                                   timeout=a.get("wait_timeout") or 1800)
            print("  Environment '{}/{}' reached status '{}'".format(project, wait_env, status))


def scap_scan_exists(hostname, exec_prefix, system, xccdf_path):
    """
    Whether a previous scan against `xccdf_path` already appears in
    'spacecmd scap_listxccdfscans <system>''s output. Heuristic (matches on
    path only, not path+profile — see module docstring): spacecmd's legacy
    XCCDF scan API has no built-in dedup.
    """
    r = _spacecmd(hostname, exec_prefix, "scap_listxccdfscans {}".format(shlex.quote(system)))
    return r.returncode == 0 and xccdf_path in (r.stdout or "")


def ensure_scap_scan(hostname, exec_prefix, system, xccdf_path, profile=None):
    """
    Heuristically-idempotently schedules a legacy XCCDF/OpenSCAP scan via
    spacecmd's native scap_schedulexccdfscan. Orchestration-only:
    `xccdf_path` (and the OpenSCAP scanner + SCAP Security Guide content
    packages) must already be installed on `system`'s own filesystem — this
    module pushes nothing there, same idiom as Ansible integration's
    control node. Skips if scap_scan_exists() already sees a scan against
    the same path for this system. NOT live-tested.
    """
    if scap_scan_exists(hostname, exec_prefix, system, xccdf_path):
        print("  System '{}' already has a scan for '{}' — leaving it alone".format(system, xccdf_path))
        return
    xccdf_options = "profile {}".format(profile) if profile else ""
    r = _spacecmd(hostname, exec_prefix, "scap_schedulexccdfscan {} {} {}".format(
        shlex.quote(xccdf_path), shlex.quote(xccdf_options), shlex.quote(system)))
    if r.returncode != 0:
        die("could not schedule XCCDF scan of '{}' on '{}': {}".format(
            xccdf_path, system, (r.stderr or r.stdout or "").strip()))
    print("  Scheduled XCCDF scan of '{}' on '{}' (profile: {})".format(xccdf_path, system, profile or "default"))


def list_scap_scans(hostname, exec_prefix, system):
    """Raw text of 'spacecmd scap_listxccdfscans <system>'. Read-only."""
    return _spacecmd(hostname, exec_prefix, "scap_listxccdfscans {}".format(shlex.quote(system))).stdout or ""


def scap_scan_details(hostname, exec_prefix, xid):
    """Raw text of 'spacecmd scap_getxccdfscandetails <xid>'. Read-only."""
    return _spacecmd(hostname, exec_prefix, "scap_getxccdfscandetails {}".format(shlex.quote(str(xid)))).stdout or ""


def scap_scan_rule_results(hostname, exec_prefix, xid):
    """Raw text of 'spacecmd scap_getxccdfscanruleresults <xid>'. Read-only."""
    return _spacecmd(hostname, exec_prefix,
                      "scap_getxccdfscanruleresults {}".format(shlex.quote(str(xid)))).stdout or ""


def run_scap_scans(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_scap_scans: a list of {system, xccdf_path,
    profile} dicts, run in order via ensure_scap_scan. NOT part of the
    automatic ensure_* flow — meant to be invoked via the install scripts'
    --run-scap-scans flag, same reasoning as Ansible/CLM (scheduling a scan
    is one-shot, real work). No-op if the field is unset or empty.
    NOT live-tested.
    """
    scans = cfg.get("{}_scap_scans".format(prefix)) or []
    for s in scans:
        system = s.get("system")
        xccdf_path = s.get("xccdf_path")
        if not system or not xccdf_path:
            die("{}_scap_scans: an entry needs 'system' and 'xccdf_path'".format(prefix))
        ensure_scap_scan(hostname, exec_prefix, system, xccdf_path, profile=s.get("profile"))


def list_systems_by_patch_status(hostname, exec_prefix, cve_id, patch_status_labels=None):
    """
    Returns the raw text of audit.listSystemsByPatchStatus(cveId[,
    statusLabels]) — no spacecmd subcommand exists for the 'audit'
    namespace at all (confirmed absent from source: no audit.py module, and
    errata.py's CVE-related commands only look up published errata, a
    different mechanism — see module docstring), so this goes through the
    generic 'api' passthrough. Pure read-only query: nothing to schedule,
    no idempotency concern. `patch_status_labels`, if given, is a list of
    labels from {"AFFECTED_PATCH_INAPPLICABLE", "AFFECTED_PATCH_APPLICABLE",
    "NOT_AFFECTED", "PATCHED"} to filter by. NOT live-tested.
    """
    args = [cve_id, list(patch_status_labels)] if patch_status_labels else [cve_id]
    r = _api_call(hostname, exec_prefix, "audit.listSystemsByPatchStatus", args)
    if r.returncode != 0:
        die("could not audit CVE '{}': {}".format(cve_id, (r.stderr or r.stdout or "").strip()))
    return r.stdout or ""


# ─── dev/QA/prod environment topology ────────────────────────────────────────
# System groups, multi-key activation-key/group linkage, custom-info tags,
# recurring patch schedules, and a thin composition layer over all of the
# above plus CLM (already built) — see the module docstring's "dev/QA/prod
# environment topology" entry for what's confirmed vs. deferred vs. heuristic.

def ensure_activation_keys(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_activation_keys: a list of dicts, each using the
    exact same <prefix>_activation_key* field names as the top-level
    single-key fields (reused as-is, one call per list entry) — lets a lab
    define multiple named activation keys (e.g. one per dev/qa/prod
    environment) without needing a separate org per key, the same reuse
    trick ensure_orgs() already uses for org-scoped keys. No-op if the
    field is unset or empty. NOT live-tested.
    """
    keys = cfg.get("{}_activation_keys".format(prefix)) or []
    for key_cfg in keys:
        ensure_activation_key(hostname, exec_prefix, key_cfg, prefix)
        ensure_appstreams(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_packages(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_groups(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_child_channels(hostname, exec_prefix, key_cfg, prefix)


def activation_key_child_channels(hostname, exec_prefix, key_name):
    """Returns the set of child channel labels currently linked to
    activation key `key_name`, via spacecmd's native
    activationkey_listchildchannels."""
    r = _spacecmd(hostname, exec_prefix, "activationkey_listchildchannels {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_child_channels(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently ensures every child channel listed in
    <prefix>_activation_key_child_channels is linked to
    <prefix>_activation_key, via spacecmd's native
    activationkey_addchildchannels. Same shape as
    ensure_activation_key_groups: a real list API exists
    (activationkey_listchildchannels), so this is genuinely idempotent and
    called unconditionally (not just at key-creation time) — generalizing
    that same pattern from groups to child channels.

    This is the real fix for a gap found live 2026-09-15: ensure_
    activation_key()'s own child-channel linking only ever runs at
    CREATION time — an already-existing key (the normal case on every run
    after the first) skips it entirely, so a lab JSON edit adding/
    correcting child_channels for an existing activation key silently had
    no effect at all until now. Confirmed live: several of solar-system-
    lab.json's own activation keys (leap16, rhel9, debian13,
    debian13arm64, oraclelinux9, amazonlinux2, amazonlinux2023) had NO
    Client Tools channel linked whatsoever — not a creation-time-only gap,
    a total, silent absence — because they were created once, early, with
    an empty child_channels field, and every later JSON fix to add the
    right channel never got applied since the key already existed.

    No-op if either the key or the child-channels field is unset. Calling
    this alongside ensure_activation_key's own creation-time linking is
    harmless — it just finds nothing new to add if that path already
    handled it.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_child_channels".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_child_channels(hostname, exec_prefix, key_name)
    missing = [c for c in spec if c not in existing]
    if not missing:
        print("  Activation key '{}' already linked to all requested child channels — "
              "leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addchildchannels {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(c) for c in missing)))
    if r.returncode != 0:
        warn("could not link child channels ({}) to activation key '{}' — check they're "
             "actually synced on the server (`mgr-sync add channels`), not just referenced "
             "in {}_activation_key_child_channels: {}".format(
                 ", ".join(missing), key_name, prefix, (r.stderr or r.stdout or "").strip()))
        return
    print("  Linked {} child channel(s) to activation key '{}': {}".format(
        len(missing), key_name, ", ".join(missing)))


def activation_key_groups(hostname, exec_prefix, key_name):
    """Returns the set of system group names currently linked to
    activation key `key_name`, via spacecmd's native
    activationkey_listgroups."""
    r = _spacecmd(hostname, exec_prefix, "activationkey_listgroups {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_groups(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently ensures every system group name listed in
    <prefix>_activation_key_groups (space-separated) is linked to
    <prefix>_activation_key, via spacecmd's native activationkey_addgroups.
    Same shape as ensure_activation_key_packages: a real list API exists
    (activationkey_listgroups), so this is genuinely idempotent and called
    unconditionally (not just at key-creation time) — generalizing that
    same pattern from packages to groups. This is a separate,
    independently-callable function from ensure_activation_key's own
    creation-time-only 'groups' follow-up (which reads the SAME field, but
    only applies it when the key is newly created) — calling both is
    harmless, since this one just finds nothing new to add if the other
    already handled it. No-op if either the key or the groups field is
    unset. NOT live-tested.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_groups".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_groups(hostname, exec_prefix, key_name)
    missing = [g for g in spec if g not in existing]
    if not missing:
        print("  Activation key '{}' already linked to all requested groups — leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addgroups {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(g) for g in missing)))
    if r.returncode != 0:
        die("could not link groups to activation key '{}': {}".format(
            key_name, (r.stderr or r.stdout or "").strip()))
    print("  Linked {} group(s) to activation key '{}': {}".format(len(missing), key_name, ", ".join(missing)))


def group_exists(hostname, exec_prefix, name):
    """Whether `name` appears in 'spacecmd group_list''s output."""
    r = _spacecmd(hostname, exec_prefix, "group_list")
    return r.returncode == 0 and name in (r.stdout or "")


def ensure_system_group(hostname, exec_prefix, name, description=None):
    """Idempotently create a system group via spacecmd's native
    group_create. NOT live-tested."""
    if group_exists(hostname, exec_prefix, name):
        print("  System group '{}' already exists — leaving it alone".format(name))
        return
    r = _spacecmd(hostname, exec_prefix, "group_create {} {}".format(
        shlex.quote(name), shlex.quote(description or name)))
    if r.returncode != 0:
        die("could not create system group '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created system group '{}'".format(name))


def list_group_systems(hostname, exec_prefix, name):
    """Raw text of 'spacecmd group_listsystems <name>' — one system name
    per line, per source. Read-only."""
    return _spacecmd(hostname, exec_prefix, "group_listsystems {}".format(shlex.quote(name))).stdout or ""


def group_has_system(hostname, exec_prefix, group_name, system):
    """Whether `system` appears in group_name's member list."""
    return system in list_group_systems(hostname, exec_prefix, group_name)


def ensure_group_systems(hostname, exec_prefix, group_name, systems):
    """
    Idempotently ensures every system name in `systems` is a member of
    `group_name`, via spacecmd's native group_addsystems — adds only the
    ones not already listed by group_listsystems. No-op if `systems` is
    empty.

    Confirmed live 2026-09-15: group_addsystems silently skips any name in
    the list that isn't (yet) a real registered system, AS LONG AS at least
    one other name in the same call IS real — but if EVERY name in one call
    is invalid, it fails outright instead (exit 1, empty stderr). A
    `systems` list built from a lab's own node hostnames routinely contains
    names not registered yet (client_registration pending or intentionally
    never a client, e.g. the server's own hostname in a "star"-type group)
    — self-healing once they do register, exactly like
    ensure_activation_key_child_channels' own not-yet-synced-channel case.
    warn(), don't die(), so one not-yet-ready group doesn't abort every
    other orchestration step after it.
    """
    if not systems:
        return
    existing = list_group_systems(hostname, exec_prefix, group_name)
    missing = [s for s in systems if s not in existing]
    if not missing:
        print("  System group '{}' already has all requested systems — leaving it alone".format(group_name))
        return
    r = _spacecmd(hostname, exec_prefix, "group_addsystems {} {}".format(
        shlex.quote(group_name), " ".join(shlex.quote(s) for s in missing)))
    if r.returncode != 0:
        warn("could not add system(s) ({}) to group '{}' — they may not be registered systems yet: "
             "{}".format(", ".join(missing), group_name, (r.stderr or r.stdout or "").strip()))
        return
    print("  Added {} system(s) to group '{}': {}".format(len(missing), group_name, ", ".join(missing)))


def ensure_system_groups(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_system_groups: a list of {name, description,
    systems: [...]} dicts. Idempotent, safe to call on every run. No-op if
    the field is unset or empty. NOT live-tested.
    """
    groups = cfg.get("{}_system_groups".format(prefix)) or []
    for g in groups:
        name = g.get("name")
        if not name:
            die("{}_system_groups: an entry is missing required 'name'".format(prefix))
        ensure_system_group(hostname, exec_prefix, name, g.get("description"))
        ensure_group_systems(hostname, exec_prefix, name, g.get("systems") or [])


def custom_info_key_exists(hostname, exec_prefix, name):
    """Whether `name` appears in 'spacecmd custominfo_listkeys''s output."""
    r = _spacecmd(hostname, exec_prefix, "custominfo_listkeys")
    return r.returncode == 0 and name in (r.stdout or "")


def ensure_custom_info_key(hostname, exec_prefix, name, description=None):
    """
    Idempotently define an org-level custom info key via spacecmd's native
    custominfo_createkey — a value can't be set for a key on any system
    until the key itself is defined this way first. NOT live-tested.
    """
    if custom_info_key_exists(hostname, exec_prefix, name):
        print("  Custom info key '{}' already exists — leaving it alone".format(name))
        return
    r = _spacecmd(hostname, exec_prefix, "custominfo_createkey {} {}".format(
        shlex.quote(name), shlex.quote(description or name)))
    if r.returncode != 0:
        die("could not create custom info key '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created custom info key '{}'".format(name))


def ensure_custom_info_keys(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_custom_info_keys: a list of {name,
    description} dicts. No-op if unset/empty. NOT live-tested."""
    keys = cfg.get("{}_custom_info_keys".format(prefix)) or []
    for k in keys:
        name = k.get("name")
        if not name:
            die("{}_custom_info_keys: an entry is missing required 'name'".format(prefix))
        ensure_custom_info_key(hostname, exec_prefix, name, k.get("description"))


def ensure_system_tag(hostname, exec_prefix, system, key, value):
    """
    Sets a custom-info key=value pair ("tag" — Uyuni has no first-class tag
    object, see module docstring) on `system` via spacecmd's native
    system_addcustomvalue. Treated as safely upsert-able without a
    pre-check: system_updatecustomvalue is documented as a literal alias of
    the same underlying call, implying setCustomValues itself doesn't
    distinguish create-vs-update — an inference, not independently
    confirmed. The key must already be defined (see ensure_custom_info_key)
    or this fails. NOT live-tested.
    """
    r = _spacecmd(hostname, exec_prefix, "system_addcustomvalue {} {} {}".format(
        shlex.quote(key), shlex.quote(value), shlex.quote(system)))
    if r.returncode != 0:
        die("could not set tag '{}={}' on system '{}': {}".format(
            key, value, system, (r.stderr or r.stdout or "").strip()))
    print("  Set tag '{}={}' on system '{}'".format(key, value, system))


def ensure_system_tags(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_system_tags: a list of {system, tags: {key:
    value, ...}} dicts. No-op if unset/empty. NOT live-tested.
    """
    entries = cfg.get("{}_system_tags".format(prefix)) or []
    for entry in entries:
        system = entry.get("system")
        tags = entry.get("tags") or {}
        if not system or not tags:
            die("{}_system_tags: an entry needs 'system' and a non-empty 'tags' map".format(prefix))
        for key, value in tags.items():
            ensure_system_tag(hostname, exec_prefix, system, key, str(value))


def group_id_for(hostname, exec_prefix, group_name):
    """
    Best-effort numeric id lookup for a system group by name, by regexing
    'spacecmd group_details <name>''s human-readable output — spacecmd's
    exact display format for this wasn't independently confirmed, so this
    is a heuristic. Returns None if no id could be parsed; callers should
    fall back to an explicit group_id given directly in the JSON, same
    precedent as Ansible integration's control_node_id.
    """
    r = _spacecmd(hostname, exec_prefix, "group_details {}".format(shlex.quote(group_name)))
    if r.returncode != 0:
        return None
    m = re.search(r"(?im)^\s*(?:group\s*)?id\s*:\s*(\d+)\s*$", r.stdout or "")
    return int(m.group(1)) if m else None


def ensure_recurring_schedule(hostname, exec_prefix, entity_type, entity_id, cron_expr,
                               schedule_type="highstate", states=None, extra=None):
    """
    Creates a recurring action (Salt highstate, or an arbitrary ordered
    list of Salt states) via recurring.highstate.create /
    recurring.custom.create — neither is wrapped by spacecmd (confirmed
    absent: no recurring.py in spacecmd's source tree), so this goes
    through the generic 'api' passthrough. `entity_type` is
    "minion"|"group"|"org" and `entity_id` its NUMERIC id (see
    group_id_for() for resolving a system group's id). `schedule_type`
    selects "highstate" (Salt highstate only) or "custom" (the `states`
    list, required in that case). `extra`, if given, is merged into the
    actionProps struct as-is — the confirmed field set is
    entity_type/entity_id/cron_expr(+states for custom), but Uyuni's own
    "Recurring Action" concept plausibly needs more (e.g. a name) that
    research couldn't confirm; `extra` lets a caller supply whatever the
    real API turns out to need without a code change, and a real API error
    surfaces clearly via die() if something required is still missing.
    NOT confirmed idempotent — no list/exists method was found for
    recurring actions during research — so this is deliberately NOT wired
    into any automatic flow; see run_environment_schedules(). NOT
    live-tested.
    """
    if schedule_type not in ("highstate", "custom"):
        die("invalid recurring schedule type '{}': expected 'highstate' or 'custom'".format(schedule_type))
    props = {"entity_type": entity_type, "entity_id": entity_id, "cron_expr": cron_expr}
    if schedule_type == "custom":
        if not states:
            die("recurring schedule type 'custom' requires a non-empty 'states' list")
        props["states"] = list(states)
    if extra:
        props.update(extra)

    method = "recurring.{}.create".format(schedule_type)
    r = _api_call(hostname, exec_prefix, method, [props])
    if r.returncode != 0:
        die("could not create recurring {} schedule for {} {}: {}".format(
            schedule_type, entity_type, entity_id, (r.stderr or r.stdout or "").strip()))
    print("  Created recurring {} schedule for {} {} (cron: {})".format(
        schedule_type, entity_type, entity_id, cron_expr))


def ensure_environments(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_environments — a THIN COMPOSITION layer, not a
    new Uyuni concept (see module docstring: no native "environment" or
    "release" object exists to build on). Each entry:
      {label, system_group, activation_key, custom_info_tags: {k: v},
       recurring_schedule: {...}}
    `system_group` and `activation_key` are NAME REFERENCES to entries
    already defined elsewhere (<prefix>_system_groups,
    <prefix>_activation_keys or the top-level singular
    <prefix>_activation_key) — this function does NOT create them, only
    links an already-existing group to an already-existing key (via
    ensure_activation_key_groups) and applies any custom_info_tags to every
    system currently in that group. Idempotent, safe to call on every run.
    `recurring_schedule`, if present, is deliberately NOT applied here —
    see run_environment_schedules() and the install scripts'
    --run-recurring-schedules flag, since recurring-action idempotency was
    never confirmed. No-op if the field is unset or empty. NOT live-tested.
    """
    environments = cfg.get("{}_environments".format(prefix)) or []
    for env in environments:
        label = env.get("label")
        if not label:
            die("{}_environments: an entry is missing required 'label'".format(prefix))

        group_name = env.get("system_group")
        key_name = env.get("activation_key")
        if group_name and key_name:
            link_cfg = {"{}_activation_key".format(prefix): key_name,
                        "{}_activation_key_groups".format(prefix): group_name}
            ensure_activation_key_groups(hostname, exec_prefix, link_cfg, prefix)

        tags = env.get("custom_info_tags") or {}
        if tags and group_name:
            for system in list_group_systems(hostname, exec_prefix, group_name).splitlines():
                system = system.strip()
                if not system:
                    continue
                for key, value in tags.items():
                    ensure_system_tag(hostname, exec_prefix, system, key, str(value))


def run_environment_schedules(hostname, exec_prefix, cfg, prefix):
    """
    Explicit trigger (see the install scripts' --run-recurring-schedules
    flag) for every <prefix>_environments entry's recurring_schedule:
    resolves that environment's system_group to a numeric group id
    (group_id_for() — a heuristic; an entry can instead give 'group_id'
    directly under recurring_schedule to skip resolution) and calls
    ensure_recurring_schedule(). NOT idempotent — see
    ensure_recurring_schedule's own docstring. No-op if
    <prefix>_environments is unset/empty or no entry has a
    recurring_schedule. NOT live-tested.
    """
    environments = cfg.get("{}_environments".format(prefix)) or []
    for env in environments:
        sched = env.get("recurring_schedule")
        if not sched:
            continue
        label = env.get("label", "?")
        group_name = env.get("system_group")
        group_id = sched.get("group_id")
        if group_id is None:
            if not group_name:
                die("environment '{}': recurring_schedule needs 'system_group' or an explicit "
                    "'group_id'".format(label))
            group_id = group_id_for(hostname, exec_prefix, group_name)
            if group_id is None:
                die("environment '{}': could not resolve system group '{}' to a numeric id — "
                    "supply 'group_id' explicitly in recurring_schedule".format(label, group_name))
        cron = sched.get("cron")
        if not cron:
            die("environment '{}': recurring_schedule needs 'cron'".format(label))
        ensure_recurring_schedule(hostname, exec_prefix, "group", group_id, cron,
                                   schedule_type=sched.get("type") or "highstate",
                                   states=sched.get("states"), extra=sched.get("extra"))


# ── Client registration (Salt bootstrap) ───────────────────────────────────
#
# For install_client_registration.py — registers some OTHER host (not the
# server itself) as a Salt client. `hostname`/`exec_prefix` below are always
# the SERVER's (for spacecmd/saltkey calls); the client is addressed
# separately via ssh_run(client_hostname, ...) for the bootstrap curl, since
# it's just a plain SSH target, not something reached through exec_prefix.

def saltkey_pending(hostname, exec_prefix):
    """Raw stdout of saltkey.pendingList — minion IDs awaiting acceptance."""
    return (_api_call(hostname, exec_prefix, "saltkey.pendingList", []).stdout or "")


def saltkey_accepted(hostname, exec_prefix, minion_id):
    """Whether minion_id already appears in saltkey.acceptedList's output —
    i.e. this client is already a fully registered system."""
    r = _api_call(hostname, exec_prefix, "saltkey.acceptedList", [])
    return r.returncode == 0 and minion_id in (r.stdout or "")


def saltkey_accept(hostname, exec_prefix, minion_id):
    """Accept a pending minion key via saltkey.accept. Dies on failure."""
    r = _api_call(hostname, exec_prefix, "saltkey.accept", [minion_id])
    if r.returncode != 0:
        die("could not accept salt key for '{}': {}".format(minion_id, (r.stderr or r.stdout or "").strip()))


def ensure_client_registered(hostname, exec_prefix, client_hostname, server_fqdn, activation_key,
                              reactivation_key=None, retry_limit=30, retry_interval=10):
    """
    Register client_hostname as a Salt client of the Uyuni/SMLM server
    reached via (hostname, exec_prefix), using an activation key the caller
    has already ensured exists (see ensure_activation_key). Idempotent: a
    no-op if client_hostname's key is already accepted. Minion ID is assumed
    to equal client_hostname (this project's own FQDN convention — see
    module docstring).

    Steps: generate a bootstrap script for this specific activation key on
    the server (confirmed live, 2026-08-28: /pub/bootstrap/bootstrap.sh does
    NOT exist by default — 404 — until generated, either via the Web UI's
    "Bootstrap Script" page or, as done here, `mgr-bootstrap
    --activation-keys=<key>`; a stock `curl .../bootstrap.sh` against a
    freshly-installed server just downloads the server's own login-page
    HTML, which then fails loudly as a shell script), then run that script
    on the client over SSH (not exec_prefix — the client is a plain SSH
    target, unrelated to the server's own container/host wrapper), then
    poll the server's pending-key list until the client's minion ID appears
    (or retry_limit is exhausted), then accept it. The generated script is
    named after the activation key (not the shared default "bootstrap.sh")
    so multiple keys don't clobber each other's script on repeat use —
    mgr-bootstrap itself only supports one key per generated script.
    """
    if saltkey_accepted(hostname, exec_prefix, client_hostname):
        print("  '{}' is already a registered client — leaving it alone".format(client_hostname))
        return

    # Resolve to Uyuni's real, org-id-prefixed key name (see
    # resolve_activation_key_name's docstring) — both mgr-bootstrap and the
    # client's own ACTIVATION_KEYS env var need an exact match.
    activation_key = resolve_activation_key_name(hostname, exec_prefix, activation_key)
    script_name = "{}.sh".format(re.sub(r"[^A-Za-z0-9_.-]", "_", activation_key))
    print("  Generating the bootstrap script for activation key '{}'".format(activation_key))
    r = _run(hostname, exec_prefix,
             "mgr-bootstrap --activation-keys={} --script={}".format(
                 shlex.quote(activation_key), shlex.quote(script_name)),
             check=False, capture=True)
    if r.returncode != 0:
        die("could not generate the bootstrap script on '{}' for key '{}': {}".format(
            hostname, activation_key, (r.stderr or r.stdout or "").strip()))

    print("  Bootstrapping '{}' against '{}'".format(client_hostname, server_fqdn))
    env = "ACTIVATION_KEYS={}".format(shlex.quote(activation_key))
    if reactivation_key:
        env += " REACTIVATION_KEY={}".format(shlex.quote(reactivation_key))
    bootstrap_cmd = "{} curl -Sks https://{}/pub/bootstrap/{} | /bin/bash".format(
        env, server_fqdn, script_name)
    r = ssh_run(client_hostname, bootstrap_cmd, check=False)
    if r.returncode != 0:
        die("bootstrap script failed on '{}' (rc={})".format(client_hostname, r.returncode))

    print("  Waiting for '{}''s salt key to appear …".format(client_hostname))
    for _ in range(retry_limit):
        if client_hostname in saltkey_pending(hostname, exec_prefix):
            break
        if saltkey_accepted(hostname, exec_prefix, client_hostname):
            # Some other run/process (or a server-side autosign policy) may
            # have already accepted it while we were polling.
            print("  '{}' is already accepted".format(client_hostname))
            return
        time.sleep(retry_interval)
    else:
        die("'{}''s salt key never appeared as pending after bootstrap ({}s) — "
            "check connectivity to {}:4505/4506 and the bootstrap script's own output".format(
                client_hostname, retry_limit * retry_interval, server_fqdn))

    saltkey_accept(hostname, exec_prefix, client_hostname)
    print("  Accepted salt key for '{}'".format(client_hostname))


# ── Config export: read a live server back into lab-in-a-box JSON ──────────
# Added 2026-09-16 at the user's explicit request ("generate a lab
# definition based on the configuration of an existing server") — the
# reverse of every ensure_* function above: read-only spacecmd/API calls,
# parsed back into the exact <prefix>_* field shapes ensure_activation_keys/
# ensure_system_groups/ensure_access_groups/ensure_orgs already consume, so
# the result can be pasted straight into a lab JSON's "smlm"/"uyuni" section.
# Two real, confirmed-live API gaps limit what's recoverable, documented
# where they bite below rather than silently guessed around:
#   - passwords are one-way hashed server-side — a re-imported user/org
#     admin always needs a real password filled in by hand.
#   - a custom access group's own member list isn't queryable for any org
#     other than the CALLING session's own (user.getDetails/access.listRoles
#     are both hard org-scoped, even for a satellite_admin — see
#     ensure_user_role()'s own docstring for the identical constraint on
#     the write side) — so smlm_access_groups' `users` field is exported
#     only for the org this session is currently authenticated as.

def describe_activation_key(hostname, exec_prefix, key_name, prefix):
    """
    Reads one activation key's live configuration via spacecmd's native
    activationkey_details and returns it as a dict using the same
    <prefix>_activation_key* field names ensure_activation_key() consumes —
    a direct, valid entry for <prefix>_activation_keys.

    `key_name` is the key's real, org-id-prefixed label as spacecmd knows
    it (e.g. "1-sles15sp7", confirmed live: activationkey_create's own
    org-id-prefixing, see ensure_activation_key()'s neighboring code) — the
    returned <prefix>_activation_key value has that numeric prefix stripped
    back off, matching what a lab JSON actually specifies (the prefix is
    applied server-side at creation time, never part of the caller's own
    input).
    """
    text = _spacecmd(hostname, exec_prefix, "activationkey_details {}".format(
        shlex.quote(key_name))).stdout or ""

    def field(label):
        m = re.search(r"^{}:\s*(.*)$".format(re.escape(label)), text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    def section(header):
        # Stop at the next section header (a line immediately followed by a
        # dashes-only line) or end of string, NOT at the first blank line —
        # confirmed live 2026-09-16 that an EMPTY section (e.g. "Configuration
        # Channels" with nothing under it) is followed by only ONE blank line
        # before the next header, not two, so a "\n\n" stop bled straight
        # into the next header's own text.
        m = re.search(r"^{}\n-+\n(.*?)(?=\n[A-Za-z][^\n]*\n-+\n|\Z)".format(re.escape(header)),
                       text, re.MULTILINE | re.DOTALL)
        return [line.strip() for line in (m.group(1).splitlines() if m else []) if line.strip()]

    channel_lines = section("Software Channels")
    base_channel = channel_lines[0].lstrip("|- ").strip() if channel_lines else ""
    child_channels = [line.lstrip("|- ").strip() for line in channel_lines[1:]]

    entry = {
        "{}_activation_key".format(prefix): re.sub(r"^\d+-", "", key_name),
        "{}_activation_key_desc".format(prefix): field("Description"),
        "{}_activation_key_base_channel".format(prefix): base_channel,
    }
    if child_channels:
        entry["{}_activation_key_child_channels".format(prefix)] = " ".join(child_channels)
    groups = section("System Groups")
    if groups:
        entry["{}_activation_key_groups".format(prefix)] = " ".join(groups)
    config_channels = section("Configuration Channels")
    if config_channels:
        entry["{}_activation_key_config_channels".format(prefix)] = " ".join(config_channels)
    entitlements = field("Entitlements") or ",".join(section("Entitlements"))
    if entitlements:
        entry["{}_activation_key_entitlements".format(prefix)] = entitlements
    packages = section("Packages")
    if packages:
        entry["{}_activation_key_packages".format(prefix)] = " ".join(packages)
    if (field("Universal Default") or "").strip().lower() == "true":
        entry["{}_activation_key_universal_default".format(prefix)] = "true"
    contact_method = field("Contact Method")
    if contact_method and contact_method != "default":
        entry["{}_activation_key_contact_method".format(prefix)] = contact_method
    return entry


def describe_system_group(hostname, exec_prefix, name):
    """
    Reads one system group's live members via spacecmd's native
    group_details, and returns a dict matching one <prefix>_system_groups
    entry: {"name": ..., "description": ..., "systems": [...]}.
    """
    text = _spacecmd(hostname, exec_prefix, "group_details {}".format(shlex.quote(name))).stdout or ""
    m = re.search(r"^Description:\s*(.*)$", text, re.MULTILINE)
    description = m.group(1).strip() if m else name
    m = re.search(r"^Members\n-+\n(.*?)\Z", text, re.MULTILINE | re.DOTALL)
    systems = [line.strip() for line in (m.group(1).splitlines() if m else []) if line.strip()]
    entry = {"name": name, "description": description}
    if systems:
        entry["systems"] = systems
    return entry


def describe_access_groups(hostname, exec_prefix):
    """
    Reads every custom access group VISIBLE TO THE CURRENT SESSION'S OWN
    ORG via access.listRoles + access.listPermissions, and returns a list
    of <prefix>_access_groups entries: {"label", "description",
    "permissions": [{"namespace", "mode"}]}. No `users` field — see this
    module's own top-of-section note on why that can't be recovered for
    any org other than the caller's own, and even for the caller's own org
    there's no API to map a namespace-permission grant back to the
    individual users holding that role (only the reverse: user -> roles,
    itself org-scoped and, for custom labels specifically, further gated by
    the same real getAssignableRoles restriction ensure_user_role()'s
    docstring documents). Callers wanting `users` populated must add it by
    hand.
    """
    r = _api_call(hostname, exec_prefix, "access.listRoles", [])
    try:
        roles = json.loads(r.stdout or "[]")
    except (ValueError, TypeError):
        roles = []

    groups = []
    for role in roles:
        label = role.get("label")
        if not label:
            continue
        perms_r = _api_call(hostname, exec_prefix, "access.listPermissions", [label])
        try:
            perms = json.loads(perms_r.stdout or "[]")
        except (ValueError, TypeError):
            perms = []
        permissions = [
            {"namespace": p["namespace"], "mode": (p.get("access_mode") or {}).get("value", "R")}
            for p in perms if p.get("namespace")
        ]
        entry = {"label": label, "description": role.get("description") or label}
        if permissions:
            entry["permissions"] = permissions
        groups.append(entry)
    return groups


def export_config(hostname, exec_prefix, admin, password, prefix):
    """
    Reads a live server's current configuration back into a dict shaped
    exactly like a lab JSON's "smlm"/"uyuni" top-level section (same
    <prefix>_* field names ensure_channels_synced/ensure_activation_keys/
    ensure_system_groups/ensure_access_groups/ensure_orgs already consume)
    — the reverse of every ensure_* function in this module. Read-only:
    issues no write calls at all.

    Covers: every software channel currently on the server
    (<prefix>_channels), every activation key with its full detail
    (<prefix>_activation_keys, via describe_activation_key), every system
    group with its current members (<prefix>_system_groups, via
    describe_system_group), the CURRENT org's own custom access groups
    (<prefix>_access_groups, via describe_access_groups — see its own
    docstring for the real org-scoping limit), and every OTHER org
    (<prefix>_orgs) with its own username list (org_listusers, confirmed
    live to work cross-org even though user.getDetails does not) — each
    flagged with an "_export_note" key (not a real schema field — strip it
    before use) since admin_pass/password/first_name/last_name/email can
    never be recovered from a live server (passwords are one-way hashed)
    and must be filled in by hand before this is usable to actually
    recreate that org/its users elsewhere.
    """
    ensure_spacecmd_config(hostname, exec_prefix, admin, password)

    channels = [line.strip() for line in
                (_spacecmd(hostname, exec_prefix, "softwarechannel_list").stdout or "").splitlines()
                if line.strip()]

    key_names = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "activationkey_list").stdout or "").splitlines()
                 if line.strip()]
    activation_keys = [describe_activation_key(hostname, exec_prefix, k, prefix) for k in key_names]

    group_names = [line.strip() for line in
                   (_spacecmd(hostname, exec_prefix, "group_list").stdout or "").splitlines()
                   if line.strip()]
    system_groups = [describe_system_group(hostname, exec_prefix, g) for g in group_names]

    access_groups = describe_access_groups(hostname, exec_prefix)

    org_names = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "org_list").stdout or "").splitlines()
                 if line.strip()]
    details = _spacecmd(hostname, exec_prefix, "user_details {}".format(shlex.quote(admin))).stdout or ""
    m = re.search(r"^Organisation:\s*(.*)$", details, re.MULTILINE)
    own_org = m.group(1).strip() if m else None

    orgs = []
    for org_name in org_names:
        if org_name == own_org:
            continue
        users = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "org_listusers {}".format(shlex.quote(org_name))).stdout
                  or "").splitlines() if line.strip()]
        orgs.append({
            "name": org_name,
            "_export_note": "admin_user/admin_pass/admin_email cannot be recovered from a live "
                             "server (passwords are one-way hashed) — fill these in by hand before "
                             "this org can be recreated elsewhere. 'existing_users' below is a "
                             "best-effort username list (org_listusers); none of their own "
                             "password/first_name/last_name/email could be recovered either "
                             "(user.getDetails is hard org-scoped, even for a satellite_admin) — "
                             "use it as a checklist, not a ready-to-use {}_users list.".format(prefix),
            "existing_users": users,
        })

    result = {
        "{}_admin".format(prefix): admin,
        "{}_org".format(prefix): own_org or "",
        "{}_channels".format(prefix): channels,
    }
    if activation_keys:
        result["{}_activation_keys".format(prefix)] = activation_keys
    if system_groups:
        result["{}_system_groups".format(prefix)] = system_groups
    if access_groups:
        result["{}_access_groups".format(prefix)] = access_groups
    if orgs:
        result["{}_orgs".format(prefix)] = orgs
    return result
