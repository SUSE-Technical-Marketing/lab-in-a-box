#!/usr/bin/env python3.11
# Part of lab-in-a-box — schema of the base lab definition
# Author/s: Raul Mahiques
# License: GPLv3

"""
libs/lab_schema_base.py: the schema of the base lab definition — the common, nodes and kclusters sections of a
lab.json, every field with its type and, where it has one, its default, enum, pattern and bounds.

The single source of truth for those three sections: `lab_schema --base` and `setup_lab.py --input-definition` emit
it, the web builder renders its fields, and libs/schema_validate.py checks a lab definition's values against it.
LAB_SCHEMA_VERSION is its own schema version, recorded in schemas/compatibility.json as "lab".
"""

__version__ = "__LABVERSION__"


LAB_SCHEMA_VERSION = "2.3"


def base_lab_schema():
    """
    Schema of the base lab definition (common / nodes / kclusters).

    Single source of truth: `setup_lab.sh --schema` emits this via `lab_schema
    --base`, and the web builder imports it directly. `nodes` and `kclusters`
    are keyed maps (hostname / cluster name -> settings), marked "repeatable"
    so a consumer knows to collect one-or-more instances rather than a single
    object.
    """
    return {
        "schema_version": LAB_SCHEMA_VERSION,
        "component": "lab-definition",
        "description": "lab-in-a-box lab definition file — passed as the first "
                       "argument to setup_lab.py and setup_vm.py",
        "sections": {
            "common": {
                "description": "Shared settings applied to every VM in the lab",
                "fields": [
                    {"name": "SOURCE_IMAGE", "required": True, "type": "string", "description": "Source image: local filename (in ISO_LOC), URL (http://, https://, ftp://, nfs://), or local file path"},
                    {"name": "SOURCE_SHA256", "required": False, "type": "string", "description": "Checksum or URL: inline hex checksum (32/40/64/128 chars), URL to fetch checksum from, or local checksum file path"},
                    {"name": "ISO_IMAGE", "required": False, "type": "string", "hidden": True, "description": "DEPRECATED: use SOURCE_IMAGE instead"},
                    {"name": "VM_MEM", "required": True, "type": "integer", "min": 256, "description": "RAM in MiB (e.g. 8192)"},
                    {"name": "VM_DSK", "required": True, "type": "integer", "min": 1, "description": "Disk size in GiB (e.g. 80)"},
                    {"name": "VM_CPU", "required": True, "type": "integer", "min": 1, "description": "vCPU count"},
                    {"name": "mymask", "required": False, "type": "integer", "description": "Network prefix length (default: auto-detected from host)"},
                    {"name": "mygw", "required": False, "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}$", "description": "Default gateway IP (default: auto-detected)"},
                    {"name": "mydns", "required": False, "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}$", "description": "DNS server IP (default: auto-detected)"},
                    {"name": "mynet_reverse", "required": False, "type": "string", "description": "Reverse DNS zone prefix e.g. 88.168.192 (default: derived from first node IP)"},
                    {"name": "mydomain", "required": False, "type": "string", "description": "Lab DNS domain (default: derived from automation VM hostname)"},
                    {"name": "VM_BOOT", "required": False, "type": "string", "default": "uefi", "enum": ["uefi", "firmware=bios", "bios", "uefi=off", "hd"], "description": "virt-install boot mode"},
                    {"name": "VM_DSK_BUS", "required": False, "type": "string", "default": "virtio", "enum": ["virtio", "scsi", "sata", "usb", "ide"], "description": "Disk bus type for the primary disk"},
                    {"name": "VM_NET_MODEL", "required": False, "type": "string", "default": "virtio", "enum": ["virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci"], "description": "NIC device model"},
                    {"name": "VM_OSVARIANT", "required": False, "type": "string", "default": "slem5.4", "description": "OS variant for virt-install (osinfo-query os)"},
                    {"name": "VM_MACHINE", "required": False, "type": "string", "default": "",
                     "enum": [{"value": "", "label": "(default) virt-install's own choice, currently q35"}, "pc"],
                     "description": "virt-install machine type override — set to \"pc\" (legacy i440fx) for an "
                     "old guest whose kernel/GRUB can't find its root disk under q35 (confirmed live for a "
                     "2015-era CentOS 7 GenericCloud image); leave unset for every image that already works"},
                    {"name": "config_method", "required": False, "type": "string", "default": "",
                     "enum": [{"value": "", "label": "(default) Ignition+Combustion"}, "cloud-init", "virt_customize", "install_iso"],
                     "description": "Provisioning method"},
                    {"name": "network_renderer", "required": False, "type": "string", "default": "NetworkManager",
                     "enum": ["NetworkManager", "networkd", "sysconfig"],
                     "description": "cloud-init network-config's \"renderer\" value (config_method=cloud-init only) — the default matches SLE Micro/SLES/Leap's own default network stack; set to \"networkd\" for guests whose distro defaults to systemd-networkd instead (e.g. Ubuntu Server, Debian's generic cloud image), or \"sysconfig\" for guests using legacy ifcfg-file networking with NetworkManager not even enabled (e.g. Alibaba Cloud Linux 2's alibase image) — otherwise their static IP is silently never applied"},
                    {"name": "install_type", "required": False, "type": "string", "default": "",
                     "enum": [{"value": "", "label": "(default) auto-detect from ISO_IMAGE name"}, "autoyast", "kickstart", "preseed", "autoinstall"],
                     "description": "For install_iso only"},
                    {"name": "VM_ROOT_PASS", "required": False, "type": "string", "default": "", "description": "Plaintext root password for virt_customize; omit to reuse ROOT_PWD_HASH from lab_creation.cfg (same as cloud-init/Ignition)"},
                    {"name": "BRIDGE", "required": False, "type": "string", "default": "br0", "description": "Host bridge every VM's NIC attaches to (virt-install --network bridge=<BRIDGE>,mac.address=<mymac>), e.g. virbr0 for libvirt's default NAT network"},
                    {"name": "ROOT_PWD_HASH", "required": False, "type": "password", "description": "crypt(3) hash of the VMs' root password for cloud-init/Ignition (overrides lab_creation.cfg's own ROOT_PWD_HASH)"},
                    {"name": "open_ports", "required": False, "type": "array", "description": "Cloud backends: ports reachable from anywhere on each VM (list, e.g. [\"443\", \"69/udp\"]; default protocol tcp) — aws: security-group rules (needs AWS_SECURITY_GROUP_ID); gcp: a firewall rule + network tag; alibaba: ALIBABA_SECURITY_GROUP_ID rules; exoscale: a per-VM security group; hetzner/scaleway/upcloud/ovhcloud: open by default"},
                    {"name": "aws_open_ports", "required": False, "type": "array", "description": "backend aws (older form of open_ports): ports opened to 0.0.0.0/0 in the security group (list, e.g. [\"443\", \"69/udp\"]; default protocol tcp) — needs AWS_SECURITY_GROUP_ID"},
                    {"name": "ISO_SHA256", "required": False, "type": "string", "hidden": True, "description": "DEPRECATED: use SOURCE_SHA256 instead"},
                    {"name": "ssh_pwauth", "required": False, "type": "string", "default": "false", "enum": ["true", "false"], "description": "config_method=cloud-init only: \"true\" enables SSH password login (cloud-init ssh_pwauth) and root login — for labs whose exercises log in or bootstrap clients with the root password"},
                    {"name": "lab_name", "required": False, "type": "string", "description": "Human-readable lab name shown in log output"},
                    {"name": "lab_description", "required": False, "type": "string", "description": "One line saying what this lab is for, shown where a lab is listed (the lab-builder's example and saved-lab lists)"},
                    {"name": "backend", "required": False, "type": "string", "default": "libvirt", "enum": ["libvirt", "harvester", "hetzner", "aws", "gcp", "alibaba", "scaleway", "upcloud", "ovhcloud", "exoscale"], "description": "Compute backend for every node in this lab, unless overridden per-node"},
                    {"name": "cloud_account", "required": False, "type": "string", "description": "Name of a cloud account file under /etc/lab_creation/credentials/<name>.{yaml,json,cfg} (path configurable via lab_creation.cfg's CREDENTIALS_PATH; multiple cloud accounts, the same way KVM_HOSTS gives multiple hypervisors) — see scripts/setup_credentials.py to create one. Encrypted by default; the file's `cloudtype` becomes the backend (so `backend` is then optional) and its keys (AWS_REGION, etc.) are layered over lab_creation.cfg. Omit for the single default account."},
                    {"name": "cloud_instance_type", "required": False, "type": "string", "description": "Cloud backends only: an explicit instance type/server type/plan/flavor name, used verbatim instead of auto-picking one from vm_cpu/vm_mem — see README's Compute backends table for each provider's own official list of valid values"},
                    {"name": "pre_commands", "required": False, "type": "array", "description": "Shell commands run on the VM itself, as root, once it is up and reachable and before any Kubernetes or add-on work (e.g. [\"zypper -n in vim\"]). Each one's output is shown; one that fails is an error for that VM and the rest still run. A node's own list replaces this one — set [] on a node to run nothing there. Not run for a clu_type harvester node, which installs itself at first boot."},
                    {"name": "post_commands", "required": False, "type": "array", "description": "Shell commands run on the VM itself, as root, after everything else for it is done — its Kubernetes join and its own add-ons included. Same reporting and per-node override as pre_commands."},
                    {"name": "services", "required": False, "type": "array", "description": "Lab-support services to configure+enable before VMs are created: dns, http (both already running from the automation VM bootstrap; listing them is a no-op today), pxe (see the top-level \"pxe\" section), portforward (DNAT-forwards each node's own \"forwarded_ports\" field from the KVM host's real IP — see that per-node field). Omit for today's default (nothing extra runs)."},
                    {"name": "overlay", "required": False, "type": "boolean", "default": False, "description": "Routes this lab's sites (each cloud account, and the home libvirt lab) to each other by their REAL subnets, site-to-site, over a hub-and-spoke WireGuard overlay (libs/overlay.py) — needed since a cloud node generally cannot route to another cloud account's private network, or to the home lab, any other way. Only each site's own automation VM (or a small dedicated gateway VM, auto-created/reused per non-hub cloud account) ever joins the overlay itself; individual lab nodes just get a local route to other sites' subnets via their own site's gateway — no per-node WireGuard membership. The hub is the gateway VM for lab_creation.cfg's designated OVERLAY_HUB_ACCOUNT (required). Also configurable via OVERLAY_CIDR (default 10.99.0.0/16, used only for the gateways' own admin addresses) and OVERLAY_WG_PORT (default 51820) in lab_creation.cfg. Known gaps: no DNS changes are made (a node is reachable by its real hostname only once that name already resolves into a now-routed subnet); a site's already-provisioned nodes aren't retroactively re-routed when a new remote site joins later, only at each node's own provisioning time; the AWS-specific source/dest-check disable needed for a cloud site gateway to forward traffic has no equivalent yet for the other 7 cloud backends."},
                ],
            },
            "nodes": {
                "description": "Map of fully-qualified VM hostname to per-node settings. Keys override common values.",
                "repeatable": True,
                "key_label": "hostname (FQDN)",
                "fields": [
                    {"name": "myip", "required": True, "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}$", "description": "VM IP address — required for libvirt/harvester; leave empty for a cloud backend (aws/gcp/hetzner/etc.), whose real IP is only known once the provider assigns it at create time"},
                    {"name": "mymac", "required": False, "type": "string", "pattern": r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", "description": "VM MAC address (generated and conflict-checked if omitted, libvirt/harvester only — a cloud backend has no MAC concept and ignores this field entirely)"},
                    {"name": "kcluster", "required": False, "type": "string", "description": "Kubernetes cluster this node belongs to (a name in kclusters)"},
                    {"name": "KUBERNETES_NODE_TYPE", "required": False, "type": "string", "default": "server", "enum": ["server", "agent"], "show_if": "kcluster", "description": "Kubernetes node role (server/control-plane or agent/worker)"},
                    {"name": "harvester_role", "required": False, "type": "string", "enum": ["default", "management", "worker", "witness"], "show_if": "kcluster", "description": "Harvester node role (clu_type harvester only; default: Harvester decides)"},
                    {"name": "INSTALL_RKE2_TYPE", "required": False, "type": "string", "default": "server", "enum": ["server", "agent"], "show_if": "kcluster", "hidden": True, "description": "DEPRECATED: use KUBERNETES_NODE_TYPE instead"},
                    {"name": "addons", "required": False, "type": "array", "description": "List of VM-level addon names (install_<addon> scripts). Each entry is either a plain \"<addon>\" string (runs with only that addon's shared top-level config section, same as always), or a single-key {\"<addon>\": {...fields...}} mapping to override that addon's config for this node only — e.g. a lab registering many different OSes against one shared Uyuni/SMLM server, each node needing its own client_registration_activation_key. See README's addon-config section."},
                    {"name": "pre_commands", "required": False, "type": "array", "description": "Overrides common.pre_commands for this node only — shell commands run on the VM itself, as root, once it is up and reachable and before any Kubernetes or add-on work (e.g. [\"zypper -n in vim\"]). Each one's output is shown; one that fails is an error for that VM and the rest still run. Not run for a clu_type harvester node, which installs itself at first boot."},
                    {"name": "post_commands", "required": False, "type": "array", "description": "Overrides common.post_commands for this node only — shell commands run on the VM itself, as root, after everything else for it is done — its Kubernetes join and its own add-ons included. Same reporting as pre_commands."},
                    {"name": "extra_dsk", "required": False, "type": "string", "description": "Additional disk paths or UUID=… entries (space-separated); per-disk bus: /dev/sdb,bus=scsi"},
                    # VM_MEM/VM_DSK/VM_CPU/VM_BOOT/VM_DSK_BUS/VM_NET_MODEL/VM_OSVARIANT/
                    # VM_ROOT_PASS: load_vm_vars() (libs/lab_creation.py) merges 'common'
                    # then overlays EVERY key present in a node's own dict, so all of
                    # these are already genuinely overridable per-node at the code
                    # level — this schema simply never exposed them as such before
                    # (reported live 2026-09-01: the webui had no way to override
                    # common's CPU/memory/disk defaults for an individual node,
                    # because the fields to do so didn't exist here at all).
                    {"name": "VM_MEM", "required": False, "type": "integer", "min": 256, "description": "Overrides common.VM_MEM for this node only"},
                    {"name": "VM_DSK", "required": False, "type": "integer", "min": 1, "description": "Overrides common.VM_DSK for this node only"},
                    {"name": "VM_CPU", "required": False, "type": "integer", "min": 1, "description": "Overrides common.VM_CPU for this node only"},
                    {"name": "VM_BOOT", "required": False, "type": "string", "enum": ["uefi", "firmware=bios", "bios", "uefi=off", "hd"], "description": "Overrides common.VM_BOOT for this node only"},
                    {"name": "VM_DSK_BUS", "required": False, "type": "string", "enum": ["virtio", "scsi", "sata", "usb", "ide"], "description": "Overrides common.VM_DSK_BUS for this node only"},
                    {"name": "VM_NET_MODEL", "required": False, "type": "string", "enum": ["virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci"], "description": "Overrides common.VM_NET_MODEL for this node only"},
                    {"name": "VM_MACHINE", "required": False, "type": "string", "enum": ["", "pc"],
                     "description": "Overrides common.VM_MACHINE for this node only — set per-node when only "
                     "one old guest in an otherwise-modern lab needs the legacy \"pc\" machine type"},
                    {"name": "VM_OSVARIANT", "required": False, "type": "string", "description": "Overrides common.VM_OSVARIANT for this node only"},
                    {"name": "VM_ROOT_PASS", "required": False, "type": "string", "description": "Overrides common.VM_ROOT_PASS for this node only (virt_customize only)"},
                    {"name": "ssh_pwauth", "required": False, "type": "string", "enum": ["true", "false"], "description": "Overrides common.ssh_pwauth for this node only"},
                    {"name": "BRIDGE", "required": False, "type": "string", "description": "Overrides common.BRIDGE for this node only"},
                    {"name": "open_ports", "required": False, "type": "array", "description": "Overrides common.open_ports for this node only"},
                    {"name": "aws_open_ports", "required": False, "type": "array", "description": "Overrides common.aws_open_ports for this node only"},
                    {"name": "SOURCE_SHA256", "required": False, "type": "string", "description": "Overrides common.SOURCE_SHA256 for this node — checksum value, URL, or local file path"},
                    {"name": "ISO_SHA256", "required": False, "type": "string", "hidden": True, "description": "DEPRECATED: use SOURCE_SHA256 instead"},
                    {"name": "config_method", "required": False, "type": "string",
                     "enum": [{"value": "", "label": "(default) Ignition+Combustion"}, "cloud-init", "virt_customize", "install_iso"],
                     "description": "Overrides common.config_method for this node only"},
                    {"name": "install_type", "required": False, "type": "string",
                     "enum": [{"value": "", "label": "(default) auto-detect from ISO_IMAGE name"}, "autoyast", "kickstart", "preseed", "autoinstall"],
                     "description": "Overrides common.install_type for this node only (config_method=install_iso only)"},
                    {"name": "network_renderer", "required": False, "type": "string", "enum": ["NetworkManager", "networkd", "sysconfig"],
                     "description": "Overrides common.network_renderer for this node only — set per-node when a lab mixes guest OSes (e.g. SLE Micro nodes on NetworkManager, an Ubuntu/Debian node on networkd, an Alibaba Cloud Linux node on sysconfig)"},
                    {"name": "SOURCE_IMAGE", "required": False, "type": "string", "description": "Overrides common.SOURCE_IMAGE for this node only — local filename, URL, or file path"},
                    {"name": "ISO_IMAGE", "required": False, "type": "string", "hidden": True, "description": "DEPRECATED: use SOURCE_IMAGE instead"},
                    {"name": "kvm_host", "required": False, "type": "string", "description": "Pin this VM to a specific KVM host from KVM_HOSTS; omit to auto-select by resource availability"},
                    {"name": "existing", "required": False, "type": "boolean", "description": "Pre-provisioned host (baremetal or already-running VM) — the tool will not create or destroy it, only SSH to it; omit for today's default (a VM this tool creates)"},
                    {"name": "backend", "required": False, "type": "string", "enum": ["libvirt", "harvester", "hetzner", "aws", "gcp", "alibaba", "scaleway", "upcloud", "ovhcloud", "exoscale"], "description": "Compute backend for this node; omit to use common.backend or the configured default"},
                    {"name": "cloud_account", "required": False, "type": "string", "description": "Overrides common.cloud_account for this node only — name of a cloud account file under /etc/lab_creation/credentials/. Its cloudtype sets this node's backend; its keys layer over lab_creation.cfg for this node only."},
                    {"name": "cloud_instance_type", "required": False, "type": "string", "description": "Overrides common.cloud_instance_type for this node only — cloud backends only, used verbatim instead of auto-picking from vm_cpu/vm_mem"},
                    {"name": "pxe_kernel", "required": False, "type": "string", "description": "PXE boot kernel path for this node (only used when the \"pxe\" service is enabled via common.services); omit to skip generating a PXE boot entry for this node"},
                    {"name": "pxe_initrd", "required": False, "type": "string", "description": "PXE boot initrd path for this node (pxe service only)"},
                    {"name": "pxe_append", "required": False, "type": "string", "description": "PXE boot kernel command-line arguments for this node (pxe service only)"},
                    {"name": "forwarded_ports", "required": False, "type": "array",
                     "description": "Ports to DNAT-forward from the KVM host's real IP to this node's own myip (only used when the \"portforward\" service is enabled via common.services — meant for nodes on a NAT'd network, see lab.cfg's _network_mode). Each entry is \"<external>:<internal>/<protocol>\", e.g. \"8080:80/TCP\"."},
                ],
            },
            "kclusters": {
                "title": "Kubernetes Clusters",
                "description": "Map of Kubernetes cluster name to its configuration",
                "repeatable": True,
                "key_label": "Kubernetes cluster name",
                "fields": [
                    {"name": "clu_type", "required": True, "type": "string", "enum": ["rke2", "k3s", "kubeadm", "harvester"], "description": "Kubernetes cluster type: rke2, k3s, kubeadm (upstream Kubernetes built with kubeadm, configured by clu_cni and clu_pod_cidr) or harvester (Harvester / SUSE Virtualization appliance nodes on AWS from an AMI)"},
                    {"name": "clu_rel", "required": True, "type": "string", "description": "Release channel: stable, latest, or a version string"},
                    {"name": "clu_cni", "required": False, "type": "string", "enum": ["flannel", "calico", "none"], "description": "clu_type kubeadm: network plugin the first server applies (default: flannel; none applies nothing, for a lab that brings its own)"},
                    {"name": "clu_pod_cidr", "required": False, "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$", "description": "clu_type kubeadm: pod network (default: the network the chosen clu_cni's own manifest expects, 10.244.0.0/16 for flannel and 192.168.0.0/16 for calico)"},
                    {"name": "mydomain", "required": True, "type": "string", "description": "DNS domain for this Kubernetes cluster (e.g. mydemo.lab)"},
                    {"name": "addons", "required": False, "type": "array", "description": "List of Kubernetes cluster add-on names (install_<addon> scripts)"},
                    {"name": "mgm_node", "required": False, "type": "string", "description": "Hostname of the node that runs addon installers (default: first server node)"},
                    {"name": "clu_token", "required": False, "type": "password", "pattern": r"^[A-Za-z0-9._-]{16,}$", "description": "rke2/k3s: join token set on the first server, at least 16 letters, digits, '.', '_' or '-' (default: generated by the first server). Set it when machines outside this lab definition join the cluster, e.g. from a kickstart profile or a Salt state"},
                    {"name": "harvester_token", "required": False, "type": "password", "description": "harvester: cluster token shared by every node (required for clu_type harvester)"},
                    {"name": "harvester_vip", "required": False, "type": "string", "pattern": r"^(\d{1,3}\.){3}\d{1,3}$", "description": "harvester: cluster VIP, a free private IP of the subnet (default: picked from AWS_SUBNET_ID)"},
                    {"name": "harvester_mtu", "required": False, "type": "integer", "default": 1500, "description": "harvester: management network MTU, the same on every node"},
                    {"name": "harvester_replica_count", "required": False, "type": "integer", "min": 1, "max": 3, "description": "harvester: Longhorn replica count of the default StorageClass (set 1 below 3 nodes)"},
                    {"name": "harvester_device", "required": False, "type": "string", "default": "/dev/nvme0n1p6", "description": "harvester: install device in the AMI"},
                    {"name": "harvester_ntp_servers", "required": False, "type": "array", "description": "harvester: NTP servers (default: 169.254.169.123, the AWS time service)"},
                    {"name": "harvester_config", "required": False, "type": "object", "hidden": True, "description": "harvester: mapping merged last into every node's Harvester configuration"},
                    {"name": "harvester_settings", "required": False, "type": "object", "hidden": True, "description": "harvester: Harvester Settings (name: value) applied once the cluster is up"},
                ],
            },
            "pxe": {
                "description": "Optional TFTP/PXE-boot/DHCP service config, used only when \"pxe\" is listed in common.services. Runs as one small podman container on the automation VM.",
                "fields": [
                    {"name": "pxe_bridge", "required": False, "type": "string", "default": "br0", "description": "Bridge interface the PXE service binds to"},
                    {"name": "pxe_dhcp_mode", "required": False, "type": "string", "default": "off",
                     "enum": [
                         {"value": "off", "label": "(default) off — no DHCP; an external DHCP server must already supply PXE boot options itself"},
                         {"value": "proxy", "label": "proxy — PXE proxyDHCP; leases still come from an external DHCP server, this only answers the PXE-specific options"},
                         {"value": "full", "label": "full — this service is the DHCP server"},
                     ],
                     "description": "DHCP role"},
                    {"name": "pxe_dhcp_range_start", "required": False, "type": "string", "description": "DHCP range start — required when pxe_dhcp_mode is \"full\""},
                    {"name": "pxe_dhcp_range_end", "required": False, "type": "string", "description": "DHCP range end — required when pxe_dhcp_mode is \"full\""},
                    {"name": "pxe_dhcp_lease", "required": False, "type": "string", "default": "12h", "description": "DHCP lease time — \"full\" mode only"},
                    {"name": "pxe_boot_filename", "required": False, "type": "string", "default": "lpxelinux.0", "description": "PXE boot filename handed out to clients (override for UEFI/iPXE chainloading)"},
                ],
            },
        },
    }
