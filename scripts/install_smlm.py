#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUSE Multi-Linux Manager (SMLM) on Kubernetes
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://documentation.suse.com/multi-linux-manager/5.2/en/docs/specialized-guides/kubernetes-guide/server-kubernetes-deployment.html
#
# ─── JSON section: "smlm" ───────────────────────────────────────────────────────
#
# OPTIONAL – deployment mode
#   smlm_deployment        : "kubernetes" (default) = the Helm-chart deployment
#                           documented at this file's own Kubernetes-guide
#                           reference link above, target node is any
#                           Kubernetes cluster's first server node.
#                           "podman" = the traditional mgradm/podman
#                           deployment directly on a dedicated host/VM (no
#                           Kubernetes at all), per
#                           documentation.suse.com/multi-linux-manager/5.2/en/
#                           docs/installation-and-upgrade/'s own bare-metal
#                           install guide — see setup_smlm_podman()'s own
#                           docstring. Target node(s): any node listing
#                           "smlm" in its own addons[] list.
#
# MANDATORY when smlm_deployment is "kubernetes" (the default)
#   smlm_fqdn             : Fully-qualified domain name for the SMLM server
#                           (e.g. "smlm.cluster1.mydemo.lab")
#   smlm_scc_user         : SUSE Customer Center (SCC) account username — used
#                           to `podman login registry.suse.com` to pull the
#                           chart's entitled images (mirroring credentials)
#   smlm_scc_password     : SUSE Customer Center (SCC) account password
#
# MANDATORY when smlm_deployment is "podman"
#   smlm_scc_regcode      : SUSE Customer Center (SCC) registration code —
#                           NOT an "account"/"subscription ID": this is the
#                           per-subscription code shown on scc.suse.com next
#                           to each of your subscriptions, and IS what
#                           identifies which subscription to activate against
#                           (SUSEConnect has no separate concept of a
#                           subscription id). Registers both the base OS
#                           product (`SUSEConnect -r <code>`) and the SMLM
#                           extension module (`SUSEConnect -p <product> -r
#                           <code>`) — confirmed via documentation.suse.com/
#                           multi-linux-manager/5.2's own server-deployment
#                           guide.
#
# OPTIONAL but STRONGLY RECOMMENDED when smlm_deployment is "podman" —
# MANDATORY in practice if smlm_channels or smlm_activation_keys are also set
#   smlm_scc_user         : SAME field/meaning as the kubernetes-mode field
#                           above, reused here for TWO separate reasons:
#                           (1) `mgradm install podman` pulls SMLM's entitled
#                           container images from registry.suse.com, which
#                           needs its own `podman login` — separate from,
#                           and IN ADDITION TO, the SUSEConnect host
#                           registration above. Confirmed via the same
#                           official doc's own "if the install fails, log in
#                           to the registry" section — this one alone is
#                           merely recommended, since the docs frame it as a
#                           fallback. (2) mgr-sync has no visibility into
#                           which channels/products are entitled until these
#                           SAME credentials are registered as its own
#                           "organization credentials" via `mgr-sync add
#                           credentials` — confirmed real command (Uyuni's
#                           own cli-sync reference), REQUIRED whenever
#                           smlm_channels or smlm_activation_keys are set, or
#                           the channel sync will simply never find anything
#                           (setup_smlm_podman() dies with a clear message if
#                           channels/activation keys are requested without
#                           these two fields). If omitted (and no channels
#                           requested), setup_smlm_podman() skips the
#                           registry login and relies on SUSEConnect's own
#                           registration alone for the image pull.
#   smlm_scc_password     : SAME field/meaning as the kubernetes-mode field above.
#   smlm_scc_product      : Exact SCC extension-product identifier for the
#                           "SUSE Multi-Linux Manager" module —
#                           `SUSEConnect -p <that> -r <smlm_scc_regcode>`.
#                           Default (used if unset): "Multi-Linux-Manager-
#                           Server-SLE/5.2/x86_64" — confirmed via
#                           documentation.suse.com/multi-linux-manager/5.2's
#                           own server-deployment guide (quoted directly, not
#                           guessed), but that guide was published as "5.2
#                           RC" — override this field if your real server
#                           rejects the default once SUSE Multi-Linux
#                           Manager 5.2 reaches GA and the identifier shifts.
#
# OPTIONAL – passwords/credentials
#   smlm_db_admin_user    : DB admin username          (default: mlmadmin)
#   smlm_db_admin_pass    : DB admin password          (default: mlmadmin123)
#   smlm_db_user          : DB application username    (default: mlmuser)
#   smlm_db_pass          : DB application password    (default: mlmuser123)
#   smlm_reportdb_user    : Report DB username         (default: reportuser)
#   smlm_reportdb_pass    : Report DB password         (default: reportuser123)
#   smlm_admin_user       : SMLM web UI admin username (default: admin)
#   smlm_admin_pass       : SMLM web UI admin password (default: admin123 for
#                            smlm_deployment "kubernetes"; "Smlm12345" for "podman"
#                            — mgradm's own product-specific default, matching
#                            install_uyuni.py's own "Uyuni12345" precedent)
#   smlm_email            : admin account email, "podman" deployment only, passed to
#                            `mgradm install`'s own --email flag  (default: admin@lab.local)
#   smlm_org              : organization name created at install time, "podman"
#                            deployment only                     (default: lab)
#   smlm_ssl_password     : password for the self-signed SSL cert `mgradm install`
#                            generates, "podman" deployment only  (default: same as
#                            smlm_admin_pass)
#
# OPTIONAL – Helm / release
#   smlm_version          : Helm chart version         (empty = latest, e.g. "5.2.0")
#   smlm_ns               : Kubernetes namespace       (default: uyuni-server)
#   smlm_rel              : Helm release name          (default: smlm-server)
#   smlm_registry         : OCI registry for the chart (default: registry.suse.com)
#   smlm_chart            : OCI chart path             (default: suse/multi-linux-manager/5.2/server-helm)
#   smlm_img_repository   : Image repository base      (default: derived from
#                           smlm_registry/smlm_chart + '/x86_64', e.g.
#                           registry.suse.com/suse/multi-linux-manager/5.2/x86_64)
#   smlm_img_tag          : Image tag for all images   (default: chart default, "latest")
#
# OPTIONAL – networking / ingress
#   smlm_shorthn          : Short hostname for DNS entry (default: smlm)
#   smlm_ingress_class    : Ingress class name         (default: traefik)
#                           NOTE: nginx is not supported by the SMLM chart; use traefik.
#
# OPTIONAL – security
#   smlm_super_privileged : Set to "true" to run in super-privileged mode
#                           (default: false — uses AppArmor/SELinux profiles)
#
# OPTIONAL – storage
#   smlm_storage_class    : StorageClass name          (empty = cluster default)
#   smlm_lh_overprovision : Longhorn storage-over-provisioning percentage set
#                           when smlm_storage_class is "longhorn" (default: 500)
#
# EXPERIMENTAL – HA database
#   smlm_db_ha            : "true" replaces the chart's single-pod PostgreSQL
#                           with an HA cluster managed by the CloudNativePG
#                           operator (default: false). The bundled db is
#                           disabled (db.enable=false) and the 'db'/'reportdb'
#                           hostnames become aliases of the operator's
#                           failover-aware primary Service (smlm-db-rw), so a
#                           failed DB pod/host is replaced by a standby with
#                           no corruption. Real host-level HA needs a kcluster
#                           with at least as many nodes as replicas.
#   smlm_db_ha_replicas   : Number of PostgreSQL instances   (default: 3)
#   smlm_db_ha_sync       : "true" (default) = synchronous replication: a
#                           primary failure loses no committed transaction,
#                           but writes stall while no standby is available.
#                           "false" = async (may lose the last commits).
#   smlm_db_ha_size       : Data volume size per instance    (default: 50Gi)
#   smlm_db_ha_pg_image   : PostgreSQL image (default:
#                           ghcr.io/cloudnative-pg/postgresql:18 — the major
#                           version must match SMLM 5.2's PostgreSQL 18)
#   smlm_db_ha_cnpg_version : CloudNativePG operator chart version (default: latest)
#
#   Failover test: after the lab is deployed, run
#       install_smlm.py <lab.json> --test-failover
#   It writes a canary row through the same path uyuni uses, deletes the
#   current primary pod, measures promotion and write-recovery time, and
#   verifies no committed row was lost and the web UI stayed up. For a harder
#   test power off the VM of the primary's node on the hypervisor instead.
#
# OPTIONAL – activation key (created after install, once the server is up;
# skipped entirely if smlm_activation_key is unset). Command syntax verified
# against documentation.suse.com/multi-linux-manager live docs (2026-08-27);
# NOT live-tested against a real server — see libs/spacecmd_common.py.
#   smlm_activation_key       : Activation key name                (default: unset — skipped)
#   smlm_activation_key_desc  : Description                        (default: same as key name)
#   smlm_activation_key_base_channel : Base channel label — required if smlm_activation_key is set
#   smlm_activation_key_child_channels : Space-separated child channel labels to add
#   smlm_activation_key_universal_default : "true" to mark this key as the org's universal
#                             default                               (default: false)
#   smlm_activation_key_entitlements : Comma-separated entitlements, e.g.
#                             "enterprise_entitled,virtualization_host"
#   smlm_activation_key_contact_method : Contact method to set on the key
#   smlm_activation_key_config_channels : Space-separated config channel labels to add
#   smlm_activation_key_enable_config_deployment : "true" to enable config-file deployment
#                             on the key                            (default: false)
#   smlm_activation_key_groups : Space-separated system group names to add
#   smlm_activation_key_appstreams : Space-separated "module:stream" pairs to enable on the key
#                             (e.g. "nodejs:20 postgresql:16"), via spacecmd's 'api' passthrough
#                             calling activationkey.addAppStreams — applied on every run, not just
#                             at key-creation time (idempotent: an already-enabled module is
#                             detected from the server's own error and skipped, since there is no
#                             list API for this — see libs/spacecmd_common.py)
#   smlm_activation_key_packages : Space-separated package names to add to the key (name-only, no
#                             arch qualification — see libs/spacecmd_common.py), via spacecmd's
#                             native activationkey_addpackages — applied on every run, not just at
#                             key-creation time (idempotent: diffs against
#                             activationkey_listpackages first and only adds what's missing)
#   smlm_sync_channels        : Space-separated software channel labels to ensure are synced
#                             (each via 'mgr-sync add channel <label>' if not already present in
#                             'spacecmd softwarechannel_list') before the activation key is created
#
# OPTIONAL – config channels (created/updated before the activation key above, so
# smlm_activation_key_config_channels can reference them). List of objects:
#   smlm_config_channels      : [{
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
#                             smlm_activation_key_config_channels for newly-registered clients.
#
# OPTIONAL – organizations (created after the above; each org gets its own admin session
# for its own scoped provisioning). List of objects:
#   smlm_orgs                 : [{
#                                 "name": "...", "admin_user": "...", "admin_pass": "...",
#                                 "admin_email": "...", "admin_first_name": "...",
#                                 "admin_last_name": "...", "prefix": "...", "pam": false,
#                                 "trust_with": ["other-org-name", ...],
#                                 "share_channels": ["channel-label", ...],
#                                 "share_channels_access": "protected" | "public" | "private",
#                                 # plus this org's OWN smlm_activation_key*/smlm_config_channels
#                                 # keys, same field names as above — reused as-is since once
#                                 # this org's admin session is active, activation keys/config
#                                 # channels are automatically scoped to it (hard-partitioned
#                                 # per org server-side)
#                                 "smlm_activation_key": "...", "smlm_config_channels": [...],
#                                 "smlm_access_groups": [...]   # see below — also reused per-org
#                               }, ...]
#                             admin_user/admin_pass/admin_email are required to create the org
#                             (skipped — not idempotent-creatable — otherwise). trust_with names
#                             other orgs (e.g. the one this server bootstrapped with) to establish
#                             channel-sharing trust with; share_channels additionally marks
#                             channels THIS org owns as shared (via the raw
#                             channel.access.setOrgSharing API — spacecmd has no subcommand for
#                             it) so a trusted org's activation keys can reference them. See
#                             libs/spacecmd_common.py for what's confirmed vs. inferred here
#                             (trust's bidirectionality in particular).
#
# OPTIONAL – user accounts. List of objects, usable at the top level (scoped to the default
# org) or nested inside an smlm_orgs entry (scoped to that org — same field name either way).
# Runs automatically on every install (idempotent), BEFORE smlm_access_groups below so its own
# "users" list can reference an account defined here:
#   smlm_users                 : [{
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
#                             group's own label here: smlm_users runs BEFORE smlm_access_groups
#                             below (an access group's own "users" list needs the account to
#                             already exist), so the custom role wouldn't exist yet and
#                             user_addrole would fail. Attach a user to a custom group the other
#                             way instead — list their username in that group's own "users" field
#                             below, which runs in the correct order.
#
# OPTIONAL – autoinstall trees ("Kickstart Distributions") and Kickstart/AutoYaST profiles.
# Distributions run automatically on every install (idempotent), BEFORE smlm_activation_key* so a
# kickstart profile below can reference one; profiles run AFTER activation keys, so
# activation_keys entries can link to a real key. See libs/spacecmd_common.py's
# ensure_distribution()/ensure_kickstart_profile() for the full real-server behavior confirmed
# live 2026-09-16 (most importantly: distribution_create itself VALIDATES that `path` already
# contains a real, extracted installer tree — this module cannot create or upload one):
#   smlm_distributions         : [{
#                                 "name": "...", "path": "/srv/www/htdocs/pub/install-trees/...",
#                                 "base_channel": "...", "install_type": "sles15generic"
#                               }, ...]
#                             `path` must already exist on the SERVER's own filesystem with a
#                             real extracted product ISO/installer tree under it (e.g. mount the
#                             ISO with `mount -o loop` and copy/rsync it there out of band first)
#                             — distribution_create dies with a clear "initrd could not be found"
#                             error otherwise. `install_type` is one of the labels
#                             `distribution_create --help` lists on the target server (changes per
#                             SMLM/Uyuni release — e.g. sles15generic, sles16generic, rhel_9,
#                             generic_rpm).
#   smlm_kickstart_profiles    : [{
#                                 "name": "...", "distribution": "...", "root_password": "...",
#                                 "virt_type": "none",   # default; or para_host/qemu/xenfv/xenpv
#                                 "variables": {"key": "value", ...},
#                                 "activation_keys": ["..."], "child_channels": ["..."]
#                               }, ...]
#                             `distribution` is a NAME REFERENCE into smlm_distributions above
#                             (define it there, not inline here). root_password is ONLY used at
#                             creation time — spacecmd hashes it server-side and there is no API
#                             to read or change it back on a repeat run, so a changed password
#                             needs the profile deleted and recreated. variables/activation_keys/
#                             child_channels are all applied idempotently on every run (diffed
#                             against the server's own current list first).
#
# OPTIONAL – server self-monitoring (Admin -> Manager Configuration -> Monitoring in the Web UI).
# Confirmed live 2026-09-16 against documentation.suse.com/suma/5.2's own Monitoring guide AND the
# real AdminMonitoringHandler.java source: this enables the node/tomcat/postgres/taskomatic
# exporters ALREADY BUNDLED in the server image (a pure on/off toggle, takes no arguments of its
# own) — it does NOT point the server at an external Prometheus. Uyuni's monitoring model is
# pull-based: an EXTERNAL Prometheus (e.g. the "prometheus" addon, install_prometheus.py) scrapes
# THIS server's own exposed exporter ports; the server never pushes to one.
#   smlm_monitoring_enabled    : "true" to enable (default: unset/false, no-op). Idempotent —
#                             checked against the server's own admin.monitoring.getStatus first.
#                             Restarts Tomcat/Taskomatic ONLY on the disabled->enabled transition
#                             (required per the official docs for the exporters to actually start
#                             listening — confirmed live), never on an already-enabled server.
#                             Real exporter ports to open on this server's firewall/AWS security
#                             group (aws_open_ports) for a remote Prometheus to reach it: 9100
#                             (node), 9187 (postgres), 5556 (tomcat JMX), 5557 (taskomatic JMX),
#                             9800 (taskomatic direct) — plus the existing web port (80/443) for
#                             the message-queue job at metrics path /rhn/metrics (confirmed live,
#                             same doc page).
#
# OPTIONAL – image management (Images -> Stores/Profiles/Build/Import in the Web UI). API-only —
# confirmed live 2026-09-16 that spacecmd has NO native subcommand for any of this; every call goes
# through the raw 'api' passthrough against image.store.*/image.profile.*/image.* (three separate
# handler classes — see libs/spacecmd_common.py). Stores run automatically on every install
# (idempotent), BEFORE profiles below (a profile references a store by label):
#   smlm_image_stores          : [{
#                                 "label": "...", "uri": "registry.suse.com",
#                                 "type": "registry" | "os_image",
#                                 "username": "...", "password": "..."   # both optional — omit
#                                                                        # for a public registry
#                               }, ...]
#                             `type` must be one of the labels the target server's own
#                             image.store.listImageStoreTypes returns ("registry"/"os_image" on a
#                             stock SMLM 5.2 server, confirmed live — re-check on other versions).
#                             registry.suse.com needs no credentials at all for SUSE's own public
#                             images (confirmed live).
#   smlm_image_profiles        : [{
#                                 "label": "...", "type": "dockerfile" | "kiwi",
#                                 "store": "...",    # NAME REFERENCE into smlm_image_stores above
#                                 "path": "https://github.com/USER/project.git#branch:folder",
#                                 "activation_key": "..."   # mandatory per the official docs —
#                                                            # determines which channels the
#                                                            # build/import can see
#                               }, ...]
#   smlm_image_imports         : [{
#                                 "name": "...", "version": "latest", "store": "...",
#                                 "build_host_id": 1000010001,   # NUMERIC Uyuni system id of an
#                                                                 # already-registered system with
#                                                                 # the "Container Build Host"
#                                                                 # entitlement enabled — this
#                                                                 # module cannot enable that
#                                                                 # itself; findable via
#                                                                 # 'spacecmd system_list'
#                                 "activation_key": "..."        # optional
#                               }, ...]
#                             Run with:  install_smlm.py <lab.json> --import-images
#                             (never runs automatically — scheduling an import is not idempotent,
#                             same reasoning as --run-ansible-playbooks/--run-clm-actions).
#
# OPTIONAL – RBAC / custom "User Access Groups" (API-only feature, Uyuni 2025.05+ / SMLM 5.1+).
# List of objects, usable at the top level (scoped to the default org) or nested inside an
# smlm_orgs entry (scoped to that org — same field name either way):
#   smlm_access_groups        : [{
#                                 "label": "...", "description": "...",
#                                 "permissions_from": ["existing-role-label", ...],
#                                 "permissions": [{"namespace": "...", "mode": "R" | "W"}, ...],
#                                 "users": ["username", ...]
#                               }, ...]
#                             Each username must exist by the time this runs — defined above via
#                             smlm_users, or an org's own admin_user, or attaching the role fails
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
#   smlm_ansible_paths        : [{"control_node_id": 1000010001, "type": "playbook" | "inventory",
#                                  "path": "/srv/ansible/playbooks"}, ...]
#                             control_node_id is the target's NUMERIC Uyuni system ID (findable via
#                             'spacecmd system_list' or the Web UI) — no name-based resolution is
#                             provided here.
#   smlm_ansible_playbooks    : [{"control_node_id": 1000010001,
#                                  "playbook_path": "/srv/ansible/playbooks/site.yml",
#                                  "inventory_path": "/srv/ansible/inventory/hosts",
#                                  "earliest": "2026-08-27T12:00:00",   # optional, default: now
#                                  "action_chain_label": "...",         # optional
#                                  "test_mode": false, "extra_vars": "...", "flush_cache": false
#                                }, ...]
#                             Run with:  install_smlm.py <lab.json> --run-ansible-playbooks
#                             (never runs automatically). See libs/spacecmd_common.py for the
#                             dateTime-encoding and orchestration-only-model details.
#
# OPTIONAL – Content Lifecycle Management (CLM). Project/source/filter/environment DEFINITION
# runs automatically on every install (idempotent, in this order so activation keys etc. can
# reference the environments); BUILD/PROMOTE are a SEPARATE, explicit trigger — see
# "--run-clm-actions" below — since each call triggers real, non-idempotent background work:
#   smlm_content_projects     : [{
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
#   smlm_content_lifecycle_actions : [
#                                 {"project": "...", "action": "build", "message": "...",
#                                  "wait": true, "wait_env": "dev", "wait_timeout": 1800},
#                                 {"project": "...", "action": "promote", "from_env": "dev",
#                                  "wait": true, "wait_env": "test"}
#                               ]
#                             "from_env" on a promote is the stage being promoted FROM, not the
#                             destination — the server determines the successor itself (confirmed
#                             from source; the admin-guide prose is ambiguous about this). "wait"
#                             polls the named environment's status until built/failed. Run with:
#                             install_smlm.py <lab.json> --run-clm-actions (never automatic).
#
# OPTIONAL – SCAP compliance auditing (legacy pre-staged-file model only — spacecmd's native
# scap_* commands don't cover SMLM 5.2's newer "centralized policies" Technology Preview layer,
# deliberately not automated here, see libs/spacecmd_common.py). Orchestration only: xccdf_path
# (and the OpenSCAP scanner + SCAP Security Guide content) must already exist on the target
# system. Explicit trigger only — see "--run-scap-scans" below:
#   smlm_scap_scans           : [{"system": "web1.mydemo.lab",
#                                  "xccdf_path": "/usr/share/openscap/scap-security-xccdf.xml",
#                                  "profile": "Web-Default"}, ...]
#                             Heuristically idempotent (skips a system already scanned against the
#                             same xccdf_path — path only, not path+profile). Run with:
#                             install_smlm.py <lab.json> --run-scap-scans (never automatic).
#
# OPTIONAL – CVE/OVAL audit (fully supported since SMLM 5.2). Pure read-only query, no JSON
# config — run with:
#   install_smlm.py <lab.json> --cve-audit CVE-YYYY-NNNNN
# prints every system's patch status for that CVE (AFFECTED_PATCH_INAPPLICABLE/
# AFFECTED_PATCH_APPLICABLE/NOT_AFFECTED/PATCHED).
#
# OPTIONAL – dev/QA/prod environment topology. A THIN COMPOSITION layer over the primitives
# above plus system groups/tags — Uyuni itself has no native "environment" or "release" object
# tying these together (see libs/spacecmd_common.py). All idempotent and automatic EXCEPT
# recurring_schedule (explicit trigger only — see "--run-recurring-schedules" below):
#   smlm_activation_keys      : [{...}, ...]   # same field names as smlm_activation_key* above,
#                             one dict per key — lets you define MULTIPLE named keys (e.g. one per
#                             environment) without needing a separate org per key
#   smlm_system_groups        : [{"name": "...", "description": "...",
#                                  "systems": ["existing-system-name", ...]}, ...]
#   smlm_custom_info_keys     : [{"name": "...", "description": "..."}, ...]
#                             Org-level key definitions — required before smlm_system_tags/
#                             environments' custom_info_tags can set any value for that key.
#   smlm_system_tags          : [{"system": "...", "tags": {"key": "value", ...}}, ...]
#                             Uyuni has no first-class "tag" object — this sets
#                             system.custominfo key/value pairs, the closest real mechanism.
#   smlm_environments         : [{
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
#                             install_smlm.py <lab.json> --run-recurring-schedules (never automatic
#                             — recurring-action idempotency was never confirmed).
#
# READ-ONLY — export a live server's own current configuration back into JSON shaped like
# this "smlm" section (channels, activation keys, system groups, the calling admin's own org's
# custom access groups, and a best-effort username list for every OTHER org), instead of
# installing anything. Never runs automatically. Prints to stdout, or writes to a file:
#   install_smlm.py <lab.json> --export-config [output.json]
# Real, confirmed limits (see libs/spacecmd_common.py's export_config()/describe_access_groups()
# for the full detail): passwords can never be recovered (one-way hashed server-side) — every
# smlm_orgs entry's admin_pass and any smlm_users entry's password must be filled in by hand
# before the result is usable to actually recreate that org/its users elsewhere. A custom access
# group's own member list is only exportable for the CALLING session's own org — user.getDetails
# and access.listRoles are both hard org-scoped, even for a satellite_admin (same constraint
# ensure_user_role()'s own docstring documents on the write side).
#
# NOTE: RKE2 (default) or K3s, with Traefik. On RKE2, Traefik is enabled
#       through the 'ingress-controller' option and the extra TCP ports 4505,
#       4506 (Salt) and 5432 (report DB) are exposed via a rke2-traefik
#       HelmChartConfig. On K3s (kclusters clu_type "k3s") the bundled Traefik
#       is reused and the same ports are exposed through its ServiceLB.

__version__ = "39753e7"

PLUGIN = {
    "name": "smlm",
    # "container"/"kubernetes" is the original Helm-chart deployment (smlm_deployment
    # unset or "kubernetes", the default — unchanged). "vm"/"baremetal"/"standalone-
    # container" is the traditional mgradm/podman deployment (smlm_deployment: "podman",
    # added 2026-09-11 — see setup_smlm_podman()'s own docstring) — the two modes are
    # dispatched completely differently in main() below, so both target shapes are
    # listed here rather than picking one.
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "standalone-container"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import json
import os
import shlex
import subprocess
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
from lab_creation import setup_helm, ssh_run, ssh_output, add_service_dns, check_ssh_conn, reboot_vm, die, log  # noqa: E402


def _validate(v):
    cfg = v.definition.get("smlm", {}) or {}
    if (cfg.get("smlm_deployment") or "kubernetes") == "podman":
        # Traditional mgradm/podman deployment (see setup_smlm_podman()) — no
        # Kubernetes cluster/fqdn-for-ingress fields apply here at all.
        # smlm_scc_product has a real, confirmed default (see the JSON
        # section comment above) so it's not required here; smlm_scc_regcode
        # has no possible default (per-customer) and always is.
        # smlm_scc_user/smlm_scc_password are recommended (needed for
        # `podman login registry.suse.com`) but not unconditionally
        # required — setup_smlm_podman() degrades to relying on SUSEConnect
        # alone if they're absent, per the official docs' own framing of the
        # registry login as a fallback, not a strict prerequisite. They
        # BECOME required the moment smlm_channels/smlm_activation_keys are
        # also set, since mgr-sync has no other way to learn which channels
        # are entitled (see setup_smlm_podman()'s own die() for the runtime
        # version of this same check).
        v.vreq("smlm", "smlm_scc_regcode")
        if cfg.get("smlm_channels") or cfg.get("smlm_activation_keys"):
            v.vreq("smlm", "smlm_scc_user")
            v.vreq("smlm", "smlm_scc_password")
    else:
        v.vreq("smlm", "smlm_fqdn")
        v.vreq("smlm", "smlm_scc_user")
        v.vreq("smlm", "smlm_scc_password")
    v.vns("smlm")
    v.vver("smlm")
    v.vbool("smlm", "smlm_super_privileged")
    v.vbool("smlm", "smlm_db_ha")
    v.vbool("smlm", "smlm_db_ha_sync")
    v.vport("smlm", "smlm_db_ha_replicas")


# ─── Traditional (mgradm/podman) deployment — added 2026-09-11 ─────────────
# User request: install_uyuni.py must refer ONLY to the open-source Uyuni
# project; a genuine SMLM install needs its own real deployment path, not
# borrowed Uyuni branding — per documentation.suse.com/multi-linux-manager/
# 5.2's own container-deployment/mlm/ vs container-deployment/uyuni/ page
# split (both exist as parallel, officially documented install guides for
# the SAME mgradm/podman tool, just sourced from different repos/registries).

def setup_smlm_podman(hostname, virt_srv, cfg):
    """
    Install SUSE Multi-Linux Manager the traditional way: mgradm/podman
    directly on a dedicated host/VM, no Kubernetes at all. Reuses
    libs/mgradm_common.py's proven mgradm-install/container-health-wait
    helpers (run_install_with_pg_hba_guard/ensure_server_container_active —
    shared with install_uyuni.py, not duplicated, since that logic works
    around a real, subtly-timed upstream mgradm/podman/postgres-image race
    condition confirmed live 2026-08-28; moved to libs/ 2026-09-12 after a
    real deployed-environment failure — `from install_uyuni import ...`
    raised ModuleNotFoundError once install_uyuni.py was actually deployed
    without its .py suffix, confirmed live running setup_lab.py for real —
    see mgradm_common.py's own docstring) — the underlying tool and
    container mechanics are identical between Uyuni and SMLM; what genuinely
    differs is where mgradm/mgrctl and the server's own container images
    come from.

    install_uyuni.py adds Uyuni's own free community OBS repo — no
    entitlement needed, by design (open source). A real SMLM install instead
    needs the host registered against SCC with the actual SUSE Multi-Linux
    Manager module, so mgradm/mgrctl — and the entitled images mgradm itself
    pulls later from registry.suse.com — are the real, licensed product, not
    the community build. Confirmed against documentation.suse.com/
    multi-linux-manager/5.2's own server-deployment guide (quoted directly,
    2026-09-12 — this corrected an earlier version of this function that
    only did the SUSEConnect step and neither installed podman on a plain
    SLES base nor logged into the image registry, both of which the real
    docs show as necessary):
      - smlm_scc_regcode : the SCC registration code for the base SLES/SL
                            Micro product (`SUSEConnect -r <code>`) — this
                            is NOT a separate "subscription ID": the regcode
                            itself is what identifies which subscription to
                            activate against, there's no other identifier.
                            ALSO passed to the module registration below
                            (`-p <product> -r <code>`, confirmed from docs —
                            an earlier version of this code omitted -r there).
      - smlm_scc_product : the exact SCC extension-product identifier for
                            the "SUSE Multi-Linux Manager" module. Defaults
                            to "Multi-Linux-Manager-Server-SLE/5.2/x86_64"
                            (quoted directly from the official docs, not
                            guessed) if unset — override if a future GA
                            release renames it.
      - smlm_scc_user/smlm_scc_password : SCC account credentials (NOT the
                            regcode) for `podman login registry.suse.com` —
                            the docs' own troubleshooting section shows this
                            as needed when the image pull isn't already
                            authorized through the SUSEConnect registration
                            alone. Optional here (skipped with a warning if
                            unset) since the docs frame it as a fallback,
                            not always strictly required.
      - On plain SLES 15 SP7 (not SL Micro), podman is NOT preinstalled and
        needs its own free module (`SUSEConnect -p sle-module-containers/
        15.7/x86_64`) plus `zypper in podman` + enabling the podman socket —
        confirmed from the same docs. SL Micro ships podman by default, same
        assumption install_uyuni.py's own (Micro-only) code already made.

    NOT live-tested (no real SCC registration code available in this
    project's dev/CI environment) — the mgradm/podman install portion itself
    reuses install_uyuni.py's own live-tested mechanism verbatim; everything
    else in this function is new and unverified against a real server.
    """
    from mgradm_common import run_install_with_pg_hba_guard, ensure_server_container_active

    regcode = cfg.get("smlm_scc_regcode")
    product = cfg.get("smlm_scc_product") or "Multi-Linux-Manager-Server-SLE/5.2/x86_64"
    email = cfg.get("smlm_email") or "admin@lab.local"

    print("- Registering the host with SCC")
    ssh_run(hostname, "SUSEConnect -r {}".format(shlex.quote(regcode)), check=False)
    # sle-module-containers MUST be registered before the SMLM extension
    # itself — confirmed live 2026-09-13: SCC's own registration server
    # rejects the SMLM module outright ("requires one of these products to
    # be activated first: Containers Module 15 SP7 x86_64", HTTP 422) if
    # attempted first. An earlier version of this function registered the
    # containers module later, only in the plain-SLES package-install
    # branch below (where it's ALSO needed, for podman itself) — too late
    # for this dependency check, which happens regardless of base OS. Free
    # module, no regcode needed, same as the official docs' own example.
    ssh_run(hostname, "SUSEConnect -p sle-module-containers/15.7/x86_64", check=False)
    r = ssh_run(hostname, "SUSEConnect -p {} -r {}".format(shlex.quote(product), shlex.quote(regcode)),
                check=False)
    if r.returncode != 0:
        die("could not register the SUSE Multi-Linux Manager module ('{}') on '{}' via SUSEConnect "
            "— confirm smlm_scc_product is the real product identifier (see setup_smlm_podman()'s "
            "own docstring for how to find it)".format(product, hostname))

    scc_user = cfg.get("smlm_scc_user")
    scc_password = cfg.get("smlm_scc_password")
    if scc_user and scc_password:
        # Separate from the SUSEConnect registration above: mgradm pulls
        # SMLM's entitled container images from registry.suse.com, which
        # needs its own podman login, per the docs' own troubleshooting
        # section (see this function's own docstring).
        print("- Logging into registry.suse.com")
        ssh_run(hostname, "echo {} | podman login -u {} --password-stdin registry.suse.com".format(
            shlex.quote(scc_password), shlex.quote(scc_user)), check=False)
    else:
        print("- smlm_scc_user/smlm_scc_password not set — skipping podman login to "
              "registry.suse.com; relying on SUSEConnect registration alone to authorize "
              "the image pull (see setup_smlm_podman()'s own docstring)")

    print("- Installing mgradm tooling")
    pkgs = "mgradm mgradm-bash-completion mgrctl mgrctl-bash-completion uyuni-storage-setup-server"
    is_transactional = ssh_run(hostname, "command -v transactional-update", check=False).returncode == 0
    if is_transactional:
        # SL Micro base — ships podman by default; package changes land in a
        # new snapshot that only takes effect after a reboot, same as
        # install_uyuni.py's own (Micro-only) assumption.
        ssh_run(hostname, "transactional-update --quiet pkg install -y {}".format(pkgs))
        reboot_vm(virt_srv, hostname)
        time.sleep(5)
        check_ssh_conn(hostname)
    else:
        # Plain SLES 15 SP7 base (the other officially-supported SMLM base) —
        # does NOT ship podman by default; needs an explicit podman
        # install/enable first (confirmed from the official docs). The
        # containers module itself is already registered above, before the
        # SMLM module registration attempt — no need to repeat it here.
        ssh_run(hostname, "zypper --non-interactive install -y podman")
        ssh_run(hostname, "systemctl enable --now podman.socket", check=False)
        ssh_run(hostname, "zypper --non-interactive install -y {}".format(pkgs))

    print("- Installing SUSE Multi-Linux Manager server")
    admin = cfg.get("smlm_admin_user") or "admin"
    password = cfg.get("smlm_admin_pass") or "Smlm12345"
    org = cfg.get("smlm_org") or "lab"
    # Same flag set as install_uyuni.py's own live-verified `mgradm install
    # podman ...` invocation (mgradm/podman mechanics are identical between
    # the two products) — see that script's own comment on why --admin-email
    # doesn't exist (it's the top-level --email flag instead). Every value
    # shell-quoted — confirmed live 2026-09-13: an unquoted multi-word
    # --organization ("SUSE Test") got split by the remote shell into
    # `--organization SUSE` plus a stray `Test` token, which mgradm then
    # misinterpreted as its own optional FQDN positional argument ("Test is
    # not a valid FQDN"). install_uyuni.py has this identical latent bug —
    # never touched here (still Uyuni-only, per the user's own instruction),
    # but its own uyuni_org just never happened to contain a space.
    install_cmd = (
        "mgradm install podman "
        "--admin-login {} "
        "--admin-password {} "
        "--email {} "
        "--ssl-password {} "
        "--organization {}".format(
            shlex.quote(admin), shlex.quote(password), shlex.quote(email),
            shlex.quote(cfg.get("smlm_ssl_password") or password), shlex.quote(org)))
    run_install_with_pg_hba_guard(hostname, install_cmd)

    time.sleep(60)
    ssh_run(hostname, "reboot", check=False)
    time.sleep(5)
    check_ssh_conn(hostname)
    ensure_server_container_active(hostname)

    print("SUSE Multi-Linux Manager available at: https://{}  ({} / {})".format(hostname, admin, password))

    # smlm_channels is a JSON array in every real lab definition (see the
    # JSON section's own docs), but this variable's "or ''" default and the
    # later plain .format(channels) both assumed a pre-joined string — a
    # real, confirmed-live 2026-09-14 bug: .format() on a Python list just
    # stringifies its repr ("['a', 'b']"), producing ONE malformed shell
    # argument instead of space-separated channel labels, so `mgr-sync add
    # channels` was never actually invoked correctly in any run before now.
    # Accept either shape (a list, the real-world case, or a pre-joined
    # string, kept for backward compatibility) and always build the actual
    # command from a normalized list.
    channels = cfg.get("smlm_channels") or []
    if isinstance(channels, str):
        channels = channels.split()

    # Real bug found live 2026-09-14: an activation key's own
    # *_activation_key_child_channels (e.g. the "managertools-*" channels
    # that actually provide venv-salt-minion) are only ever REFERENCED by
    # ensure_activation_key()'s own activationkey_addchildchannels call —
    # nothing ever adds those channels to the server in the first place if
    # they're not also separately listed in smlm_channels. The link call
    # then fails with "Invalid channel" (previously silent — see
    # ensure_activation_key()'s own fix in spacecmd_common.py), and every
    # client bootstrapped against that key gets the wrong tooling package
    # with no visible error anywhere: confirmed live, this is why a real
    # SLES15 client kept getting classic salt-minion instead of SUSE's own
    # venv-salt-minion (the bootstrap script's own venv-enabled marker file
    # 404s until its owning channel is actually synced), and the SMLM
    # server's hardened salt-master then rejected it outright ("protocol
    # version 2, minimum required 3"). Fold every activation key's own
    # child channels into what actually gets synced, deduplicated against
    # smlm_channels, so this can't happen silently again.
    for key_cfg in ([cfg] + list(cfg.get("smlm_activation_keys") or [])):
        for c in (key_cfg.get("smlm_activation_key_child_channels") or "").split():
            if c not in channels:
                channels.append(c)

    if channels or cfg.get("smlm_activation_keys"):
        # Unlike the podman-registry login above (a fallback the docs frame
        # as optional), this step IS required: mgr-sync has no visibility
        # into which channels/products are entitled until the server's own
        # SCC "organization credentials" (mirror credentials) are registered
        # via `mgr-sync add credentials` — confirmed real command (Uyuni's
        # own cli-sync reference: `add` covers "channels, organization
        # credentials, or products").
        #
        # Confirmed live 2026-09-14 (real SMLM 5.2 server) the actual
        # non-interactive prompt shape, which this project's earlier guesses
        # (SCC user/password only, then a 4-line admin+SCC-pair guess) both
        # got wrong: `mgr-sync add credentials` asks for FIVE lines total —
        # first a Login/Password pair for the server's own local admin
        # account (smlm_admin/smlm_password, the account `mgradm install`
        # just created; this round is printed under a "Please enter the
        # credentials of SUSE Multi-Linux Manager Administrator" banner),
        # THEN three more prompts for the real SCC mirror credentials:
        # "User to add:", "Password to add:", and "Confirm password:" (the
        # SCC password a second time). Feeding fewer lines left a later
        # prompt waiting forever and the whole call died silently — a
        # local-admin-only 2-line feed died with "General error: EOF when
        # reading a line" at the SCC "User to add:" prompt; a 4-line feed
        # (missing the confirmation) died with a bare "General error:" at
        # "Confirm password:" — both silent, unnoticed no-ops under this
        # function's own check=False. Verified live: this exact 5-line
        # sequence gets "Successfully added credentials."
        if not (scc_user and scc_password):
            die("smlm_channels/smlm_activation_keys are set but smlm_scc_user/smlm_scc_password "
                "are not — mgr-sync cannot see any entitled channels without the SCC organization "
                "credentials registered on '{}' first (`mgr-sync add credentials`)".format(hostname))
        print("- Registering SCC organization (mirror) credentials with mgr-sync")
        # -i is required: confirmed live (libs/spacecmd_common.py's own
        # _run() docstring, 2026-08-28) that `mgrctl exec` does NOT forward
        # stdin unless given -i explicitly — omitting it here would silently
        # send this input_text nowhere instead of erroring.
        ssh_run(hostname, "mgrctl exec -i -- mgr-sync add credentials",
                input_text="{}\n{}\n{}\n{}\n{}\n".format(admin, password, scc_user, scc_password, scc_password),
                check=False)

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
        channel_args = " ".join(shlex.quote(c) for c in channels)
        ssh_run(hostname, "mgrctl exec -- mgr-sync add channels {}".format(channel_args))
        ensure_channel_sync_monitor(hostname, admin, password)

    sync_channels = (cfg.get("smlm_sync_channels") or "").split()
    config_channels = cfg.get("smlm_config_channels") or []
    orgs = cfg.get("smlm_orgs") or []
    access_groups = cfg.get("smlm_access_groups") or []
    ansible_paths = cfg.get("smlm_ansible_paths") or []
    content_projects = cfg.get("smlm_content_projects") or []
    activation_keys = cfg.get("smlm_activation_keys") or []
    system_groups = cfg.get("smlm_system_groups") or []
    custom_info_keys = cfg.get("smlm_custom_info_keys") or []
    system_tags = cfg.get("smlm_system_tags") or []
    environments = cfg.get("smlm_environments") or []
    distributions = cfg.get("smlm_distributions") or []
    kickstart_profiles = cfg.get("smlm_kickstart_profiles") or []
    image_stores = cfg.get("smlm_image_stores") or []
    image_profiles = cfg.get("smlm_image_profiles") or []
    if (cfg.get("smlm_activation_key") or sync_channels or config_channels or orgs
            or access_groups or ansible_paths or content_projects or activation_keys
            or system_groups or custom_info_keys or system_tags or environments
            or distributions or kickstart_profiles or image_stores or image_profiles
            or cfg.get("smlm_monitoring_enabled")):
        exec_prefix = "mgrctl exec --"
        sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
        sc.ensure_channels_synced(hostname, exec_prefix, sync_channels)
        sc.ensure_config_channels(hostname, exec_prefix, cfg, "smlm")
        # System groups BEFORE any activation key: ensure_activation_key()/
        # ensure_activation_keys() link a key to <prefix>_activation_key_groups
        # via activationkey_addgroups, which dies if the named group doesn't
        # exist yet server-side — confirmed live 2026-09-15 ("Unable to locate
        # or access server group: 'prod'") the first time a lab actually
        # combined smlm_system_groups with smlm_activation_key_groups.
        sc.ensure_monitoring(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_system_groups(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_distributions(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_image_stores(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_image_profiles(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_key(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_appstreams(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_key_packages(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_keys(hostname, exec_prefix, cfg, "smlm")
        # Kickstart profiles AFTER activation keys: a profile can link to one
        # via kickstart_addactivationkeys, which needs the key to already exist
        # — same ordering reasoning as system groups vs activation keys above.
        sc.ensure_kickstart_profiles(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_users(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_access_groups(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_ansible_paths(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_content_projects(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_custom_info_keys(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_system_tags(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_environments(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_orgs(hostname, exec_prefix, cfg, "smlm", admin, password)


_CHANNEL_SYNC_MONITOR_SCRIPT = """#!/bin/bash
# Installed by lab-in-a-box's install_smlm.py (ensure_channel_sync_monitor) —
# checks every software channel present on this SMLM server for a clean,
# completed reposync, and re-triggers any that never synced, errored, or
# were left interrupted by something like a mid-flight server restart. Runs
# periodically via smlm-channel-sync-monitor.timer (see the matching
# .service unit next to this file).
#
# Triggers AT MOST ONE resync per run — confirmed live 2026-09-14:
# spacewalk-repo-sync only ever allows a single instance system-wide
# ("attempting to run more than one instance... Exiting"), and taskomatic
# does not automatically retry a collision. Triggering every pending
# channel each run (the original behavior) caused this project's own
# monitor to self-collide with itself: taskomatic tried to launch several
# at once, only one ever actually got the lock, and every other channel
# lost the race, got no log file, and sat untried for a full cycle since
# nothing else prompted a retry sooner. Firing one at a time, and only when
# nothing is already running, means every trigger this monitor issues has
# a real, uncontested chance to actually run.
set -uo pipefail

LOG_DIR=/var/log/rhn/reposync
LOGFILE=/var/log/smlm-channel-sync-monitor.log
ADMIN=__ADMIN__
PASSWORD=__PASSWORD__

log() {
    echo "$(date -Is) $*" >> "$LOGFILE"
}

spacecmd_() {
    podman exec uyuni-server spacecmd -u "$ADMIN" -p "$PASSWORD" -- "$@" 2>/dev/null
}

if podman exec uyuni-server pgrep -f spacewalk-repo-sync >/dev/null 2>&1; then
    log "a reposync is already running -- nothing to trigger this cycle"
    exit 0
fi

CHANNELS=$(spacecmd_ softwarechannel_list)
if [ -z "$CHANNELS" ]; then
    log "no software channels found on the server -- nothing to check"
    exit 0
fi

for channel in $CHANNELS; do
    reposync_log="$LOG_DIR/$channel.log"
    reason=""

    if ! podman exec uyuni-server test -f "$reposync_log"; then
        reason="never synced (no reposync log)"
    else
        tail_lines=$(podman exec uyuni-server tail -n 20 "$reposync_log" 2>/dev/null)
        if echo "$tail_lines" | tail -n 3 | grep -qF "Sync completed."; then
            continue
        elif echo "$tail_lines" | grep -qiE 'error|traceback'; then
            reason="last reposync log shows an error"
        elif ! podman exec uyuni-server pgrep -f "spacewalk-repo-sync --channel $channel " >/dev/null 2>&1; then
            reason="incomplete reposync log with no active sync process (interrupted)"
        fi
    fi

    if [ -n "$reason" ]; then
        log "channel '$channel': $reason -- triggering resync"
        spacecmd_ softwarechannel_syncrepos "$channel" >/dev/null
        exit 0
    fi
done
"""

_CHANNEL_SYNC_MONITOR_SERVICE = """[Unit]
Description=Check SMLM software channels for a failed/interrupted reposync and retry
After=uyuni-server.service
Wants=uyuni-server.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/smlm-channel-sync-monitor.sh
"""

_CHANNEL_SYNC_MONITOR_TIMER = """[Unit]
Description=Periodically check SMLM software channels for a failed/interrupted reposync

[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
"""


def ensure_channel_sync_monitor(hostname, admin, password):
    """
    Deploys a HOST-level (not container-internal) systemd service+timer that
    periodically checks every software channel present on the SMLM server
    for a clean, completed reposync, and re-triggers any that never synced,
    errored, or were left interrupted — confirmed live 2026-09-14 that a
    mid-flight `mgradm restart` orphaned exactly this kind of stuck,
    half-downloaded channel log (no "Sync completed." line, no error either,
    just abandoned) with nothing to notice or recover on its own.

    Deliberately host-level, not inside the uyuni-server container itself:
    that container runs with `--rm` and is fully recreated (fresh
    filesystem, `/etc` included — only the explicit named volumes survive)
    on every restart/upgrade, so anything installed inside it — including a
    systemd timer — would be silently lost the next time mgradm or systemd
    recycles it. The host's own systemd is not ephemeral, matching how
    uyuni-server.service/uyuni-db.service themselves already manage the
    container from outside it. The monitor script itself just reaches in via
    `podman exec uyuni-server ...` for every actual check/action, the same
    way this project's own live troubleshooting did tonight.

    Failure detection, per channel (matching each channel's own
    /var/log/rhn/reposync/<label>.log inside the container):
      - no log file at all -> never synced
      - last lines contain "error"/"traceback" -> failed
      - doesn't end with "Sync completed." AND no spacewalk-repo-sync
        process is currently running for that channel -> interrupted
      - otherwise (ends with "Sync completed.") -> healthy, left alone

    Re-trigger uses `spacecmd softwarechannel_syncrepos <label>` (confirmed
    live, real command, verified it actually resumes a stuck sync) rather
    than `mgr-sync sync channel <label>` — the latter needs the same
    fragile interactive multi-round credential prompt as `mgr-sync add
    credentials` (see that function's own docstring), unsafe to script
    unattended from a timer.
    """
    script = _CHANNEL_SYNC_MONITOR_SCRIPT.replace(
        "__ADMIN__", shlex.quote(admin)).replace("__PASSWORD__", shlex.quote(password))

    print("- Installing the channel-sync failure monitor (checks every 30 min)")
    ssh_run(hostname, "cat > /usr/local/sbin/smlm-channel-sync-monitor.sh <<'EOF'\n{}EOF".format(script),
            check=False)
    ssh_run(hostname, "chmod 755 /usr/local/sbin/smlm-channel-sync-monitor.sh", check=False)
    ssh_run(hostname, "cat > /etc/systemd/system/smlm-channel-sync-monitor.service <<'EOF'\n{}EOF".format(
        _CHANNEL_SYNC_MONITOR_SERVICE), check=False)
    ssh_run(hostname, "cat > /etc/systemd/system/smlm-channel-sync-monitor.timer <<'EOF'\n{}EOF".format(
        _CHANNEL_SYNC_MONITOR_TIMER), check=False)
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable --now smlm-channel-sync-monitor.timer",
            check=False)


# ─── Traefik configuration ───────────────────────────────────────────────────

def setup_smlm_traefik(hostname, clu_type):
    """Mirrors setup_smlm_traefik (bash)."""
    ports = ["salt-publish:4505", "salt-request:4506", "reportdb-pgsql:5432"]
    if clu_type == "k3s":
        k8s.setup_traefik_k3s(hostname, ports)
    else:
        k8s.setup_traefik_rke2(hostname, ports)


# ─── Namespace and secrets ───────────────────────────────────────────────────

def setup_smlm_prereqs(hostname, cfg):
    """Mirrors setup_smlm_prereqs (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")

    if (cfg.get("smlm_storage_class") or "") == "longhorn":
        k8s.set_longhorn_overprovisioning(hostname, cfg.get("smlm_lh_overprovision") or "500")

    print("# Creating namespace and secrets in '{}'".format(ns))
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(ns), check=False)

    ssh_run(hostname,
            "kubectl create secret docker-registry scc-credentials "
            "-n {} --docker-server={} --docker-username='{}' --docker-password='{}' "
            "--dry-run=client -o yaml | kubectl apply -f -".format(
                ns, cfg.get("smlm_registry") or "registry.suse.com", cfg.get("smlm_scc_user", ""),
                cfg.get("smlm_scc_password", "")))

    k8s.create_basic_auth_secret(hostname, ns, "db-admin-credentials",
                                  cfg.get("smlm_db_admin_user") or "mlmadmin",
                                  cfg.get("smlm_db_admin_pass") or "mlmadmin123")
    k8s.create_basic_auth_secret(hostname, ns, "db-credentials",
                                  cfg.get("smlm_db_user") or "mlmuser", cfg.get("smlm_db_pass") or "mlmuser123")
    k8s.create_basic_auth_secret(hostname, ns, "reportdb-credentials",
                                  cfg.get("smlm_reportdb_user") or "reportuser",
                                  cfg.get("smlm_reportdb_pass") or "reportuser123")
    k8s.create_basic_auth_secret(hostname, ns, "admin-credentials",
                                  cfg.get("smlm_admin_user") or "admin", cfg.get("smlm_admin_pass") or "admin123")

    print("  Generating self-signed TLS certificates")
    ssh_run(hostname, (
        "set -e\n"
        "_fqdn={fqdn}\n"
        "_ns={ns}\n"
        "_tmp=$(mktemp -d)\n"
        "\n"
        "openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \\\n"
        "    -keyout ${{_tmp}}/tls.key \\\n"
        "    -out    ${{_tmp}}/tls.crt \\\n"
        "    -subj \"/CN=${{_fqdn}}\" \\\n"
        "    -addext \"subjectAltName=DNS:${{_fqdn}}\" 2>/dev/null\n"
        "\n"
        "kubectl create secret tls uyuni-cert -n ${{_ns}} \\\n"
        "    --cert=${{_tmp}}/tls.crt \\\n"
        "    --key=${{_tmp}}/tls.key \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "kubectl create configmap uyuni-ca -n ${{_ns}} \\\n"
        "    --from-file=ca.crt=${{_tmp}}/tls.crt \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "kubectl create secret generic db-cert -n ${{_ns}} \\\n"
        "    --from-file=ca.crt=${{_tmp}}/tls.crt \\\n"
        "    --from-file=tls.crt=${{_tmp}}/tls.crt \\\n"
        "    --from-file=tls.key=${{_tmp}}/tls.key \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "kubectl create configmap db-ca -n ${{_ns}} \\\n"
        "    --from-file=ca.crt=${{_tmp}}/tls.crt \\\n"
        "    --dry-run=client -o yaml | kubectl apply -f -\n"
        "\n"
        "rm -rf ${{_tmp}}"
    # smlm_fqdn is free-text with no format validation at all — the hand-
    # rolled single quotes above (found in code review 2026-09-05) broke,
    # or could be injected through, this remote command the moment the
    # value contained an embedded single quote. shlex.quote() escapes
    # correctly even nested inside the surrounding heredoc.
    ).format(fqdn=shlex.quote(cfg.get("smlm_fqdn", "") or ""), ns=shlex.quote(ns)))


# ─── EXPERIMENTAL: HA database (CloudNativePG) ───────────────────────────────

def setup_smlm_db_ha(hostname, cfg):
    """Mirrors setup_smlm_db_ha (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    replicas = cfg.get("smlm_db_ha_replicas") or "3"

    k8s.setup_cnpg_operator(hostname, cfg.get("smlm_db_ha_cnpg_version") or None)

    print("# [experimental] Creating HA PostgreSQL cluster 'smlm-db' ({} instances)".format(replicas))

    sc_line = "    storageClass: {}".format(cfg["smlm_storage_class"]) if cfg.get("smlm_storage_class") else ""
    sync_block = ""
    if (cfg.get("smlm_db_ha_sync") or "true") == "true" and int(replicas) > 1:
        sync_block = "  postgresql:\n    synchronous:\n      method: any\n      number: 1"

    manifest = (
        "apiVersion: postgresql.cnpg.io/v1\n"
        "kind: Cluster\n"
        "metadata:\n"
        "  name: smlm-db\n"
        "  namespace: {ns}\n"
        "spec:\n"
        "  instances: {replicas}\n"
        "  imageName: {pg_image}\n"
        "  primaryUpdateStrategy: unsupervised\n"
        "  affinity:\n"
        "    enablePodAntiAffinity: true\n"
        "    topologyKey: kubernetes.io/hostname\n"
        "    podAntiAffinityType: preferred\n"
        "{sync_block}\n"
        "  storage:\n"
        "    size: {size}\n"
        "{sc_line}\n"
        "  bootstrap:\n"
        "    initdb:\n"
        "      postInitSQL:\n"
        "        - CREATE ROLE \"{db_admin_user}\" SUPERUSER LOGIN PASSWORD '{db_admin_pass}'\n"
        "        - CREATE ROLE \"{db_user}\" LOGIN PASSWORD '{db_pass}'\n"
        "        - CREATE ROLE \"{reportdb_user}\" LOGIN PASSWORD '{reportdb_pass}'\n"
        "        - CREATE DATABASE susemanager OWNER \"{db_user}\"\n"
        "        - CREATE DATABASE reportdb OWNER \"{reportdb_user}\"\n"
    ).format(
        ns=ns, replicas=replicas, pg_image=cfg.get("smlm_db_ha_pg_image") or "ghcr.io/cloudnative-pg/postgresql:18",
        sync_block=sync_block, size=cfg.get("smlm_db_ha_size") or "50Gi", sc_line=sc_line,
        db_admin_user=cfg.get("smlm_db_admin_user") or "mlmadmin",
        db_admin_pass=cfg.get("smlm_db_admin_pass") or "mlmadmin123",
        db_user=cfg.get("smlm_db_user") or "mlmuser", db_pass=cfg.get("smlm_db_pass") or "mlmuser123",
        reportdb_user=cfg.get("smlm_reportdb_user") or "reportuser",
        reportdb_pass=cfg.get("smlm_reportdb_pass") or "reportuser123",
    )
    ssh_run(hostname, "cat > /tmp/smlm-db-cluster.yaml", input_text=manifest)

    # Right after the operator install its admission webhook may not be
    # reachable yet — retry the apply
    applied = False
    for i in range(1, 11):
        r = ssh_run(hostname, "kubectl apply -f /tmp/smlm-db-cluster.yaml", check=False)
        if r.returncode == 0:
            applied = True
            break
        log("  CNPG webhook not ready yet, retrying ({}/10) …".format(i))
        time.sleep(15)
    if not applied:
        r = ssh_run(hostname, "kubectl get cluster smlm-db -n {}".format(ns), check=False, capture=True)
        if r.returncode != 0:
            die("could not create the smlm-db CNPG cluster")
    ssh_run(hostname, "rm -f /tmp/smlm-db-cluster.yaml", check=False)

    print("  Waiting for {} database instances to be ready …".format(replicas))
    ready = ""
    for _ in range(60):
        ready = ssh_output(hostname, "kubectl get cluster smlm-db -n {} -o jsonpath='{{.status.readyInstances}}' "
                                      "2>/dev/null".format(ns))
        if ready == str(replicas):
            break
        time.sleep(15)
    if ready != str(replicas):
        die("HA database not ready ({}/{}) — check: kubectl get cluster smlm-db -n {}".format(
            ready or "0", replicas, ns))

    r = ssh_run(hostname,
                "kubectl exec -n {ns} $(kubectl get pods -n {ns} "
                "-l cnpg.io/cluster=smlm-db,cnpg.io/instanceRole=primary -o name | head -1) "
                "-c postgres -- psql -d susemanager -tAc 'CREATE EXTENSION IF NOT EXISTS pg_trgm'".format(ns=ns),
                check=False)
    if r.returncode != 0:
        die("could not create the pg_trgm extension in susemanager")

    alias_manifest = (
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: db\n"
        "  namespace: {ns}\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: smlm-db-rw.{ns}.svc.cluster.local\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: reportdb\n"
        "  namespace: {ns}\n"
        "spec:\n"
        "  type: ExternalName\n"
        "  externalName: smlm-db-rw.{ns}.svc.cluster.local\n"
    ).format(ns=ns)
    r = ssh_run(hostname, "kubectl apply -f -", input_text=alias_manifest, check=False)
    if r.returncode != 0:
        die("could not create the db/reportdb Service aliases")


# ─── EXPERIMENTAL: HA database failover test (--test-failover) ──────────────

def _smlm_canary_sql(hostname, ns, sql, check=False, capture=False):
    """Mirrors _smlm_canary_sql (bash)."""
    return ssh_run(
        hostname,
        "kubectl exec -n {} deploy/uyuni -c uyuni -- sh -c "
        "'PGPASSWORD=$MANAGER_PASS psql -h $MANAGER_DB_HOST -p $MANAGER_DB_PORT "
        "-U $MANAGER_USER -d $MANAGER_DB_NAME -tAc \"{}\"'".format(ns, sql),
        check=check, capture=capture,
    )


def _smlm_db_primary(hostname, ns):
    """Mirrors _smlm_db_primary (bash). Returns (pod_name, node_name)."""
    out = ssh_output(hostname,
                      "kubectl get pods -n {} -l cnpg.io/cluster=smlm-db,cnpg.io/instanceRole=primary "
                      "-o jsonpath='{{.items[0].metadata.name}} {{.items[0].spec.nodeName}}' 2>/dev/null".format(ns))
    parts = out.split(None, 1)
    pod = parts[0] if len(parts) > 0 else ""
    node = parts[1] if len(parts) > 1 else ""
    return pod, node


def smlm_db_failover_test(hostname, cfg):
    """Mirrors smlm_db_failover_test (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    fqdn = cfg.get("smlm_fqdn", "")
    fail_reasons = []

    r = ssh_run(hostname, "kubectl get cluster smlm-db -n {}".format(ns), check=False, capture=True)
    if r.returncode != 0:
        die("no smlm-db cluster in '{}' — deploy the lab with smlm_db_ha \"true\" first".format(ns))
    replicas = ssh_output(hostname, "kubectl get cluster smlm-db -n {} -o jsonpath='{{.spec.instances}}'".format(ns))

    print("# [experimental] HA database failover test ({} instances)".format(replicas))

    old_primary, old_node = _smlm_db_primary(hostname, ns)
    if not old_primary:
        die("could not determine the current primary pod")
    print("  Current primary: {} (node {})".format(old_primary, old_node))

    print("  Writing canary row through the uyuni DB path …")
    _smlm_canary_sql(hostname, ns, "DROP TABLE IF EXISTS lab_failover_canary", check=False)
    r = _smlm_canary_sql(hostname, ns, "CREATE TABLE lab_failover_canary(i int, ts timestamptz DEFAULT now())",
                          check=False)
    if r.returncode != 0:
        die("canary write failed before the failover — DB path is not working")
    r = _smlm_canary_sql(hostname, ns, "INSERT INTO lab_failover_canary(i) VALUES (1)", check=False)
    if r.returncode != 0:
        die("canary write failed before the failover — DB path is not working")

    web_before = subprocess.run(
        ["curl", "-kso", "/dev/null", "-w", "%{http_code}", "--max-time", "15",
         "https://{}/rhn/manager/login".format(fqdn)],
        capture_output=True, text=True, check=False).stdout
    print("  Web UI before failover: HTTP {}".format(web_before))

    print("  Deleting the primary pod (simulated failure) …")
    r = ssh_run(hostname, "kubectl delete pod {} -n {} --wait=false".format(old_primary, ns), check=False)
    if r.returncode != 0:
        die("could not delete the primary pod")
    start = time.monotonic()

    new_primary, new_node = "", ""
    for _ in range(60):
        new_primary, new_node = _smlm_db_primary(hostname, ns)
        if new_primary and new_primary != old_primary:
            break
        time.sleep(2)
    if new_primary and new_primary != old_primary:
        promote_s = int(time.monotonic() - start)
        print("  Promoted:  {} (node {}) after {}s".format(new_primary, new_node, promote_s))
    else:
        fail_reasons.append("no standby was promoted within 120s")
        promote_s = "-"

    write_s = "-"
    for _ in range(60):
        r = _smlm_canary_sql(hostname, ns, "INSERT INTO lab_failover_canary(i) VALUES (2)", check=False,
                              capture=True)
        if r.returncode == 0:
            write_s = int(time.monotonic() - start)
            break
        time.sleep(2)
    if write_s == "-":
        fail_reasons.append("writes did not recover within 120s")

    rows_out = _smlm_canary_sql(hostname, ns, "SELECT count(*) FROM lab_failover_canary",
                                 check=False, capture=True).stdout or ""
    rows = "".join(c for c in rows_out if c.isdigit())
    if rows != "2":
        fail_reasons.append("expected 2 canary rows, found '{}' (committed data lost?)".format(rows))

    web_after = subprocess.run(
        ["curl", "-kso", "/dev/null", "-w", "%{http_code}", "--max-time", "15",
         "https://{}/rhn/manager/login".format(fqdn)],
        capture_output=True, text=True, check=False).stdout
    if web_after != "200":
        fail_reasons.append("web UI returned HTTP {} after the failover".format(web_after))

    print("  Waiting for the cluster to heal ({}/{} instances) …".format(replicas, replicas))
    healed = "no"
    for _ in range(60):
        ready = ssh_output(hostname, "kubectl get cluster smlm-db -n {} -o jsonpath='{{.status.readyInstances}}' "
                                      "2>/dev/null".format(ns))
        if ready == replicas:
            healed = "yes"
            break
        time.sleep(10)
    if healed != "yes":
        fail_reasons.append("cluster did not return to {} ready instances within 10 min".format(replicas))

    _smlm_canary_sql(hostname, ns, "DROP TABLE IF EXISTS lab_failover_canary", check=False)

    print("")
    print("─── HA database failover test ─────────────────────────────")
    print("  Old primary     : {} (node {})".format(old_primary, old_node))
    print("  New primary     : {} (node {})".format(new_primary or "none", new_node or "—"))
    print("  Promotion time  : {}s".format(promote_s))
    print("  Write recovery  : {}s".format(write_s))
    print("  Canary rows     : {}/2 (row 1 committed before, row 2 after)".format(rows))
    print("  Web UI          : HTTP {} before / HTTP {} after".format(web_before, web_after))
    print("  Cluster healed  : {}".format(healed))
    print("────────────────────────────────────────────────────────────")
    if fail_reasons:
        die("HA failover test FAILED: {}".format("; ".join(fail_reasons)))
    print("  RESULT: PASSED — primary lost, service continued, no data lost.")
    print("")


# ─── Helm install ─────────────────────────────────────────────────────────────

def setup_smlm(hostname, definition, clu_name, clu_type, mydomain, cfg):
    """Mirrors setup_smlm (bash)."""
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    rel = cfg.get("smlm_rel") or "smlm-server"
    chart = "oci://{}/{}".format(cfg.get("smlm_registry") or "registry.suse.com",
                                  cfg.get("smlm_chart") or "suse/multi-linux-manager/5.2/server-helm")
    ver_arg = "--version {}".format(cfg["smlm_version"]) if cfg.get("smlm_version") else ""

    storage_class_arg = "--set global.storageClassName={}".format(cfg["smlm_storage_class"]) \
        if cfg.get("smlm_storage_class") else ""
    security_arg = "--set server.superPrivileged=true" if (cfg.get("smlm_super_privileged") or "false") == "true" \
        else ""

    if cfg.get("smlm_img_repository"):
        img_repo = cfg["smlm_img_repository"]
    else:
        img_repo = "{}/{}".format(cfg.get("smlm_registry") or "registry.suse.com",
                                   cfg.get("smlm_chart") or "suse/multi-linux-manager/5.2/server-helm")
        if img_repo.endswith("/server-helm"):
            img_repo = img_repo[: -len("/server-helm")]
        img_repo = img_repo + "/x86_64"
    img_tag_arg = "--set tag={}".format(cfg["smlm_img_tag"]) if cfg.get("smlm_img_tag") else ""

    db_ha = (cfg.get("smlm_db_ha") or "false") == "true"
    db_ha_arg = "--set db.enable=false" if db_ha else ""

    print("# Installing SUSE Multi-Linux Manager ({})".format(rel))

    result = ssh_run(hostname,
                      "helm upgrade -i {} {} --namespace {} --create-namespace "
                      "--set global.fqdn='{}' --set registrySecret=scc-credentials --set repository={} "
                      "--set ingress.className={} {} {} {} {} {}".format(
                          rel, chart, ns, cfg.get("smlm_fqdn", ""), img_repo,
                          cfg.get("smlm_ingress_class") or "traefik",
                          storage_class_arg, security_arg, img_tag_arg, db_ha_arg, ver_arg),
                      check=False)
    if result.returncode != 0:
        die("helm install failed for SMLM")

    if not db_ha:
        r = ssh_run(hostname, "kubectl set env deploy/db -n {} PGDATA=/var/lib/pgsql/data/pgdata".format(ns),
                    check=False)
        if r.returncode != 0:
            die("could not set PGDATA on the db deployment")

        patch = (
            '{{"spec":{{"template":{{"spec":{{"initContainers":[{{'
            '"name":"pgdata-perms",'
            '"image":"{img_repo}/server-postgresql:{tag}",'
            '"command":["sh","-c","[ -d /var/lib/pgsql/data/pgdata ] && chmod 0700 /var/lib/pgsql/data/pgdata; true"],'
            '"securityContext":{{"runAsUser":0}},'
            '"volumeMounts":[{{"mountPath":"/var/lib/pgsql/data","name":"var-pgsql"}}]}}]}}}}}}}}'
        ).format(img_repo=img_repo, tag=cfg.get("smlm_img_tag") or "latest")
        r = ssh_run(hostname, "kubectl patch deploy db -n {} -p '{}'".format(ns, patch), check=False)
        if r.returncode != 0:
            die("could not add pgdata-perms init container to the db deployment")

    initvol_patch = (
        '{"spec":{"template":{"spec":{"initContainers":[{\n'
        '  "name": "init-volumes",\n'
        '  "command": ["sh", "-x", "-c",\n'
        '    "for mnt in $(awk \'$2 ~ /^\\\\/mnt\\\\// {print $2}\' /proc/mounts); do vol=${mnt#/mnt}; '
        'rmdir $mnt/lost+found 2>/dev/null; [ -d $vol ] || continue; chown --reference=$vol $mnt; '
        'chmod --reference=$vol $mnt; if [ -z \\"$(ls -A $mnt)\\" ]; then cp -a $vol/. $mnt || exit 1; fi; done; '
        'exit 0"]\n'
        '}]}}}}\n'
    )
    ssh_run(hostname, "cat > /tmp/uyuni-initvol-patch.json", input_text=initvol_patch)
    r = ssh_run(hostname,
                "kubectl patch deploy uyuni -n {} --patch-file /tmp/uyuni-initvol-patch.json "
                "&& rm -f /tmp/uyuni-initvol-patch.json".format(ns), check=False)
    if r.returncode != 0:
        die("could not patch init-volumes on the uyuni deployment")

    print("# Adding DNS entry for SMLM")
    dns_entry = "{}.{}".format(cfg.get("smlm_shorthn") or "smlm", clu_name)
    add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain)

    print("# Waiting for SMLM pods to be ready (this can take 15+ minutes on first boot) …")
    r = ssh_run(hostname, "kubectl wait pods -n {} --all --for condition=Ready --timeout=1500s 2>/dev/null".format(
        ns), check=False)
    if r.returncode != 0:
        log("Pods not fully ready after 25 min — check: kubectl get pods -n {}".format(ns))

    print("")
    print("SUSE Multi-Linux Manager deployed.")
    print("  URL     : https://{}".format(cfg.get("smlm_fqdn", "")))
    print("  User    : {}".format(cfg.get("smlm_admin_user") or "admin"))
    print("  Password: {}".format(cfg.get("smlm_admin_pass") or "admin123"))
    print("")
    print("  Salt clients point to: {}:4505 / 4506".format(cfg.get("smlm_fqdn", "")))

    sync_channels = (cfg.get("smlm_sync_channels") or "").split()
    config_channels = cfg.get("smlm_config_channels") or []
    orgs = cfg.get("smlm_orgs") or []
    access_groups = cfg.get("smlm_access_groups") or []
    ansible_paths = cfg.get("smlm_ansible_paths") or []
    content_projects = cfg.get("smlm_content_projects") or []
    activation_keys = cfg.get("smlm_activation_keys") or []
    system_groups = cfg.get("smlm_system_groups") or []
    custom_info_keys = cfg.get("smlm_custom_info_keys") or []
    system_tags = cfg.get("smlm_system_tags") or []
    environments = cfg.get("smlm_environments") or []
    distributions = cfg.get("smlm_distributions") or []
    kickstart_profiles = cfg.get("smlm_kickstart_profiles") or []
    image_stores = cfg.get("smlm_image_stores") or []
    image_profiles = cfg.get("smlm_image_profiles") or []
    if (cfg.get("smlm_activation_key") or sync_channels or config_channels or orgs
            or access_groups or ansible_paths or content_projects or activation_keys
            or system_groups or custom_info_keys or system_tags or environments
            or distributions or kickstart_profiles or image_stores or image_profiles
            or cfg.get("smlm_monitoring_enabled")):
        exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
        admin_user = cfg.get("smlm_admin_user") or "admin"
        admin_pass = cfg.get("smlm_admin_pass") or "admin123"
        sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
        sc.ensure_channels_synced(hostname, exec_prefix, sync_channels)
        sc.ensure_config_channels(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_monitoring(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_system_groups(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_distributions(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_image_stores(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_image_profiles(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_key(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_appstreams(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_key_packages(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_activation_keys(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_kickstart_profiles(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_users(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_access_groups(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_ansible_paths(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_content_projects(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_custom_info_keys(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_system_tags(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_environments(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_orgs(hostname, exec_prefix, cfg, "smlm", admin_user, admin_pass)


def export_smlm_config(hostname, exec_prefix, cfg, output_path=None):
    """
    Reads `hostname`'s live SMLM configuration back into JSON shaped
    exactly like a lab definition's "smlm" section (see
    libs/spacecmd_common.py's export_config() for exactly what's covered
    and its real, confirmed-live limits — most notably: activation keys,
    channels, and system groups round-trip cleanly, but user/org
    passwords can never be recovered, since they're one-way hashed
    server-side). Prints the result as pretty JSON to stdout, or writes it
    to `output_path` if given. Read-only — issues no write calls at all.
    """
    admin = cfg.get("smlm_admin_user") or "admin"
    password = cfg.get("smlm_admin_pass") or "Smlm12345"
    result = sc.export_config(hostname, exec_prefix, admin, password, "smlm")
    text = json.dumps(result, indent=2)
    if output_path:
        Path(output_path).write_text(text + "\n")
        print("# Wrote live config from '{}' to {}".format(hostname, output_path), file=sys.stderr)
        print("# Review the _export_note field(s) under smlm_orgs before using this — "
              "passwords could not be recovered and must be filled in by hand.", file=sys.stderr)
    else:
        print(text)


def run_ansible_playbooks(hostname, cfg):
    """
    Schedules every entry in smlm_ansible_playbooks (see the JSON section
    comment above) via sc.schedule_ansible_playbook, once per invocation —
    NOT idempotent, NOT part of the automatic setup_smlm() flow (see
    libs/spacecmd_common.py for why). Prints each run's action id and how
    to check on it afterwards.
    """
    playbooks = cfg.get("smlm_ansible_playbooks") or []
    if not playbooks:
        print("No smlm_ansible_playbooks entries in the 'smlm' JSON section — nothing to run.")
        return
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    for pb in playbooks:
        control_node_id = pb.get("control_node_id")
        playbook_path = pb.get("playbook_path")
        inventory_path = pb.get("inventory_path")
        if control_node_id is None or not playbook_path or not inventory_path:
            print("ERROR: smlm_ansible_playbooks entry missing 'control_node_id'/'playbook_path'/"
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
    Runs every entry in smlm_content_lifecycle_actions (see the JSON section
    comment above) via sc.run_content_lifecycle_actions — NOT idempotent,
    NOT part of the automatic setup_smlm() flow (see libs/spacecmd_common.py
    for why).
    """
    actions = cfg.get("smlm_content_lifecycle_actions") or []
    if not actions:
        print("No smlm_content_lifecycle_actions entries in the 'smlm' JSON section — nothing to run.")
        return
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    sc.run_content_lifecycle_actions(hostname, exec_prefix, cfg, "smlm")


def run_scap_scans(hostname, cfg):
    """
    Runs every entry in smlm_scap_scans (see the JSON section comment
    above) via sc.run_scap_scans — heuristically idempotent per-scan, but
    NOT part of the automatic setup_smlm() flow (see libs/spacecmd_common.py
    for why).
    """
    scans = cfg.get("smlm_scap_scans") or []
    if not scans:
        print("No smlm_scap_scans entries in the 'smlm' JSON section — nothing to run.")
        return
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    sc.run_scap_scans(hostname, exec_prefix, cfg, "smlm")


def cve_audit(hostname, cfg, cve_id):
    """Prints audit.listSystemsByPatchStatus's raw result for `cve_id` — a
    pure read-only query, see libs/spacecmd_common.py."""
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    print(sc.list_systems_by_patch_status(hostname, exec_prefix, cve_id))


def run_recurring_schedules(hostname, cfg):
    """
    Runs every smlm_environments entry's recurring_schedule (see the JSON
    section comment above) via sc.run_environment_schedules — NOT
    idempotent, NOT part of the automatic setup_smlm() flow (see
    libs/spacecmd_common.py for why).
    """
    environments = cfg.get("smlm_environments") or []
    if not any(e.get("recurring_schedule") for e in environments):
        print("No smlm_environments entries with a recurring_schedule — nothing to run.")
        return
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    sc.run_environment_schedules(hostname, exec_prefix, cfg, "smlm")


def main():
    usage = ("Usage: {0} <lab.json> [<vm_name>]\n"
             "       {0} <lab.json> --test-failover   # HA DB failover test (requires smlm_db_ha)\n"
             "       {0} <lab.json> --run-ansible-playbooks   # schedule smlm_ansible_playbooks\n"
             "       {0} <lab.json> --run-clm-actions   # build/promote smlm_content_lifecycle_actions\n"
             "       {0} <lab.json> --run-scap-scans   # schedule smlm_scap_scans\n"
             "       {0} <lab.json> --cve-audit CVE-YYYY-NNNNN   # patch-status audit for one CVE\n"
             "       {0} <lab.json> --run-recurring-schedules   # create smlm_environments' recurring schedules"
             ).format(Path(__file__).name)
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, usage=usage, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    cfg = definition.get("smlm", {}) or {}

    # "podman" deployment — traditional mgradm/podman install directly on a
    # dedicated host/VM, no Kubernetes cluster involved at all (see
    # setup_smlm_podman()'s own docstring). Dispatched via k8s.addon_nodes()
    # (any node with "smlm" in its addons[] list), same shape as
    # install_uyuni.py's own main() — NOT k8s.first_server_node(), which only
    # makes sense for the Kubernetes/Helm-chart deployment below.
    if (cfg.get("smlm_deployment") or "kubernetes") == "podman":
        if not cfg.get("smlm_scc_regcode"):
            print("ERROR: smlm_scc_regcode is required in the 'smlm' JSON section "
                  "when smlm_deployment is 'podman'", file=sys.stderr)
            sys.exit(1)

        config = primary.load_config()
        virt_srv = config.get("VIRT_SRV", "")
        env_vm_name = os.environ.get("_vm_name") or None

        # Read-only: dump the target's LIVE configuration back into JSON
        # instead of installing — see export_smlm_config()'s own docstring.
        # Optional 4th arg is an output file path; without it, prints to
        # stdout. Never runs automatically.
        if len(sys.argv) > 2 and sys.argv[2] == "--export-config":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            if len(nodes) > 1:
                print("WARNING: multiple 'smlm' nodes found — exporting only the first ({})".format(
                    nodes[0][0]), file=sys.stderr)
            export_smlm_config(nodes[0][0], "mgrctl exec --", cfg,
                                sys.argv[3] if len(sys.argv) > 3 else None)
            return

        # Schedule smlm_image_imports instead of installing when requested —
        # deliberately a separate, explicit trigger, same reasoning as
        # --run-ansible-playbooks: scheduling an import is not idempotent
        # (each call creates a brand-new action).
        if len(sys.argv) > 2 and sys.argv[2] == "--import-images":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            admin = cfg.get("smlm_admin_user") or "admin"
            password = cfg.get("smlm_admin_pass") or "Smlm12345"
            sc.ensure_spacecmd_config(nodes[0][0], "mgrctl exec --", admin, password)
            sc.import_images(nodes[0][0], "mgrctl exec --", cfg, "smlm")
            return

        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name):
            setup_smlm_podman(vm_name, virt_srv, cfg)
        return

    if not cfg.get("smlm_fqdn"):
        print("ERROR: smlm_fqdn is required in the 'smlm' JSON section", file=sys.stderr)
        sys.exit(1)
    if not cfg.get("smlm_scc_user"):
        print("ERROR: smlm_scc_user is required in the 'smlm' JSON section", file=sys.stderr)
        sys.exit(1)
    if not cfg.get("smlm_scc_password"):
        print("ERROR: smlm_scc_password is required in the 'smlm' JSON section", file=sys.stderr)
        sys.exit(1)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    clu_type = clu_cfg.get("clu_type", "")
    mydomain = clu_cfg.get("mydomain", "")
    online = definition.get("common", {}).get("online") == "1"

    # Run the HA failover test instead of installing when requested — mirrors
    # bash checking sys.argv[2] == "--test-failover" (on_first_server, single
    # function call).
    if len(sys.argv) > 2 and sys.argv[2] == "--test-failover":
        smlm_db_failover_test(vm_name, cfg)
        return

    # Read-only: dump the target's LIVE configuration back into JSON instead
    # of installing — see export_smlm_config()'s own docstring. Optional 4th
    # arg is an output file path; without it, prints to stdout. Never runs
    # automatically.
    if len(sys.argv) > 2 and sys.argv[2] == "--export-config":
        ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
        export_smlm_config(vm_name, "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns), cfg,
                            sys.argv[3] if len(sys.argv) > 3 else None)
        return

    # Schedule smlm_ansible_playbooks instead of installing when requested —
    # deliberately a separate, explicit trigger rather than part of the
    # automatic flow below, since scheduling a playbook run is not
    # idempotent (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-ansible-playbooks":
        run_ansible_playbooks(vm_name, cfg)
        return

    # Run smlm_content_lifecycle_actions instead of installing when
    # requested — same reasoning as --run-ansible-playbooks: build/promote
    # are not idempotent (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-clm-actions":
        run_clm_actions(vm_name, cfg)
        return

    # Schedule smlm_scap_scans instead of installing when requested — same
    # reasoning as --run-ansible-playbooks/--run-clm-actions.
    if len(sys.argv) > 2 and sys.argv[2] == "--run-scap-scans":
        run_scap_scans(vm_name, cfg)
        return

    # Ad-hoc CVE/OVAL patch-status audit — read-only, takes the CVE id as a
    # third argument.
    if len(sys.argv) > 3 and sys.argv[2] == "--cve-audit":
        cve_audit(vm_name, cfg, sys.argv[3])
        return

    # Create smlm_environments' recurring_schedule entries instead of
    # installing when requested — same reasoning as
    # --run-ansible-playbooks/--run-clm-actions/--run-scap-scans: recurring
    # action idempotency was never confirmed (see libs/spacecmd_common.py).
    if len(sys.argv) > 2 and sys.argv[2] == "--run-recurring-schedules":
        run_recurring_schedules(vm_name, cfg)
        return

    setup_helm(vm_name, clu_name, online=online)
    setup_smlm_traefik(vm_name, clu_type)
    setup_smlm_prereqs(vm_name, cfg)
    if (cfg.get("smlm_db_ha") or "false") == "true":
        setup_smlm_db_ha(vm_name, cfg)
    setup_smlm(vm_name, definition, clu_name, clu_type, mydomain, cfg)


if __name__ == "__main__":
    main()
