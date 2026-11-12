# Lab definition JSON — field reference

A lab file is a single untracked JSON document, usually named `<lab-name>.json` in the
repo root or the automation VM's working directory. It is read by `setup_lab.sh`
(`scripts/setup_lab.py`) and by every `scripts/install_*` addon (each addon receives the
file path as `$1`).

## Top-level sections

```jsonc
{
  "nodes": { ... },       // required — every VM in the lab
  "common": { ... },      // required — shared defaults (network, image, sizing)
  "kclusters": { ... },   // one Kubernetes cluster per entry — preferred for new labs
  // "cluster": { ... }   // OLD single-cluster format, still supported, don't use for new labs
  "addons": [ ... ],      // only used with the OLD single-cluster format
  "<addon-name>": { ... } // one optional section per addon that needs config
}
```

### `nodes`

One entry per VM, keyed by its FQDN:

```jsonc
"node101.mydemo.lab": {
  "myip": "192.168.88.101",       // static IP (bare-metal/on-prem). Omit for most cloud nodes.
  "mymac": "34:8a:b1:4b:1a:c1",   // MAC (bare-metal only)
  "INSTALL_RKE2_TYPE": "server",  // "server" or "agent" — Kubernetes cluster role
  "kcluster": "cluster1",         // which kclusters entry this node belongs to
  "extra_dsk": "/dev/sdb,bus=scsi", // optional extra disk; ",bus=X" overrides the default bus
  "config_method": "cloud-init",  // see "Provisioning methods" below; empty = Ignition+Combustion
  "existing": true                // marks a node as already-provisioned; most fields optional then
}
```

### `common`

Shared defaults applied to every node unless overridden per-node: `mymask`, `mygw`,
`mydns`, `mynet_reverse`, `ISO_IMAGE`, `VM_MEM`, `VM_DSK`, `VM_CPU`, `VM_BOOT`
(`uefi`/`bios`/...), `VM_DSK_BUS`, `VM_NET_MODEL`. Read `libs/lab_creation.py`'s
`load_vm_vars` for the authoritative full list and current defaults — this file only
covers the fields you'll touch routinely.

### `kclusters` (preferred) vs. `cluster` (old format)

```jsonc
"kclusters": {
  "cluster1": {
    "clu_type": "rke2",      // "rke2" or "k3s"
    "clu_rel": "stable",
    "mydomain": "mydemo.lab",
    "addons": ["rancher", "longhorn"],
    "clu_storage": "longhorn" // optional: which StorageClass addon to require
  }
}
```

Use `kclusters` for anything new — it's the only form that supports more than one
cluster in a single lab. The old flat format uses a single top-level `"cluster": {...}`
plus a top-level `"addons": [...]` list instead; only use it when extending an existing
lab file that's already in that format.

### Addon entries and overrides

Each entry in an `addons` list is either a plain string (`"rancher"`) or a single-key
object overriding that addon's config for this context:
`{"client_registration": {"client_registration_activation_key": "1-sles"}}`. The shared
parsing lives in `libs/apps.py` (`addon_entry_name()`, `addon_entry_overrides()`) — any
new code that reads an `addons[]` list must go through those, not raw string/dict
assumptions, because a dict entry breaks plain `in`/`set()`/`sorted()` usage on the list.

### Per-addon config sections

An addon that needs configuration gets its own top-level section named exactly after the
addon, e.g.:

```jsonc
"rancher": {
  "cert_manager_ver": "--version v1.14.4",
  "rancher_rel": "rancher-stable",
  "rancher_repo_url": "https://releases.rancher.com/server-charts/stable",
  "rancher_shorthn": "rancher"
}
```

**The field names for any given addon are documented in that addon's own script header**
(`scripts/install_<name>`'s leading comment block), not in a central schema file — open
the script before guessing a field name.

## Provisioning methods (`config_method` per node)

- **(empty/default)** — Ignition + Combustion, for SLE Micro. Templates:
  `templates/combustion.template`, `templates/ignition.template`.
- `cloud-init` — for Ubuntu/Leap. Templates: `templates/cloud-init.template_*`.
- `virt_customize` — modifies the QCOW2 directly on the hypervisor; use for CentOS 7, old
  Debian/RHEL, or any image with no Ignition/cloud-init support. Autodetects the guest
  network stack (netplan → nm-keyfile → wicked → network-scripts → ifupdown →
  systemd-networkd). Set `VM_ROOT_PASS` in `common` for the root password, or omit to
  reuse `ROOT_PWD_HASH` from `lab_creation.cfg`.

## Worked example (single-cluster, old format — adapt to `kclusters` for new labs)

```json
{
  "nodes": {
    "node51.mydemo.lab": {"myip": "192.168.8.51", "mymac": "34:8a:b1:4b:16:b1", "INSTALL_RKE2_TYPE": "server"},
    "node52.mydemo.lab": {"myip": "192.168.8.52", "mymac": "34:8a:b1:4b:16:b2", "INSTALL_RKE2_TYPE": "agent"}
  },
  "common": {
    "mymask": "24", "mygw": "192.168.8.1", "mydns": "192.168.8.71", "mynet_reverse": "8.168.192",
    "ISO_IMAGE": "SLE-Micro.x86_64-5.5.0-Default-qcow-GM.qcow2",
    "VM_MEM": "8192", "VM_DSK": "40", "VM_CPU": "4"
  },
  "cluster": {"clu_name": "cluster2", "clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab"},
  "addons": ["rancher", "longhorn", "neuvector"],
  "rancher": {
    "cert_manager_ver": "--version v1.14.4",
    "rancher_rel": "rancher-stable",
    "rancher_repo_url": "https://releases.rancher.com/server-charts/stable",
    "rancher_helm_chart": "rancher-stable/rancher",
    "rancher_shorthn": "rancher"
  }
}
```

For a multi-cluster or cloud-backend lab, look for a closer `examples/*.json.template` or
an existing lab JSON in the repo matching the target shape and adapt it — don't build a
cloud or multi-cluster lab from this single-cluster example alone.

## Cloud-backend specifics

Cloud nodes (AWS, etc.) usually have no `myip`/`mymac` — the backend assigns the IP at
create time. DNS registration for cloud nodes needs a reachable DNS target inside the
same VPC (`backends.ensure_cloud_dns_vm()`), not the on-prem BIND server, since cloud
nodes often can't reach it directly. An `aws_open_ports` field (or the equivalent for
another backend) controls what's exposed through the security group — opening a port to
the world is a live, user-facing action; don't do it without the user's go-ahead (see
`conventions.md`).
