#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install SUSE Multi-Linux Manager (SMLM) on Kubernetes
# Author/s: Raul Mahiques
# License: GPLv3
#
#
# Reference: https://documentation.suse.com/multi-linux-manager/5.2/en/docs/specialized-guides/kubernetes-guide/server-kubernetes-deployment.html
#
# ─── JSON section: "smlm" ───────────────────────────────────────────────────────
#
# Deployment mode
#   smlm_deployment        : "kubernetes" (default) installs the Helm chart on the first server node of a
#                            Kubernetes cluster. "podman" installs with mgradm and podman directly on a
#                            dedicated host or VM, with no Kubernetes. Target nodes list "smlm" in addons[].
#
# Required for "kubernetes"
#   smlm_fqdn              : fully-qualified domain name of the server (e.g. "smlm.cluster1.mydemo.lab")
#   smlm_scc_user          : SUSE Customer Center account, used to log in to registry.suse.com for the chart images
#   smlm_scc_password      : SUSE Customer Center account password
#
# Required for "podman"
#   smlm_scc_regcode       : SUSE Customer Center registration code of the subscription. It registers the base
#                            product (SUSEConnect -r) and the SMLM module (SUSEConnect -p <product> -r <code>).
#
# Optional for "podman"
#   smlm_coco_replicas     : replicas of the confidential-computing attestation container. Unset omits the flag.
#   smlm_coco_image        : image of that container (default "suse/multi-linux-manager/5.2/x86_64/server-attestation")
#   smlm_coco_tag          : tag override for that image
#   smlm_scc_account       : name of an encrypted credential file (credential_kind "scc") under
#                            /etc/lab_creation/credentials/ to read smlm_scc_user, smlm_scc_password and
#                            smlm_scc_regcode from. If unset and exactly one "scc" file exists, it is used.
#                            Plaintext fields above remain valid.
#   smlm_scc_user / smlm_scc_password
#                          : also used for the podman login to registry.suse.com, and for "mgr-sync add
#                            credentials". That step is required when smlm_channels or smlm_activation_keys is set,
#                            and setup_smlm_podman() fails with a clear message when they are missing. Without
#                            channels or keys, the registry login is skipped and SUSEConnect alone covers the pull.
#   smlm_scc_product       : SCC identifier of the SMLM module, for "SUSEConnect -p". Default
#                            "Multi-Linux-Manager-Server-SLE/5.2/x86_64". Override it if a GA release renames it.
#
# Passwords and credentials (defaults)
#   smlm_db_admin_user     : mlmadmin
#   smlm_db_admin_pass     : mlmadmin123
#   smlm_db_user           : mlmuser
#   smlm_db_pass           : mlmuser123
#   smlm_reportdb_user     : reportuser
#   smlm_reportdb_pass     : reportuser123
#   smlm_admin_user        : admin
#   smlm_admin_pass        : admin123 for "kubernetes", Smlm12345 for "podman"
#                            (the mgradm product default, as in install_uyuni.py)
#
# "podman" install options
#   smlm_email             : admin e-mail, passed to "mgradm install --email"       (default admin@lab.local)
#   smlm_org               : organization created at install                        (default lab)
#   smlm_ssl_password      : password of the generated self-signed CA                (default smlm_admin_pass)
#   smlm_ssl_country       : CA subject country code (2 letters)
#   smlm_ssl_state         : CA subject state/province
#   smlm_ssl_city          : CA subject city
#   smlm_ssl_org           : CA subject organization
#   smlm_ssl_ou            : CA subject organizational unit
#                            smlm_ssl_country/state/city/org/ou are passed as --ssl-<field> only when set, and only
#                            on a fresh install. Ignored when the uyuni-server container already exists.
#
# SUSE's server image ("podman")
#   smlm_byos              : "true" when the node boots SUSE's SMLM server BYOS image (qcow2 for KVM, or a cloud
#                            marketplace image). The image is registered with smlm_scc_regcode only. It needs no
#                            containers module, no SMLM extension and no tooling install. (default false)
#
# Pre-built server images ("podman")
#   smlm_preinstalled      : "true" when the image already contains an installed, channel-synced server. When the
#                            uyuni-server container exists, SCC registration and the tooling install are skipped,
#                            smlm_scc_regcode is not required, and channels already on the server are not re-added.
#                            (default false)
#   smlm_image_admin_pass  : admin password the image was built with. When set and smlm_admin_pass does not log in,
#                            the admin password is changed to smlm_admin_pass before any other step.
#
# Server conveniences ("podman")
#   smlm_salt_auto_accept  : "true" to accept every new salt minion key. It writes
#                            /etc/salt/master.d/zz-lab-auto-accept.conf in the server container and restarts
#                            salt-master. For throw-away labs only. (default false)
#   smlm_bootstrap_scripts : [{"name": "generic_bootstrap.sh", "url": "https://..."}]. The files are downloaded on the
#                            server host and published under /pub/bootstrap/<name> (mode 0755). name is a plain file name.
#
# Helm and release ("kubernetes")
#   smlm_version           : chart version (empty = latest, e.g. "5.2.0")
#   smlm_ns                : namespace (default uyuni-server)
#   smlm_rel               : Helm release name (default smlm-server)
#   smlm_registry          : OCI registry of the chart (default registry.suse.com)
#   smlm_chart             : OCI chart path (default suse/multi-linux-manager/5.2/server-helm)
#   smlm_img_repository    : image repository base (default derived from registry and chart, plus /x86_64)
#   smlm_img_tag           : tag of all images (default: the chart default, "latest")
#
# Networking and security ("kubernetes")
#   smlm_shorthn           : short hostname for the DNS entry (default smlm)
#   smlm_ingress_class     : ingress class (default traefik). The SMLM chart does not support nginx.
#   smlm_super_privileged  : "true" runs in super-privileged mode (default false, which uses AppArmor or SELinux)
#   smlm_storage_class     : StorageClass (empty = cluster default)
#   smlm_lh_overprovision  : Longhorn over-provisioning percentage, when smlm_storage_class is "longhorn" (default 500)
#
# HA database ("kubernetes", experimental)
#   smlm_db_ha             : "true" replaces the single-pod PostgreSQL with a CloudNativePG cluster (default false).
#                            The 'db' and 'reportdb' hostnames then point at the operator's primary Service, smlm-db-rw.
#                            Host-level HA needs a cluster with at least as many nodes as replicas.
#   smlm_db_ha_replicas    : PostgreSQL instances (default 3)
#   smlm_db_ha_sync        : "true" (default) for synchronous replication. A primary failure loses no committed
#                            transaction, but writes stall while no standby is available. "false" is asynchronous.
#   smlm_db_ha_size        : data volume per instance (default 50Gi)
#   smlm_db_ha_pg_image    : PostgreSQL image (default ghcr.io/cloudnative-pg/postgresql:18, which must match SMLM 5.2's major version)
#   smlm_db_ha_cnpg_version: CloudNativePG operator chart version (default latest)
#   Failover test, after deployment:  install_smlm.py <lab.json> --test-failover
#   It writes a canary row, deletes the primary pod, measures promotion and write recovery, and checks that no
#   committed row was lost and the web UI stayed up. For a harder test, power off the primary's node VM.
#
# Activation key (created after install; skipped entirely when smlm_activation_key is unset)
#   smlm_activation_key                   : key name
#   smlm_activation_key_desc              : description (default: the key name)
#   smlm_activation_key_base_channel      : base channel label. Required when smlm_activation_key is set.
#   smlm_activation_key_child_channels    : space-separated child channel labels
#   smlm_activation_key_universal_default : "true" marks it as the organization's universal default (default false)
#   smlm_activation_key_entitlements      : comma-separated, e.g. "enterprise_entitled,virtualization_host"
#   smlm_activation_key_contact_method    : contact method
#   smlm_activation_key_config_channels   : space-separated config channel labels
#   smlm_activation_key_enable_config_deployment : "true" enables config-file deployment on the key (default false)
#   smlm_activation_key_groups            : space-separated system group names
#   smlm_activation_key_appstreams        : space-separated "module:stream" pairs, e.g. "nodejs:20 postgresql:16".
#                                           Applied on every run. An already-enabled module is detected from the
#                                           server's error and skipped.
#   smlm_activation_key_packages          : space-separated package names, added on every run. Names only.
#
# Channels ("podman")
#   smlm_channels          : software channel labels (list or space-separated string), added with "mgr-sync add
#                            channels", together with every activation key's child channels. Needs smlm_scc_user
#                            and smlm_scc_password.
#   smlm_sync_channels     : channels to ensure are synced (via "mgr-sync add channel <label>" when not already
#                            present) before the activation key is created.
#   smlm_beta_channels     : BETA-flagged channel labels. They are merged into smlm_channels and treated identically.
#                            Listing them here makes the opt-in explicit, e.g. ["sle-product-sles-16.1-x86_64"].
#
# Config channels (created before the activation key, so smlm_activation_key_config_channels can refer to them)
#   smlm_config_channels   : [{"label", "name", "description", "type": "normal" (default) | "state",
#                            "init_sls" (state channels only),
#                            "files": [{"path", "content", "owner", "group", "mode", "binary"}]}]
#                            Idempotent per channel and per file. A file whose sha256 already matches is skipped.
#                            This does not attach a channel to a registered system. Use
#                            smlm_activation_key_config_channels for newly registered clients.
#
# Virtual Host Managers (Systems -> Virtual Host Managers)
#   smlm_virtual_host_managers : [{"label", "type": "aws" (default), "region", "zone", "access_key_id",
#                                  "secret_access_key"}, {"label", "type": "libvirt", "uri", "sasl_username",
#                                  "sasl_password"}]
#                            The "aws" credentials are usually left out of the entry and resolved from a
#                            long-lived IAM key, see smlm_vhm_aws_account below. A key on the entry overrides the
#                            resolved one for that manager only. The gatherer polls for ever, so it needs a key
#                            that does not expire, not an SSO or STS session. For "libvirt", sasl fields are
#                            optional and can be omitted for SSH-key auth, e.g.
#                            "qemu+ssh://root@nuc6.mydemo.lab/system".
#   smlm_vhm_aws_account   : name of a 'vhm_aws' credential file under /etc/lab_creation/credentials/ with
#                            vhm_aws_access_key_id and vhm_aws_secret_access_key. Auto-discovered when it is the only
#                            such file and this is unset.
#
# Virtual guest provisioning (explicit trigger, --provision-guests; a real new VM is created)
#   smlm_virtual_guests    : [{"host": "nuc6.mydemo.lab" (an already registered client),
#                            "name", "kickstart_profile" (name of a smlm_kickstart_profiles entry),
#                            "memory_mb" (2048), "vcpus" (2), "disk_gb" (20)}]
#
# Organizations (created after the above; each has its own admin session for scoped provisioning)
#   smlm_orgs              : [{"name", "admin_user", "admin_pass", "admin_email", "admin_first_name",
#                            "admin_last_name", "prefix", "pam" (false), "trust_with": ["other-org", ...],
#                            "share_channels": ["channel-label", ...],
#                            "share_channels_access": "protected" | "public" | "private",
#                            the organization's own smlm_activation_key*, smlm_config_channels and
#                            smlm_access_groups, with the same field names}]
#                            admin_user, admin_pass and admin_email are required to create the organization.
#                            trust_with establishes channel-sharing trust. share_channels marks channels this
#                            organization owns as shared, through channel.access.setOrgSharing, so a trusted
#                            organization's activation keys can use them.
#
# User accounts (top level, scoped to the default organization, or inside an smlm_orgs entry)
#   smlm_users             : [{"username", "password", "first_name", "last_name", "email", "pam" (false),
#                            "roles": [...]}]
#                            The first four are required for creation, as for an org's admin account.
#                            "roles" are applied on every run with user_addrole. Use only the fixed labels from
#                            'spacecmd user_listavailableroles': activation_key_admin, channel_admin, config_admin,
#                            image_admin, org_admin, regular_user, satellite_admin, system_group_admin.
#                            A custom access group label does not belong here. Put the user in that group's
#                            "users" list instead (see smlm_access_groups).
#
# Kickstart snippets (run before distributions and profiles, which may refer to them)
#   smlm_snippets          : [{"name", "content"}]. A snippet is used in kickstart and AutoYaST profiles through the
#                            $SNIPPET('spacewalk/<org>/<name>') macro. It is idempotent, and new content overwrites it.
#
# Autoinstall trees ("Kickstart Distributions") and kickstart profiles
#   smlm_distributions     : [{"name", "path", "base_channel", "install_type"}]
#                            path must already exist on the server, with an extracted installer tree under it.
#                            Mount the ISO out of band, for example with "mount -o loop", and copy the contents there.
#                            distribution_create fails with "initrd could not be found" otherwise. install_type is
#                            one of the labels "distribution_create --help" lists on that server, such as sles15generic,
#                            sles16generic, rhel_9 or generic_rpm.
#   smlm_kickstart_profiles: [{"name", "distribution" (name of an smlm_distributions entry), "root_password",
#                            "virt_type": "none" (default) | "para_host" | "qemu" | "xenfv" | "xenpv",
#                            "variables": {"key": "value"}, "activation_keys": [...], "child_channels": [...]}]
#                            root_password is used only at creation, because the server stores only a hash. To change
#                            it, delete and recreate the profile. variables, activation_keys and child_channels are
#                            applied on every run, diffed against the server's current lists.
#
# Server self-monitoring (Admin -> Manager Configuration -> Monitoring)
#   smlm_monitoring_enabled: "true" enables the bundled exporters for node, tomcat, postgres, taskomatic and
#                            self_monitoring (default unset, which does nothing). This is a switch for exporters the
#                            image already includes. It does not point at an external Prometheus. Uyuni is pull-based:
#                            an external Prometheus (the "prometheus" addon) scrapes this server.
#                            Restarts Tomcat and Taskomatic only on the change from disabled to enabled.
#                            Ports to open for a remote Prometheus: 9100 (node), 9187 (postgres), 5556 (tomcat JMX),
#                            5557 (taskomatic JMX), 9800 (taskomatic direct), and the web port 80 or 443 for the
#                            message-queue metrics path /rhn/metrics.
#
# Image management (Images -> Stores, Profiles, Build, Import). API only.
#   smlm_image_stores      : [{"label", "uri": "registry.suse.com", "type": "registry" | "os_image", "username",
#                            "password"}]. type must be a label that image.store.listImageStoreTypes returns on the
#                            server. username and password are optional. registry.suse.com needs none for SUSE's own
#                            public images. Stores are created before profiles.
#   smlm_image_profiles    : [{"label", "type": "dockerfile" | "kiwi", "store" (label of an smlm_image_stores entry),
#                            "path": "https://github.com/USER/project.git#branch:folder", "activation_key"}].
#                            activation_key is required. It decides which channels a build or import can use.
#   smlm_image_imports     : [{"name", "version": "latest", "store", "build_host_id" (numeric id of a system that has
#                            the "Container Build Host" entitlement; find it with 'spacecmd system_list'),
#                            "activation_key" (optional)}]
#                            Run with install_smlm.py <lab.json> --import-images. It never runs automatically, because
#                            each call schedules a new import.
#   smlm_image_build_hosts : [{"system": "registered-hostname"}]. Enables the "container_build_host" entitlement and
#                            applies highstate. It runs before imports, so a build_host_id can be used.
#                            The Containers module must still be in that system's channels.
#   smlm_mcp_server        : {"version": "latest", "port": 8090, "user", "password" (default the smlm admin account),
#                            "write_tools_enabled": false, "ssl_verify": false}
#                            Deploys the Uyuni MCP server (github.com/uyuni-project/mcp-server-uyuni) as a standalone
#                            podman container next to uyuni-server, so an MCP client can inspect and manage this server.
#                            Only with smlm_deployment "podman". The port is bound to 127.0.0.1 only. The server runs
#                            without authentication and can hold write credentials, so reach it from elsewhere through an
#                            SSH tunnel or port forward.
#
# Recurring actions (automatic and idempotent)
#   smlm_recurring_actions : [{"name" (required, unique per entity), "entity_type": "minion" | "group" | "org",
#                            "entity" (system or group name, or a numeric org id), "cron_expr" ("0 2 * * *"),
#                            "schedule_type": "highstate" (default) | "custom", "states": [...] (required for custom)}]
#
# Maintenance windows (automatic and idempotent)
#   smlm_maintenance_calendars : [{"label", "ical": "BEGIN:VCALENDAR ... END:VCALENDAR"} or {"label", "url"}].
#                            Give exactly one of ical or url. Calendars are synced, so an existing calendar whose ical
#                            or url differs is updated, with the reschedule strategy "Fail". Scheduled actions are never
#                            cancelled. To sync a live server without anything else, run
#                            install_smlm.py <lab.json> --sync-maintenance-calendars.
#   smlm_maintenance_schedules : [{"name", "type": "single" | "multi", "calendar" (name of a calendar, optional),
#                            "systems": [...] (optional, assigned right after creation)}]
#
# Action chains (automatic, idempotent by label)
#   smlm_action_chains     : [{"label", "actions": [{"system", "type": "script", "script": "..."} or
#                            {"system", "type": "highstate"}]}]
#                            A chain is created with its actions and is left unscheduled. It never runs.
#
# Custom software channels, pushed packages and patches (automatic and idempotent)
#   smlm_custom_channels   : [{"label", "name", "summary", "arch_label" ("channel-x86_64" default for RPM channels),
#                            "parent_label" (an existing base channel makes this a child channel),
#                            "checksum_type": "sha256",
#                            "packages": ["/local/path/to.rpm", ...]}]
#                            Packages are local files on the automation node, pushed with rhnpush. The push works only
#                            with smlm_deployment "podman". The channel is still created in any case.
#   smlm_patches           : [{"advisory_name" (required, e.g. "LAB-2026:0001"), "channel" (an smlm_custom_channels
#                            label), "synopsis", "topic", "description", "solution", "advisory_release": 1,
#                            "advisory_type": "Bug Fix Advisory", "product": "lab-in-a-box", "severity": "Low",
#                            "packages": [names already in that channel]}]
#                            The server accepts errata only for channels on its allowlist,
#                            java.allow_adding_patches_via_api in /etc/rhn/rhn.conf. The channel is added to that list
#                            the first time it is used. Tomcat restarts to apply the change, so expect a short API
#                            interruption after the first patch of a run.
#
# Image builds (explicit trigger, --build-images; never automatic)
#   smlm_image_builds      : [{"profile" (an smlm_image_profiles label), "build_host" (a system with the container
#                            build host entitlement), "version": "latest"}]
#
# Stored system profiles (automatic, idempotent by label)
#   smlm_system_profiles   : [{"system", "label", "description"}]
#
# Custom info values on systems (automatic and idempotent)
#   smlm_system_custom_values : [{"system", "key", "value"}]. The key must already be defined in smlm_custom_info_keys.
#
# Organization-to-organization system transfers (automatic)
#   smlm_org_system_transfers : [{"org", "systems": [...]}]. The source and destination organizations must already trust
#                            each other, which smlm_orgs trust_with sets up. A system already in the destination
#                            organization is skipped.
#
# External authentication, SAML 2.0 SSO via Keycloak (explicit trigger, --enable-sso; never automatic)
#   Uyuni's SSO uses SAML 2.0. The OAuth settings of smlm_mcp_server are a separate feature.
#   smlm_sso               : {"keycloak_host" (required; a separate host that the browser can reach),
#                            "keycloak_port": 8080, "realm": "lab-in-a-box", "admin_user": "admin",
#                            "admin_password": "admin", "demo_user", "demo_password", "demo_email"}
#                            demo_user is one extra Keycloak user. It must be an existing Uyuni account, because SSO maps
#                            to an account and never creates one. A Keycloak user is also created for the SMLM admin account
#                            and for each smlm_users entry with a password. A "pam": true entry is skipped, because it
#                            authenticates through the OS.
#                            With SSO enabled, the web login goes through Keycloak for everyone. spacecmd, mgr-sync and this
#                            project's automation keep password auth.
#                            The keycloak_port must be reachable from the browser. On a cloud node, add it to aws_open_ports.
#                            Otherwise the browser times out with nothing logged on either side.
#
# RBAC: custom user access groups (API only, Uyuni 2025.05 and later, SMLM 5.1 and later)
#   smlm_access_groups     : [{"label", "description", "permissions_from": [role labels], "permissions": [{"namespace",
#                            "mode": "R" | "W"}], "users": [usernames]}]
#                            Each user must already exist, from smlm_users or an organization's admin account.
#                            Access groups are created after users.
#
# Ansible integration (orchestration only; playbook and inventory files stay on the control node)
#   smlm_ansible_control_nodes : [{"system": "hostname"}]. Enables the "Ansible Control Node" entitlement and applies
#                            highstate to install the ansible package. The system must be registered first.
#                            Also run with install_smlm.py <lab.json> --enable-ansible-control-nodes.
#   smlm_ansible_paths     : [{"system" (hostname) or "control_node_id" (numeric), "type": "playbook" | "inventory",
#                            "path"}]. For "playbook", path is a directory, such as /srv/ansible/playbooks. For
#                            "inventory", path is the inventory file or script.
#   smlm_ansible_playbooks : [{"control_node_id", "playbook_path", "inventory_path", "earliest" (optional, default now),
#                            "action_chain_label" (optional), "test_mode": false, "extra_vars": "...", "flush_cache": false}]
#                            Run with install_smlm.py <lab.json> --run-ansible-playbooks. It never runs automatically.
#
# Content Lifecycle Management (CLM)
#   Projects, sources, filters and environments are defined automatically and idempotently, before activation keys so the
#   keys can refer to environments. Builds and promotions are explicit, with --run-clm-actions.
#   smlm_content_projects  : [{"label", "name", "description", "sources": [software channel labels],
#                            "filters": [{"name", "rule": "allow" | "deny", "entity_type": "package" | "erratum" |
#                            "module" | "ptf", "matcher", "field", "value"}],
#                            "environments": ["dev", "test", "prod"] or [{"label", "name", "description"}]}]
#                            Only software channels can be sources. Filters have no lookup by name, so the check is made at
#                            project level and the new id is read from spacecmd's output.
#   smlm_content_lifecycle_actions : [{"project", "action": "build", "message", "wait": true, "wait_env", "wait_timeout"},
#                            {"project", "action": "promote", "from_env", "wait": true, "wait_env"}]
#                            For promote, from_env is the stage being promoted from. The server picks the next stage.
#                            "wait" polls the environment until it is built or failed.
#
# SCAP compliance auditing
#   smlm_scap_scans        : [{"system" or "group", "xccdf_path", "profile"}]. Exactly one of system or group. A group
#                            expands to its members. Idempotent by system and xccdf_path. The XCCDF path and the scanner
#                            must already exist on the target, unless ensure_openscap_prerequisites() installs them.
#                            Run with install_smlm.py <lab.json> --run-scap-scans. It never runs automatically.
#   smlm_scap_policies     : [{"policy_name", "scap_content_id" (a content id uploaded through the Web UI), "xccdf_profile_id"
#                            (read it from the uploaded document), "description", "earliest" (ISO local date-time),
#                            "tailoring_file", "tailoring_profile_id", "oval_files", "advanced_args",
#                            "fetch_remote_resources": false}]
#                            Automatic and idempotent by name. It logs in as smlm_admin_user through the Web UI route. That
#                            route is internal and may change between releases. SCAP content and tailoring files are not
#                            uploaded by this module. Upload them once through the Web UI, then use their ids here.
#
# CVE audit (read only, no JSON)
#   install_smlm.py <lab.json> --cve-audit CVE-YYYY-NNNNN         systems' patch status for the CVE
#   install_smlm.py <lab.json> --cve-audit-images CVE-YYYY-NNNNN  the same for container and OS images
#
# Environment topology (a composition of the primitives above; automatic except recurring_schedule)
#   smlm_activation_keys   : [{...}], one dict per key, with the same fields as the smlm_activation_key* keys.
#                            This lets one organization define several named keys, one per environment.
#   smlm_system_groups     : [{"name", "description", "systems": [...]}]
#   smlm_custom_info_keys  : [{"name", "description"}]. Must be defined before smlm_system_tags or an environment's
#                            custom_info_tags can set a value.
#   smlm_system_tags       : [{"system", "tags": {"key": "value"}}]. Uyuni has no tag object. These are custom-info values.
#   smlm_environments      : [{"label", "system_group" (name of an smlm_system_groups entry), "activation_key" (name of a key),
#                            "custom_info_tags": {...},
#                            "recurring_schedule": {"type": "highstate" (default) | "custom", "cron", "states" (for custom),
#                            "group_id" (skips the name lookup), "extra": {...}}}]
#                            system_group and activation_key are references only. recurring_schedule needs the numeric group
#                            id, which is looked up from the name unless group_id is given. Run it with
#                            install_smlm.py <lab.json> --run-recurring-schedules.
#   smlm_grafana_formulas  : [{"system", "admin_user" (default admin), "admin_pass" (default admin),
#                            "prometheus": [{"key", "url" (default http://localhost:9090), "user", "password"}],
#                            "reportdb": false, "is_hub": false,
#                            "dashboards": {"uyuni": true, "uyuni_clients": true, "postgresql": true, "apache": true}}]
#                            Applies SMLM's own grafana Salt formula to the target, which installs Grafana there. This is
#                            separate from the install_prometheus.py and install_grafana.py addons, which use podman. The
#                            target needs a monitoring add-on subscription and Prometheus. Grafana is not available on SMLM
#                            Proxy.
#
# Read-only export of a running server (never runs automatically)
#   install_smlm.py <lab.json> --export-config [output.json]
#   Writes the "smlm" section for channels, activation keys, system groups, the calling organization's access groups, and
#   a best-effort username list for the other organizations. Passwords cannot be recovered, because the server stores only
#   hashes, so fill in each admin_pass and password by hand. A custom access group's member list is readable only for the
#   organization of the calling session.
#
# Note: RKE2 (default) or K3s, with Traefik. On RKE2, Traefik is enabled through the 'ingress-controller' option, and the
#       extra TCP ports 4505, 4506 (Salt) and 5432 (report DB) are exposed through a rke2-traefik HelmChartConfig. On K3s
#       (kclusters clu_type "k3s") the bundled Traefik is used, and the same ports are exposed through its ServiceLB.

__version__ = "39753e7"

PLUGIN = {
    "name": "smlm",
    # Deployment modes: "kubernetes" (default) is the Helm chart deployment. "podman" (smlm_deployment) is the
    # mgradm and podman deployment on a dedicated host or VM. The two modes are dispatched separately in main(), so
    # both are listed here.
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "standalone-container"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
import xmlrpc.client
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
import spacecmd_common as sc  # noqa: E402
from lab_creation import setup_helm, ssh_run, ssh_output, add_service_dns, check_ssh_conn, reboot_vm, die, log  # noqa: E402


_BOOTSTRAP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_true(value):
    return value is True or str(value).lower() == "true"


def _api_login_ok(hostname, login, password):
    """True if login/password authenticates against the server's XML-RPC API."""
    ctx = ssl._create_unverified_context()  # lab server, self-signed certificate
    client = xmlrpc.client.ServerProxy("https://{}/rpc/api".format(hostname), context=ctx)
    try:
        client.auth.logout(client.auth.login(login, password))
        return True
    except xmlrpc.client.Fault:
        return False


def rotate_admin_password(hostname, admin, password, previous):
    """
    Make `password` the admin password of a server built from an image whose
    admin password was `previous` (smlm_image_admin_pass). No-op when
    `password` already works; dies when neither does.
    """
    if _api_login_ok(hostname, admin, password):
        return
    if not previous or not _api_login_ok(hostname, admin, previous):
        die("neither smlm_admin_pass nor smlm_image_admin_pass log in as '{}' on '{}'".format(
            admin, hostname))
    print("- Replacing the image's admin password with this lab's smlm_admin_pass")
    ctx = ssl._create_unverified_context()
    client = xmlrpc.client.ServerProxy("https://{}/rpc/api".format(hostname), context=ctx)
    key = client.auth.login(admin, previous)
    try:
        client.user.setDetails(key, admin, {"password": password})
    finally:
        client.auth.logout(key)


def _existing_channels(hostname, admin, password):
    """Software channel labels already present on the server."""
    out = ssh_run(hostname, "mgrctl exec -- spacecmd -q -u {} -p {} -- softwarechannel_list".format(
        shlex.quote(admin), shlex.quote(password)), check=False, capture=True).stdout or ""
    return {line.strip() for line in out.splitlines() if line.strip()}


def ensure_salt_auto_accept(hostname):
    print("- Enabling salt auto_accept for new minion keys")
    script = ("mkdir -p /etc/salt/master.d && "
              "printf 'auto_accept: True\\n' > /etc/salt/master.d/zz-lab-auto-accept.conf && "
              "systemctl restart salt-master")
    ssh_run(hostname, "mgrctl exec -- sh -c {}".format(shlex.quote(script)), check=False)


def ensure_bootstrap_scripts(hostname, scripts):
    for entry in scripts:
        name, url = entry["name"], entry["url"]
        print("- Publishing bootstrap script /pub/bootstrap/{}".format(name))
        tmp = "/tmp/lab-bootstrap-{}".format(name)
        dest = "/srv/www/htdocs/pub/bootstrap/{}".format(name)
        ssh_run(hostname, "curl -4 -fsSL --retry 5 -o {tmp} {url} && "
                          "mgrctl cp {tmp} server:{dest} && "
                          "mgrctl exec -- chmod 0755 {dest} && rm -f {tmp}".format(
                              tmp=shlex.quote(tmp), url=shlex.quote(url), dest=shlex.quote(dest)))


def _registry_login(hostname, scc_user, scc_password):
    """
    podman login to registry.suse.com with the SCC account, when one is given.
    Separate from the SUSEConnect registration: mgradm pulls SMLM's entitled
    container images from registry.suse.com, which needs its own podman login,
    per the docs' own troubleshooting section (see setup_smlm_podman()'s own
    docstring).
    """
    if scc_user and scc_password:
        print("- Logging into registry.suse.com")
        ssh_run(hostname, "echo {} | podman login -u {} --password-stdin registry.suse.com".format(
            shlex.quote(scc_password), shlex.quote(scc_user)), check=False)
    else:
        print("- smlm_scc_user/smlm_scc_password not set — skipping podman login to "
              "registry.suse.com; relying on SUSEConnect registration alone to authorize "
              "the image pull (see setup_smlm_podman()'s own docstring)")


def _register_byos_image(hostname, virt_srv, regcode):
    """
    Register the SUSE Multi-Linux Manager BYOS image (bring your own subscription) with the operator's regcode.

    The image already ships mgradm, mgrctl, podman and SMLM as its base product. It needs no containers module, no SMLM extension
    and no tooling install. On the transactional (SL Micro) base, the registration is `transactional-update register`, which takes
    effect after a reboot.
    """
    if ssh_run(hostname, "command -v mgradm", check=False).returncode != 0:
        die("smlm_byos is set, but '{}' has no mgradm — boot it from SUSE's SUSE Multi-Linux "
            "Manager Server BYOS image".format(hostname))
    print("- smlm_byos: registering SUSE's SMLM Server image with smlm_scc_regcode")
    transactional = ssh_run(hostname, "command -v transactional-update", check=False).returncode == 0
    cmd = "transactional-update --quiet register -r {}" if transactional else "SUSEConnect -r {}"
    if ssh_run(hostname, cmd.format(shlex.quote(regcode)), check=False).returncode != 0:
        die("could not register '{}' with smlm_scc_regcode".format(hostname))
    if transactional:
        reboot_vm(virt_srv, hostname)
        time.sleep(5)
        check_ssh_conn(hostname)


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
        # smlm_preinstalled images skip SCC registration entirely (see
        # setup_smlm_podman()), so neither the regcode nor the mirror
        # credentials can be required up front — setup_smlm_podman() still
        # dies at runtime if channels turn out to be missing.
        if not _is_true(cfg.get("smlm_preinstalled")):
            v.vreq_or_credential("smlm", "smlm_scc_regcode", "scc", account_field="smlm_scc_account")
        if (cfg.get("smlm_channels") or cfg.get("smlm_activation_keys")) and not _is_true(cfg.get("smlm_preinstalled")):
            v.vreq_or_credential("smlm", "smlm_scc_user", "scc", account_field="smlm_scc_account")
            v.vreq_or_credential("smlm", "smlm_scc_password", "scc", account_field="smlm_scc_account")
    else:
        v.vreq("smlm", "smlm_fqdn")
        v.vreq_or_credential("smlm", "smlm_scc_user", "scc", account_field="smlm_scc_account")
        v.vreq_or_credential("smlm", "smlm_scc_password", "scc", account_field="smlm_scc_account")
    v.vns("smlm")
    v.vver("smlm")
    v.vbool("smlm", "smlm_super_privileged")
    v.vbool("smlm", "smlm_db_ha")
    v.vbool("smlm", "smlm_db_ha_sync")
    v.vbool("smlm", "smlm_preinstalled")
    v.vbool("smlm", "smlm_byos")
    v.vbool("smlm", "smlm_salt_auto_accept")
    for entry in cfg.get("smlm_bootstrap_scripts") or []:
        if not isinstance(entry, dict) or not entry.get("url") or not _BOOTSTRAP_NAME_RE.match(str(entry.get("name", ""))):
            v.errors.append("[ERROR] smlm.smlm_bootstrap_scripts: every entry needs a plain file "
                            "'name' (letters, digits, '.', '_', '-') and a 'url'")
    v.vport("smlm", "smlm_db_ha_replicas")


# ─── Traditional (mgradm/podman) deployment ─────────────────────────────────
# A genuine SMLM install uses its own deployment path, not Uyuni's. The SUSE documentation has a separate guide for
# SMLM with the same mgradm and podman tooling, sourced from the SUSE registries.

def _resolve_and_fill_vhm_credentials(cfg, virtual_host_managers):
    """
    Resolves the shared smlm_vhm_aws_account credential (see that JSON
    field's own doc comment for why this needs a real, long-lived AWS key
    rather than this project's usual SSO/STS cloud_account mechanism) and
    fills it into every "aws"-type entry in virtual_host_managers that
    doesn't already carry its own explicit access_key_id/secret_access_key.
    Split out from setup_smlm_podman() itself so it can be run through
    run_provisioning_step() like every other step — see that call site's
    own comment for the real cascading-failure bug this fixes.
    """
    vhm_creds = ac.resolve_credential(
        cfg, "vhm_aws",
        {"vhm_aws_access_key_id": "smlm_vhm_aws_access_key_id",
         "vhm_aws_secret_access_key": "smlm_vhm_aws_secret_access_key"},
        account_key="smlm_vhm_aws_account")
    for vhm in virtual_host_managers:
        if (vhm.get("type") or "aws") == "aws":
            vhm.setdefault("access_key_id", vhm_creds["vhm_aws_access_key_id"])
            vhm.setdefault("secret_access_key", vhm_creds["vhm_aws_secret_access_key"])


def setup_smlm_podman(hostname, virt_srv, cfg):
    """
    Install SUSE Multi-Linux Manager with mgradm and podman directly on a dedicated host or VM, without Kubernetes.

    The function reuses the mgradm install and container health-wait helpers in libs/mgradm_common.py, which are shared with
    install_uyuni.py. The underlying tool and container mechanics are the same for Uyuni and SMLM. What differs is where mgradm,
    mgrctl and the server images come from.

    install_uyuni.py adds Uyuni's community OBS repository, which needs no entitlement. A SMLM install registers the host with SCC
    for the SUSE Multi-Linux Manager module, so mgradm, mgrctl and the entitled images pulled from registry.suse.com are the
    licensed product.

    Options (smlm_*, see the module reference):
      - smlm_scc_regcode registers the base product with SUSEConnect -r. It is also passed to the module registration
        (SUSEConnect -p <product> -r <code>).
      - smlm_scc_product is the SCC identifier of the SMLM module. The default is "Multi-Linux-Manager-Server-SLE/5.2/x86_64".
      - smlm_scc_user and smlm_scc_password are the SCC account for "podman login registry.suse.com". The login is skipped with a
        warning when they are unset.
      - On plain SLES 15 SP7, podman is not preinstalled. The function enables the containers module (SUSEConnect -p
        sle-module-containers/15.7/x86_64), installs podman and enables its socket. SL Micro ships podman already.
    """
    from mgradm_common import run_install_with_pg_hba_guard, ensure_server_container_active

    regcode = cfg.get("smlm_scc_regcode")
    product = cfg.get("smlm_scc_product") or "Multi-Linux-Manager-Server-SLE/5.2/x86_64"
    email = cfg.get("smlm_email") or "admin@lab.local"

    scc_user = cfg.get("smlm_scc_user")
    scc_password = cfg.get("smlm_scc_password")
    # A pre-built image (smlm_preinstalled) already contains the registered tooling and the installed server, so SCC
    # registration is skipped and no regcode is needed.
    preinstalled = _is_true(cfg.get("smlm_preinstalled")) and ssh_run(
        hostname, "podman container exists uyuni-server", check=False).returncode == 0
    if preinstalled:
        print("- smlm_preinstalled: uyuni-server already present — skipping SCC registration "
              "and mgradm tooling install")
    elif _is_true(cfg.get("smlm_byos")):
        _register_byos_image(hostname, virt_srv, regcode)
        _registry_login(hostname, scc_user, scc_password)
    else:
        print("- Registering the host with SCC")
        ssh_run(hostname, "SUSEConnect -r {}".format(shlex.quote(regcode)), check=False)
        # sle-module-containers must be registered before the SMLM extension. SCC rejects the SMLM module with HTTP 422
        # when the containers module is not already active. The module is free and needs no regcode. It is registered for
        # every base OS, because the dependency check runs regardless of the base OS.
        ssh_run(hostname, "SUSEConnect -p sle-module-containers/15.7/x86_64", check=False)
        r = ssh_run(hostname, "SUSEConnect -p {} -r {}".format(shlex.quote(product), shlex.quote(regcode)),
                    check=False)
        if r.returncode != 0:
            die("could not register the SUSE Multi-Linux Manager module ('{}') on '{}' via SUSEConnect "
                "— confirm smlm_scc_product is the real product identifier (see setup_smlm_podman()'s "
                "own docstring for how to find it)".format(product, hostname))

        _registry_login(hostname, scc_user, scc_password)

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
    # Same flags as install_uyuni.py's `mgradm install podman` call. mgradm and podman behave the same for both products.
    # Every value is shell-quoted. An unquoted multi-word value such as --organization "SUSE Test" is split by the remote
    # shell, and mgradm then reads the leftover word as an FQDN.
    #
    # A previous `mgradm install` can fail after the bootstrap has finished (schema, organization and admin), for example
    # while checking an optional image the subscription is not entitled to. The server is then healthy, but mgradm refuses
    # a second install ("Server is already initialized!"), and the only other path is a full uninstall. The function
    # detects an existing, populated server and goes straight to the health checks and post-install steps.
    already_initialized = ssh_run(hostname, "podman container exists uyuni-server", check=False).returncode == 0
    if already_initialized:
        print("- uyuni-server container already exists — skipping mgradm install, resuming "
              "from the post-install steps (a prior run's mgradm install likely died AFTER "
              "the real bootstrap completed; see this line's own comment)")
    else:
        install_cmd = (
            "mgradm install podman "
            "--admin-login {} "
            "--admin-password {} "
            "--email {} "
            "--ssl-password {} "
            "--organization {}".format(
                shlex.quote(admin), shlex.quote(password), shlex.quote(email),
                shlex.quote(cfg.get("smlm_ssl_password") or password), shlex.quote(org)))
        # Confidential Computing attestation container — see this JSON section's
        # own smlm_coco_replicas doc comment above for the
        # `mgradm install podman --help` flags this maps to.
        for field in ("country", "state", "city", "org", "ou"):
            if cfg.get("smlm_ssl_" + field):
                install_cmd += " --ssl-{} {}".format(field, shlex.quote(str(cfg["smlm_ssl_" + field])))
        if cfg.get("smlm_coco_replicas") is not None:
            install_cmd += " --coco-replicas {}".format(shlex.quote(str(cfg["smlm_coco_replicas"])))
        if cfg.get("smlm_coco_image"):
            install_cmd += " --coco-image {}".format(shlex.quote(cfg["smlm_coco_image"]))
        if cfg.get("smlm_coco_tag"):
            install_cmd += " --coco-tag {}".format(shlex.quote(cfg["smlm_coco_tag"]))
        run_install_with_pg_hba_guard(hostname, install_cmd)

        time.sleep(60)
        ssh_run(hostname, "reboot", check=False)
        time.sleep(5)
        check_ssh_conn(hostname)
    ensure_server_container_active(hostname)
    if cfg.get("smlm_image_admin_pass"):
        rotate_admin_password(hostname, admin, password, cfg["smlm_image_admin_pass"])
    if _is_true(cfg.get("smlm_salt_auto_accept")):
        ensure_salt_auto_accept(hostname)
    ensure_bootstrap_scripts(hostname, cfg.get("smlm_bootstrap_scripts") or [])

    print("SUSE Multi-Linux Manager available at: https://{}  ({} / {})".format(hostname, admin, password))

    # smlm_channels is a JSON array in the lab definition. The command is built from a normalized list, which accepts
    # either a list or a pre-joined string. Formatting a Python list directly would produce one malformed argument.
    channels = cfg.get("smlm_channels") or []
    if isinstance(channels, str):
        channels = channels.split()

    # smlm_beta_channels is merged into the channel list. mgr-sync treats BETA channels like any other channel.
    for c in cfg.get("smlm_beta_channels") or []:
        if c not in channels:
            channels.append(c)

    # The child channels of each activation key are added to the set of channels to sync, de-duplicated against
    # smlm_channels. An activation key refers to child channels that are otherwise never synced, such as the
    # managertools channels that provide venv-salt-minion. Without them the link call fails with "Invalid channel", and
    # clients bootstrapped with that key receive the wrong tooling.
    for key_cfg in ([cfg] + list(cfg.get("smlm_activation_keys") or [])):
        for c in (key_cfg.get("smlm_activation_key_child_channels") or "").split():
            if c not in channels:
                channels.append(c)

    if preinstalled and channels:
        present = _existing_channels(hostname, admin, password)
        channels = [c for c in channels if c not in present]
        print("- smlm_preinstalled: {} requested channel(s) missing from the image{}".format(
            len(channels), (": " + " ".join(channels)) if channels else " — skipping mgr-sync"))
    if channels or (cfg.get("smlm_activation_keys") and not preinstalled):
        # Registering the mirror credentials is required, not optional. mgr-sync uses the server's SCC organization
        # credentials to find the entitled channels and products. `mgr-sync add credentials` asks five questions: the
        # local admin's login and password, then the SCC user, the SCC password and its confirmation. Each answer must be
        # sent. Otherwise the call waits for input and ends with a general error, which this function does not report.
        # The five-line input completes with "Successfully added credentials."
        if not (scc_user and scc_password):
            die("smlm_channels/smlm_activation_keys are set but smlm_scc_user/smlm_scc_password "
                "are not — mgr-sync cannot see any entitled channels without the SCC organization "
                "credentials registered on '{}' first (`mgr-sync add credentials`)".format(hostname))
        print("- Registering SCC organization (mirror) credentials with mgr-sync")
        # -i is required. mgrctl exec does not forward stdin unless -i is given, so without it the input is dropped.
        ssh_run(hostname, "mgrctl exec -i -- mgr-sync add credentials",
                input_text="{}\n{}\n{}\n{}\n{}\n".format(admin, password, scc_user, scc_password, scc_password),
                check=False)

    if channels:
        # mgr-sync add credentials does not fill the product and channel catalog. A separate `mgr-sync refresh` is needed,
        # and it contacts the SCC API. The wait loop below has a timeout, and a refresh runs first, so the loop does not
        # depend on the server's own scheduled job.
        print("- Refreshing mgr-sync's product/channel catalog from SCC")
        ssh_run(hostname, "mgrctl exec -- mgr-sync refresh", check=False)

        count = 0
        max_retries = 60  # 10 min — the refresh above should make this near-instant; this
                           # bound exists only as a safety net, not the primary fix.
        print("- Waiting for channel list to sync")
        while True:
            time.sleep(10)
            count += 1
            print("Retry {}".format(count), end="\r")
            out = ssh_run(hostname, "mgrctl exec -- mgr-sync list channels 2>/dev/null",
                          check=False, capture=True).stdout or ""
            if any("no channels found." not in line.lower() for line in out.splitlines()):
                break
            if count >= max_retries:
                die("channel list on '{}' is still empty after mgr-sync refresh + {} retries "
                    "({} min) — check 'mgrctl exec -- mgr-sync refresh' and 'mgrctl exec -- "
                    "mgr-sync list channels' there directly".format(
                        hostname, max_retries, max_retries * 10 // 60))
        time.sleep(300)
        channel_args = " ".join(shlex.quote(c) for c in channels)
        ssh_run(hostname, "mgrctl exec -- mgr-sync add channels {}".format(channel_args))
        ensure_channel_sync_monitor(hostname, admin, password)
        ensure_bootstrap_repo_monitor(hostname)
        print("  NOTE: channels have been ADDED but not necessarily fully SYNCED yet — real "
              "package content is now downloading from SCC in the background, one channel at "
              "a time (see the channel-sync monitor above). Depending on how many channels and "
              "how large they are, this can legitimately take SEVERAL HOURS to finish. Clients "
              "registering against a channel that isn't fully synced yet won't fail — "
              "install_client_registration.py detects this and retries automatically in the "
              "background until it's ready — but don't expect every system to show up "
              "registered right away.")

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
    snippets = cfg.get("smlm_snippets") or []
    image_stores = cfg.get("smlm_image_stores") or []
    image_profiles = cfg.get("smlm_image_profiles") or []
    image_build_hosts = cfg.get("smlm_image_build_hosts") or []
    virtual_host_managers = cfg.get("smlm_virtual_host_managers") or []
    mcp_server_set = cfg.get("smlm_mcp_server") is not None  # {} is a valid "enable with
                                                              # defaults" value, not "unset"
    recurring_actions = cfg.get("smlm_recurring_actions") or []
    maintenance_calendars = cfg.get("smlm_maintenance_calendars") or []
    maintenance_schedules = cfg.get("smlm_maintenance_schedules") or []
    action_chains = cfg.get("smlm_action_chains") or []
    custom_channels = cfg.get("smlm_custom_channels") or []
    patches = cfg.get("smlm_patches") or []
    system_profiles = cfg.get("smlm_system_profiles") or []
    system_custom_values = cfg.get("smlm_system_custom_values") or []
    org_system_transfers = cfg.get("smlm_org_system_transfers") or []
    if (cfg.get("smlm_activation_key") or sync_channels or config_channels or orgs
            or access_groups or ansible_paths or content_projects or activation_keys
            or system_groups or custom_info_keys or system_tags or environments
            or distributions or kickstart_profiles or snippets or image_stores or image_profiles
            or image_build_hosts or virtual_host_managers or mcp_server_set
            or recurring_actions or maintenance_calendars or maintenance_schedules
            or action_chains or custom_channels or patches or system_profiles
            or system_custom_values or org_system_transfers
            or cfg.get("smlm_monitoring_enabled")):
        exec_prefix = "mgrctl exec --"
        sc.ensure_spacecmd_config(hostname, exec_prefix, admin, password)
        rps = sc.run_provisioning_step
        if virtual_host_managers:
            # The VHM credential resolution runs through rps(), like every other step. A missing or misnamed credential file
            # then skips only the VHM provisioning, not the rest of the server's configuration.
            rps("vhm credentials", _resolve_and_fill_vhm_credentials, cfg, virtual_host_managers)
        rps("channels sync", sc.ensure_channels_synced, hostname, exec_prefix, sync_channels)
        rps("config channels", sc.ensure_config_channels, hostname, exec_prefix, cfg, "smlm")
        # System groups are created before any activation key. The key's groups are linked with activationkey_addgroups,
        # which fails when a named group does not exist on the server yet.
        rps("monitoring", sc.ensure_monitoring, hostname, exec_prefix, cfg, "smlm")
        rps("system groups", sc.ensure_system_groups, hostname, exec_prefix, cfg, "smlm")
        # Snippets BEFORE distributions/kickstart profiles: a profile's own
        # %pre/%post/partitioning can reference one by name via its real
        # $SNIPPET(...) macro, so it should already exist first.
        rps("snippets", sc.ensure_snippets, hostname, exec_prefix, cfg, "smlm")
        rps("distributions", sc.ensure_distributions, hostname, exec_prefix, cfg, "smlm")
        rps("image stores", sc.ensure_image_stores, hostname, exec_prefix, cfg, "smlm")
        rps("image profiles", sc.ensure_image_profiles, hostname, exec_prefix, cfg, "smlm")
        # BEFORE any --import-images run: an import's own build_host_id needs a
        # system that already holds this entitlement — see this JSON section's
        # own smlm_image_build_hosts doc comment above.
        rps("image build hosts", sc.ensure_container_build_hosts, hostname, exec_prefix, cfg, "smlm")
        # activation key(s) depend on the system groups step just above (see
        # the comment on that reorder) — a couple of short retries smooth
        # over ordinary server-side propagation lag right after a group was
        # just created, without masking a real config mistake for long.
        rps("activation key", sc.ensure_activation_key, hostname, exec_prefix, cfg, "smlm",
            retries=3, retry_delay=15)
        rps("appstreams", sc.ensure_appstreams, hostname, exec_prefix, cfg, "smlm")
        rps("activation key packages", sc.ensure_activation_key_packages, hostname, exec_prefix, cfg, "smlm")
        rps("activation keys", sc.ensure_activation_keys, hostname, exec_prefix, cfg, "smlm",
            retries=3, retry_delay=15)
        # Kickstart profiles AFTER activation keys: a profile can link to one
        # via kickstart_addactivationkeys, which needs the key to already exist
        # — same ordering reasoning as system groups vs activation keys above.
        # (ensure_kickstart_profile() already self-skips cleanly when its own
        # distribution isn't populated yet — see that function's own
        # docstring — so retrying here is only for the activation-key link,
        # not for the known kickstart-tree gap.)
        rps("kickstart profiles", sc.ensure_kickstart_profiles, hostname, exec_prefix, cfg, "smlm",
            retries=3, retry_delay=15)
        rps("users", sc.ensure_users, hostname, exec_prefix, cfg, "smlm")
        rps("access groups", sc.ensure_access_groups, hostname, exec_prefix, cfg, "smlm")
        # Each step is independent of the ones before it. Each one runs through run_provisioning_step(), so a failure in
        # one step does not skip the steps after it.
        #
        # The Ansible control node and its paths get the largest retry window, because they depend on a client that
        # registers on its own schedule. Ten attempts, 60 seconds apart, give that client time to finish. A client that is
        # still unregistered after that is picked up by a later configuration run.
        #
        # ansible_control_node/ansible_paths get the most generous retry
        # window of anything in this block: they're the one real, CONFIRMED
        # cross-dependency on a system that registers on its own, completely
        # decoupled schedule (install_client_registration.py's background
        # retry workers) — 10 attempts 60s apart gives a client that's just
        # about to finish registering a real chance, without blocking this
        # whole run indefinitely for one that's genuinely still hours away
        # (that case still needs a later config run — see
        # run_provisioning_step()'s own docstring for why that's fine).
        rps("ansible control node", sc.ensure_ansible_control_node, hostname, exec_prefix, cfg, "smlm",
            retries=10, retry_delay=60)
        rps("ansible paths", sc.ensure_ansible_paths, hostname, exec_prefix, cfg, "smlm",
            retries=10, retry_delay=60)
        rps("content projects", sc.ensure_content_projects, hostname, exec_prefix, cfg, "smlm")
        rps("custom info keys", sc.ensure_custom_info_keys, hostname, exec_prefix, cfg, "smlm")
        # System custom VALUES right after the KEY definitions just above —
        # setCustomValues fails on an undefined key.
        rps("system custom values", sc.ensure_system_custom_values, hostname, exec_prefix, cfg, "smlm")
        rps("system tags", sc.ensure_system_tags, hostname, exec_prefix, cfg, "smlm")
        rps("environments", sc.ensure_environments, hostname, exec_prefix, cfg, "smlm")
        rps("grafana formula", sc.ensure_grafana_formula, hostname, exec_prefix, cfg, "smlm")
        rps("virtual host managers", sc.ensure_virtual_host_managers, hostname, exec_prefix, cfg, "smlm")
        rps("mcp server", sc.ensure_mcp_server, hostname, exec_prefix, cfg, "smlm")
        # Custom software channels BEFORE the patches that get created in
        # them — errata.create needs the channel to already exist and be
        # allow-listed (see ensure_patches' own docstring).
        rps("custom channels", sc.ensure_custom_channels, hostname, exec_prefix, cfg, "smlm")
        rps("patches", sc.ensure_patches, hostname, exec_prefix, cfg, "smlm")
        rps("recurring actions", sc.ensure_recurring_schedules, hostname, exec_prefix, cfg, "smlm")
        rps("maintenance calendars", sc.ensure_maintenance_calendars, hostname, exec_prefix, cfg, "smlm")
        rps("maintenance schedules", sc.ensure_maintenance_schedules, hostname, exec_prefix, cfg, "smlm")
        rps("action chains", sc.ensure_action_chains, hostname, exec_prefix, cfg, "smlm")
        rps("system profiles", sc.ensure_system_profiles, hostname, exec_prefix, cfg, "smlm")
        # Organizations run last and its own per-org steps (activation keys,
        # system groups, users) mirror the same top-level dependencies above
        # — same modest retry window.
        rps("organizations", sc.ensure_orgs, hostname, exec_prefix, cfg, "smlm", admin, password,
            retries=3, retry_delay=15)
        # System transfers AFTER organizations: org.transferSystems needs
        # the destination org to already exist and (per its own real API
        # requirement) be in a trust relationship with the source org —
        # both established by the "organizations" step just above via that
        # org's own trust_with field.
        rps("org system transfers", sc.ensure_org_system_transfers, hostname, exec_prefix, cfg, "smlm")


_CHANNEL_SYNC_MONITOR_SCRIPT = """#!/bin/bash
# Installed by lab-in-a-box's install_smlm.py (ensure_channel_sync_monitor). It checks every software channel on this
# SMLM server for a completed reposync, and re-triggers any channel that never synced, failed, or was interrupted, for
# example by a server restart. It runs periodically through smlm-channel-sync-monitor.timer, with the .service unit
# next to this file.
#
# It triggers at most one resync per run. spacewalk-repo-sync allows a single instance on the server, and a second
# attempt exits with an error. Triggering every pending channel at once would make them race for that one slot, and the
# losers would wait a full cycle. One trigger per run, only when nothing is running, gives each one a real chance to run.
set -uo pipefail

LOG_DIR=/var/log/rhn/reposync
LOGFILE=/var/log/smlm-channel-sync-monitor.log
ADMIN=__ADMIN__
PASSWORD=__PASSWORD__
ERRFILE=$(mktemp)
trap 'rm -f "$ERRFILE"' EXIT

log() {
    echo "$(date -Is) $*" >> "$LOGFILE"
}

# spacecmd_() captures stderr and checks it with check_spacecmd_error(), instead of discarding it. Errors such as a
# stale cached session or a failed connection are logged, and the run exits non-zero, so the failure shows in
# `systemctl status` or the journal. Routine INFO lines, for example the connection banner, do not match the error
# keywords and are ignored. The timer runs again at the next cycle, without retrying in a tight loop.
spacecmd_() {
    podman exec uyuni-server spacecmd -u "$ADMIN" -p "$PASSWORD" -- "$@" 2>"$ERRFILE"
}

check_spacecmd_error() {
    if [ -s "$ERRFILE" ] && grep -qiE 'error|invalid|traceback|denied|refused|fail' "$ERRFILE"; then
        log "spacecmd call failed: $(tr '\n' ' ' < "$ERRFILE")"
        exit 1
    fi
}

if podman exec uyuni-server pgrep -f spacewalk-repo-sync >/dev/null 2>&1; then
    log "a reposync is already running -- nothing to trigger this cycle"
    exit 0
fi

CHANNELS=$(spacecmd_ softwarechannel_list)
check_spacecmd_error
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
        check_spacecmd_error
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
    Deploy a host-level systemd service and timer that checks every software channel on the SMLM server, and re-triggers
    the ones that never synced, failed, or were interrupted. A restart can leave a half-downloaded channel log with no
    "Sync completed." line and no error, and nothing else notices.

    The monitor runs on the host, not inside the uyuni-server container. That container runs with --rm and is recreated on every
    restart, so anything installed inside it, including the timer, would be lost. The monitor reaches into the container with
    podman exec for each check and action.

    Per channel, the monitor reads /var/log/rhn/reposync/<label>.log inside the container:
      - no log file: never synced
      - the last lines contain "error" or "traceback": failed
      - no "Sync completed." line and no spacewalk-repo-sync process running for that channel: interrupted
      - otherwise: healthy, left alone

    A channel is re-triggered with 'spacecmd softwarechannel_syncrepos <label>', which resumes a stuck sync. mgr-sync sync channel
    is not used, because it needs an interactive credential prompt and cannot run unattended from a timer.
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


_BOOTSTRAP_REPO_MONITOR_SCRIPT = """#!/bin/bash
# Installed by lab-in-a-box's install_smlm.py (ensure_bootstrap_repo_monitor). It retries mgr-create-bootstrap-repo for
# each distribution whose bootstrap repository failed to build. The usual cause is venv-salt-minion, which is not yet in
# the locally synced Tools/managertools channel content. It runs periodically through
# smlm-bootstrap-repo-monitor.timer, with the .service unit next to this file.
#
# The script does not use the tool's --auto mode. Once --auto has attempted a distribution, even one that failed, a later
# --auto run reports "Nothing to do" for it and never retries it. The script uses --list for discovery, which is a
# metadata-only listing, and --create <label> for every build or retry.
#
# It makes one explicit build or retry per cycle. A label that still fails because its channel is not synced yet is
# retried next cycle, and a label that succeeds moves to DONE_DIR. The queue therefore converges with one attempt in
# flight at a time.
set -uo pipefail

LOGFILE=/var/log/smlm-bootstrap-repo-monitor.log
STATE_DIR=/var/lib/smlm-bootstrap-repo-monitor
PENDING_DIR=$STATE_DIR/pending
DONE_DIR=$STATE_DIR/done
mkdir -p "$PENDING_DIR" "$DONE_DIR"

log() {
    echo "$(date -Is) $*" >> "$LOGFILE"
}

mcbr() {
    podman exec uyuni-server mgr-create-bootstrap-repo "$@"
}

# Discovery: any real distribution label this server currently knows about
# that isn't already DONE (built successfully by a previous cycle) or
# already PENDING (queued from a previous cycle, still being retried) is a
# newly-seen one — queue it. Covers both the very first run (nothing is
# DONE or PENDING yet, so every label gets queued) and a distribution/
# product that only appeared later (e.g. a lab JSON adding a new
# activation key and re-running install_smlm.py), with no need for --auto's
# own separate "changed products" bookkeeping.
while IFS= read -r label; do
    [ -n "$label" ] || continue
    if [ ! -e "$DONE_DIR/$label" ] && [ ! -e "$PENDING_DIR/$label" ]; then
        touch "$PENDING_DIR/$label"
        log "distribution '$label': newly seen -- queued"
    fi
done < <(mcbr --list 2>/dev/null | sed -E 's/^[0-9]+\\.\\s*//')

# --list omits products that the tool considers not connected to the CDN, even when their channel content is fully
# synced. --create <label> builds those products correctly and only prints a warning. The discovery step therefore uses
# --auto --dryrun, which lists the same products without touching the disk, to catch these names. Each one is queued
# like any other label, and the build still goes through --create.
while IFS= read -r label; do
    [ -n "$label" ] || continue
    if [ ! -e "$DONE_DIR/$label" ] && [ ! -e "$PENDING_DIR/$label" ]; then
        touch "$PENDING_DIR/$label"
        log "distribution '$label': not connected to CDN per --list, but its own channel "
        log "distribution '$label': content may already be ready -- queued for an explicit --create attempt"
    fi
done < <(mcbr --auto --dryrun 2>&1 | sed -nE 's/^(WARNING: )?([A-Za-z0-9_.-]+) not connected to CDN\\.?.*/\\2/p')

# One pending distribution is built or retried per cycle, chosen by the oldest marker file (ls -tr). A failed retry
# touches its marker, which moves it to the back of the queue. The queue then rotates through every pending
# distribution, instead of retrying the first alphabetical one forever.
retry_label=$(ls -tr "$PENDING_DIR" 2>/dev/null | head -n1)
if [ -n "$retry_label" ]; then
    RETRY_OUT=$(mcbr --create "$retry_label" 2>&1)
    if [ $? -eq 0 ] && ! echo "$RETRY_OUT" | grep -q '^ERROR:'; then
        rm -f "$PENDING_DIR/$retry_label"
        touch "$DONE_DIR/$retry_label"
        log "distribution '$retry_label': bootstrap repo created successfully"
    else
        err_line=$(echo "$RETRY_OUT" | grep '^ERROR:' | head -n1)
        log "distribution '$retry_label': still failing -- ${err_line:-see the mgr-create-bootstrap-repo log for details}"
        touch "$PENDING_DIR/$retry_label"
    fi
fi
"""

_BOOTSTRAP_REPO_MONITOR_SERVICE = """[Unit]
Description=Check SMLM bootstrap repositories for build failures and retry
After=uyuni-server.service
Wants=uyuni-server.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/smlm-bootstrap-repo-monitor.sh
"""

_BOOTSTRAP_REPO_MONITOR_TIMER = """[Unit]
Description=Periodically retry failed SMLM bootstrap repository builds

[Timer]
OnBootSec=10min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
"""


def ensure_bootstrap_repo_monitor(hostname):
    """
    Deploy a host-level systemd service and timer that retries mgr-create-bootstrap-repo for each distribution whose
    bootstrap repository build still fails. The reason for host-level placement is the same as for ensure_channel_sync_monitor().

    A distribution fails with "package 'venv-salt-minion' not found" until the Tools/managertools channel content is synced. A
    fresh server can still be syncing that channel for hours. mgr-create-bootstrap-repo --auto never retries a distribution it
    already attempted, so a distribution that failed during the sync stays broken. This timer revisits it until the repository
    builds.

    The monitor also finds products that --list omits. The tool leaves out any product it considers not connected to the CDN.
    Those products build correctly when named directly, so the monitor runs --auto --dryrun to discover them as well. See the
    script's own comment for the retry design.
    """
    print("- Installing the bootstrap-repository failure monitor (checks every 15 min)")
    ssh_run(hostname, "cat > /usr/local/sbin/smlm-bootstrap-repo-monitor.sh <<'EOF'\n{}EOF".format(
        _BOOTSTRAP_REPO_MONITOR_SCRIPT), check=False)
    ssh_run(hostname, "chmod 755 /usr/local/sbin/smlm-bootstrap-repo-monitor.sh", check=False)
    ssh_run(hostname, "cat > /etc/systemd/system/smlm-bootstrap-repo-monitor.service <<'EOF'\n{}EOF".format(
        _BOOTSTRAP_REPO_MONITOR_SERVICE), check=False)
    ssh_run(hostname, "cat > /etc/systemd/system/smlm-bootstrap-repo-monitor.timer <<'EOF'\n{}EOF".format(
        _BOOTSTRAP_REPO_MONITOR_TIMER), check=False)
    ssh_run(hostname, "systemctl daemon-reload && systemctl enable --now smlm-bootstrap-repo-monitor.timer",
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
    # smlm_fqdn is free text. shlex.quote() escapes any quote in the value, so it cannot break or inject into the remote
    # command, even when it is nested inside the heredoc.
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
        sc.ensure_ansible_control_node(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_ansible_paths(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_content_projects(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_scap_policies(hostname, exec_prefix, cfg, "smlm",
                                cfg.get("smlm_admin_user") or "admin", cfg.get("smlm_admin_pass") or "admin123")
        sc.ensure_custom_info_keys(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_system_tags(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_environments(hostname, exec_prefix, cfg, "smlm")
        sc.ensure_grafana_formula(hostname, exec_prefix, cfg, "smlm")
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


def _trigger_exec_prefix(hostname, cfg):
    """
    Return the exec_prefix used by the explicit-trigger functions: run_ansible_playbooks, run_clm_actions, run_scap_scans,
    cve_audit, cve_audit_images and run_recurring_schedules. It follows smlm_deployment. Podman deployments use mgrctl and
    Kubernetes deployments use kubectl, the same dispatch as setup_smlm_podman() and setup_smlm_kubernetes().
    """
    if (cfg.get("smlm_deployment") or "kubernetes") == "podman":
        return "mgrctl exec --"
    ns = ac.require_k8s_name(cfg, "smlm_ns", "uyuni-server")
    return "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)


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
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
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
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
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
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    sc.run_scap_scans(hostname, exec_prefix, cfg, "smlm")


def sync_maintenance_calendars(hostname, cfg):
    """
    Pushes every entry in smlm_maintenance_calendars (see the JSON section
    comment above) to the live server via sc.ensure_maintenance_calendars —
    genuinely idempotent (creates a missing calendar, updates one whose
    content differs, no-ops if it already matches — see
    libs/spacecmd_common.py's sync_maintenance_calendar()), but still a
    separate, explicit trigger rather than folded into the full automatic
    setup_smlm() flow: this lets a calendar's schedule be pushed to an
    already-provisioned live server surgically, without re-running every
    other provisioning step against it too.
    """
    calendars = cfg.get("smlm_maintenance_calendars") or []
    if not calendars:
        print("No smlm_maintenance_calendars entries in the 'smlm' JSON section — nothing to sync.")
        return
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    sc.ensure_maintenance_calendars(hostname, exec_prefix, cfg, "smlm")


def cve_audit(hostname, cfg, cve_id):
    """Prints audit.listSystemsByPatchStatus's raw result for `cve_id` — a
    pure read-only query, see libs/spacecmd_common.py."""
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    print(sc.list_systems_by_patch_status(hostname, exec_prefix, cve_id))


def cve_audit_images(hostname, cfg, cve_id):
    """Prints audit.listImagesByPatchStatus's raw result for `cve_id` — the
    container/OS-image counterpart of cve_audit() above, same real 'audit'
    namespace, see libs/spacecmd_common.py's list_images_by_patch_status()."""
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
    admin_user = cfg.get("smlm_admin_user") or "admin"
    admin_pass = cfg.get("smlm_admin_pass") or "admin123"
    sc.ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)
    print(sc.list_images_by_patch_status(hostname, exec_prefix, cve_id))


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
    exec_prefix = _trigger_exec_prefix(hostname, cfg)
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
             "       {0} <lab.json> --sync-maintenance-calendars   # push smlm_maintenance_calendars\n"
             "       {0} <lab.json> --cve-audit CVE-YYYY-NNNNN   # patch-status audit for one CVE\n"
             "       {0} <lab.json> --cve-audit-images CVE-YYYY-NNNNN   # same, for images\n"
             "       {0} <lab.json> --run-recurring-schedules   # create smlm_environments' recurring schedules\n"
             "       {0} <lab.json> --import-images   # schedule smlm_image_imports\n"
             "       {0} <lab.json> --build-images   # schedule smlm_image_builds\n"
             "       {0} <lab.json> --enable-sso   # deploy Keycloak + enable smlm_sso\n"
             "       {0} <lab.json> --provision-guests   # schedule smlm_virtual_guests\n"
             "       {0} <lab.json> --enable-ansible-control-nodes   # enable smlm_ansible_control_nodes"
             ).format(Path(__file__).name)
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, usage=usage, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    cfg = definition.get("smlm", {}) or {}
    # smlm_scc_user/smlm_scc_password/smlm_scc_regcode may alternatively come
    # from an encrypted "scc"-kind credential file (see README's Credentials
    # section) — resolved once, here, so every call site below (podman-mode
    # install, Kubernetes-mode registry secret, the validation checks further
    # down) sees the SAME already-resolved values with zero code change of
    # its own. A fresh dict, not mutating the caller's own definition —
    # falls straight through to today's plaintext values unchanged whenever
    # no matching credential file is used.
    scc_creds = ac.resolve_credential(cfg, "scc", {
        "scc_user": "smlm_scc_user", "scc_password": "smlm_scc_password",
        "scc_regcode": "smlm_scc_regcode",
    }, account_key="smlm_scc_account")
    cfg = dict(cfg)
    cfg["smlm_scc_user"] = scc_creds["scc_user"]
    cfg["smlm_scc_password"] = scc_creds["scc_password"]
    cfg["smlm_scc_regcode"] = scc_creds["scc_regcode"]

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

        # The explicit-trigger flags (--run-scap-scans, --run-ansible-playbooks, --run-clm-actions, --cve-audit,
        # --cve-audit-images and --run-recurring-schedules) are handled here for the podman deployment too. Before this,
        # they were checked only in the Kubernetes branch, which the podman branch returns from first. The shared run_*()
        # functions pick their exec_prefix through _trigger_exec_prefix().
        _podman_triggers = {
            "--run-ansible-playbooks": run_ansible_playbooks,
            "--run-clm-actions": run_clm_actions,
            "--run-scap-scans": run_scap_scans,
            "--sync-maintenance-calendars": sync_maintenance_calendars,
            "--run-recurring-schedules": run_recurring_schedules,
        }
        if len(sys.argv) > 2 and sys.argv[2] in _podman_triggers:
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            _podman_triggers[sys.argv[2]](nodes[0][0], cfg)
            return

        if len(sys.argv) > 3 and sys.argv[2] in ("--cve-audit", "--cve-audit-images"):
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            (cve_audit if sys.argv[2] == "--cve-audit" else cve_audit_images)(
                nodes[0][0], cfg, sys.argv[3])
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

        # Schedule smlm_image_builds — same explicit-trigger reasoning as
        # --import-images just above (scheduling a build is one-shot, real work).
        if len(sys.argv) > 2 and sys.argv[2] == "--build-images":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            admin = cfg.get("smlm_admin_user") or "admin"
            password = cfg.get("smlm_admin_pass") or "Smlm12345"
            sc.ensure_spacecmd_config(nodes[0][0], "mgrctl exec --", admin, password)
            sc.build_images(nodes[0][0], "mgrctl exec --", cfg, "smlm")
            return

        # Deploy Keycloak + enable real SAML 2.0 SSO (smlm_sso) — explicit
        # trigger, not automatic: per Uyuni's own real docs, enabling SSO
        # makes the WEB UI's login SSO-only from then on (spacecmd/mgr-sync
        # and this project's own automation are unaffected — they keep
        # using password auth, confirmed in the same docs), a real,
        # user-visible behavior change deliberate enough to want its own
        # explicit trigger rather than happening on every routine run.
        if len(sys.argv) > 2 and sys.argv[2] == "--enable-sso":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            admin = cfg.get("smlm_admin_user") or "admin"
            password = cfg.get("smlm_admin_pass") or "Smlm12345"
            sc.ensure_spacecmd_config(nodes[0][0], "mgrctl exec --", admin, password)
            sc.ensure_sso(nodes[0][0], "mgrctl exec --", cfg, "smlm")
            return

        # Provision smlm_virtual_guests — real, one-shot new-VM creation via
        # system.provisionVirtualGuest, same explicit-trigger reasoning as
        # --build-images/--import-images above.
        if len(sys.argv) > 2 and sys.argv[2] == "--provision-guests":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            admin = cfg.get("smlm_admin_user") or "admin"
            password = cfg.get("smlm_admin_pass") or "Smlm12345"
            sc.ensure_spacecmd_config(nodes[0][0], "mgrctl exec --", admin, password)
            sc.provision_virtual_guests(nodes[0][0], "mgrctl exec --", cfg, "smlm")
            return

        # Enable smlm_ansible_control_nodes' entitlement (+ highstate apply) AND
        # register smlm_ansible_paths, without re-running the whole podman install —
        # same reasoning as --import-images: a scoped, explicit trigger, not folded
        # into the automatic flow's own already-idempotent calls, just faster to
        # reach on an already-installed server than a full setup_smlm_podman()
        # re-run. Mirrors the automatic flow's own ordering (entitlement first,
        # since a path registration on a system that isn't yet a recognised control
        # node was never confirmed to work).
        if len(sys.argv) > 2 and sys.argv[2] == "--enable-ansible-control-nodes":
            nodes = list(k8s.addon_nodes(definition, "smlm", vm_name=env_vm_name))
            if not nodes:
                print("ERROR: no node with the 'smlm' addon found in '{}'".format(json_file),
                      file=sys.stderr)
                sys.exit(1)
            admin = cfg.get("smlm_admin_user") or "admin"
            password = cfg.get("smlm_admin_pass") or "Smlm12345"
            sc.ensure_spacecmd_config(nodes[0][0], "mgrctl exec --", admin, password)
            sc.ensure_ansible_control_node(nodes[0][0], "mgrctl exec --", cfg, "smlm")
            sc.ensure_ansible_paths(nodes[0][0], "mgrctl exec --", cfg, "smlm")
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

    # Push smlm_maintenance_calendars to an already-provisioned live server
    # instead of installing when requested — genuinely idempotent (unlike
    # the triggers above), but still explicit so it can run surgically
    # against a live server without re-running the whole automatic flow.
    if len(sys.argv) > 2 and sys.argv[2] == "--sync-maintenance-calendars":
        sync_maintenance_calendars(vm_name, cfg)
        return

    # Ad-hoc CVE/OVAL patch-status audit — read-only, takes the CVE id as a
    # third argument.
    if len(sys.argv) > 3 and sys.argv[2] == "--cve-audit":
        cve_audit(vm_name, cfg, sys.argv[3])
        return

    # audit.listImagesByPatchStatus's own CLI entry point — same shape as
    # --cve-audit above, for container/OS images instead of systems.
    if len(sys.argv) > 3 and sys.argv[2] == "--cve-audit-images":
        cve_audit_images(vm_name, cfg, sys.argv[3])
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
