# DNS and multi-site architecture

How a lab's names resolve, which server holds them, and why it is built this way. Read this before changing
anything in `libs/dns_zone.py`, `libs/services.py`'s `DNSService`, `libs/overlay.py`'s registries, or
`backends.ensure_cloud_dns_vm()`. Several of the decisions below look like oversights until you know the
constraint behind them.

## Two words that mean different things here

**Site** is a network: one cloud account's VPC, one KVM host's network, anywhere a set of machines can reach each
other directly. `overlay.site_name_for(backend, account)` names it, `<backend>-<account>` for a cloud account and
`home` for libvirt or Harvester. A site is the unit of reachability, and it is per **account** because cloud
accounts cannot talk to each other.

**Zone** is a DNS zone: a domain a server is authoritative for, `mydemo.lab`. `ensure_zone()`, `zone_file`,
`_compare_zone` and `validate_zones` all mean this. A zone is the unit of naming, and it is per **lab**.

Sites and zones are independent. One server serves several zones; one zone lives on several servers. Conflating
them is the single most common way to misread this code.

**Control plane** is the provider's API, reached with the account's credentials: create an instance, start it, read
its address. Available from anywhere the credentials are, which is why the automation node can deploy and inspect
a cloud lab it has no network route into.

**Data plane** is a route: SSH into a node, SSH into a remote DNS master to write its zone file. This is the one
that can be missing, and most of the design below is about that.

## The two kinds of DNS server

They are different artifacts and must not be described as one.

| | Automation node | Cloud DNS VM |
|---|---|---|
| What it is | BIND, an HTTP provisioning server, the scripts, the credential store | BIND and nothing else |
| Built by | `setup_demo_server/setup_automation.py`, or `scripts/setup_automation_node.py` in a cloud | `backends.ensure_cloud_dns_vm()`, on demand |
| Named | whatever the operator called it | `lab-dns-<backend>[-<account>]` (`cloud_dns_vm_name()`) |
| Scope | runs every command; one per deployment | one per cloud account, **shared by every lab there** |
| Reused | n/a | yes, deliberately: an existing one is found and kept |

A lab that spans sites does **not** get a second automation node per site. It gets one automation node writing to
N BIND masters over SSH. The plurality is in the masters. A second automation node is possible (a pure-cloud lab
builds one, with `ensure_cloud_dns=False` so it serves its own DNS rather than getting a DNS VM beside it), but it
is not the answer to "this lab spans two networks".

## Who resolves what, from where

- **A node inside a site** asks that site's DNS server, at its **private** address, and is answered from the
  **site view**, where that site's own nodes are at their private addresses. A query to the server's public
  address would leave through the internet gateway, arrive from the node's own public address, and be refused
  recursion (`backends.dns_recursion_networks()`).
- **A node in another site** is answered from that other site's own view, over the overlay, also at private
  addresses.
- **Anyone else**, the operator included, is answered from the **external view**, where every node is at its
  public address.

So private addresses are the normal answer and they never move. The external view is the only place a moving
address appears, and it exists for clients outside every site.

## The two views

`dns_zone.view_named_conf()` writes the whole `named.conf` of a view master, because BIND refuses a zone outside a
view once any view exists.

- `view "site"`, matched on that site's client addresses: the zones of `site_named_conf()`, served from
  `site_file(mydomain, site)`. Recursion is allowed from `localnets` and the site's own networks.
- `view "external"`, `match-clients { any; }`: the zones of `remote_named_conf()`, served from
  `forward_file(mydomain)`, with `recursion no`, `allow-recursion { none; }` and `allow-query-cache { none; }`.

This is what makes "isolate the network, keep DNS reachable" safe: the public-facing view is authoritative-only and
never a resolver anyone can abuse.

## Every master is an independent primary, and there is no zone transfer

`allow-transfer { none; }` in `dns_zone.stanza()`. Records arrive by SSH-editing each master's own zone file
(`DNSService.add_to_dns()` and friends). No secondary ever pulls anything.

**Why, and it is not inertia:** a zone transfer is a pull. The secondary opens TCP 53 to the primary; `NOTIFY` is
only a hint in the other direction. Native replication would therefore require the cloud DNS VM to reach the
automation node *inbound*, and the whole reason the cloud DNS VM exists is that the automation node is usually
behind NAT and cannot be reached that way. Flipping it, so a cloud server is primary and the automation node pulls,
fixes the direction but gives a lab spanning three sites three primaries for one zone, which DNS does not allow.

Consequences to keep in mind:

- A write that misses a master leaves it behind. `_compare_zone()` diffs record **sets** because of this.
- Nobody can point a stock BIND secondary at these servers.
- `dns_sync.py` and `overlay_ctl.py dns-sync` are a hand-rolled AXFR: `sync_zone()` only ever **adds**, and
  `prune_zone()` is a separate, deliberate step.
- The SOA serial is real and moves (see below), which is what tells "behind" from "diverged".

## The serial

`dns_zone.serial()` / `with_serial()` / `next_serial()`, and `DNSService.bump_serials()`. Every write path ends in
`DNSService._finish_write()`, which raises the serial of every file it touched on every master it reached and then
reloads named. A master `masters()` skipped because it did not answer keeps its lower serial.

That gives the one thing a record diff cannot: a record held by a master **whose serial is behind** was deleted
from the others while it was away, so pruning it is the repair; a record held by a master **level with the others**
is one they have not been written yet, and pruning it would destroy it. `_difference_messages()` says which.

Two SOA layouts exist and both must keep working: the one-line form `forward_header()` writes, and the multi-line
form `setup_demo_server/automation-node-lib.sh` writes, whose serial is on the line after the SOA.

## What one write touches

A name resolving correctly depends on four separate copies of the same truth. `add_to_dns()` handles all of them,
which is why reconciliation goes through it rather than editing a zone directly:

1. The A record in the external view's file and in **every** site view's file, on this host and on every reachable
   master, with the node's old record replaced rather than a second one appended.
2. The PTR record, one canonical one per address.
3. Every **service** record that pointed at the address being replaced (`_dns_retarget_services()`), so an
   add-on's ingress hostname follows the node.
4. `LAB_HOSTS_FILE` (`/etc/hosts`), for an automation node that runs no BIND.

A fifth lives outside `add_to_dns()`: the **guest's own resolver**, which DHCP can reset on reboot. That is
`use_lab_dns()`, called from `refresh_kept_node_dns()`, and it is why `reconcile_lab.py --all` exists.

## The registries

Three plain text files on the WireGuard hub, under `/etc/wireguard/`, rewritten whole on each update:

| File | Holds | Key |
|---|---|---|
| `overlay_sites` | which site owns which subnet | site |
| `overlay_nodes` | which node sits behind which site gateway | node |
| `overlay_dns` | `site  address  domain  private-address` | **(site, domain)** |

And one on the automation node, which is not part of the overlay:

| Path | Holds |
|---|---|
| `/etc/lab_creation/labs/<lab>.json` | the labs this node has deployed (`libs/lab_registry.py`) |
| `/var/log/lab-in-a-box/labs.log` | one line per lab action, with the lab name and the FQDNs |

The lab registry exists because nothing else knows which labs there are: every command takes a definition file as
an argument. `setup_lab.py` writes an entry before it builds anything, `destroy_lab.py` removes it, and
`lab_watch.py` is the only reader.

## Addresses that move, and the three things that handle it

A provider that releases an instance's auto-assigned **public** address on stop hands back a different one on
start. The **private** address is bound to the interface for the instance's lifetime on AWS, GCP and Alibaba, and
the guest never sees the public one at all (it is 1:1 NAT at the gateway), so there is no DHCP lease to key off.

1. **Reserve it.** `static_ip: "true"` in `common` or on a node: `VMBackend.ensure_stable_address()` is an Elastic
   IP on AWS, the assigned address promoted in place on GCP, an EIP on Alibaba, a flexible IP on Scaleway, and a
   documented no-op on Hetzner, UpCloud, OVHcloud and Exoscale. `delete_vm()` releases it. Shared infrastructure
   (the cloud DNS VM, each site's overlay gateway) reserves unconditionally through
   `backends.ensure_infra_address()`: it belongs to no single lab, so an address that moves there breaks every lab
   on that account at once.
2. **Repair it.** `libs/reconcile.py` compares each cloud node's A record with the address its provider reports and
   re-registers the ones that disagree, through `refresh_kept_node_dns()` so all four copies above are fixed.
   `reconcile_lab.py` is the explicit command; `check_lab.py` and `vm_power.py start` do it in passing.
3. **Do not let it be cached.** A zone holding a cloud node is capped at `dns_zone.CLOUD_TTL` (300s) by
   `ensure_zone(zone_ttl=...)`, which only ever lowers. Without that, a repaired record would be ignored by every
   resolver that already asked, for up to `DEFAULT_TTL` (a day).

What reconciliation **cannot** fix is a WireGuard spoke's `Endpoint` after a gateway's address moves: nothing
records which endpoint the spokes were given, and a gateway whose tunnel is down cannot be reached through that
tunnel. That is why gateways reserve their addresses rather than relying on repair.

## The scheduled check

`scripts/lab_watch.py`, on a systemd timer (`lab-watch.timer`, every 15 minutes, **enabled by default**) with a
`cron.d` fallback. Per lab in the registry it asks each node's provider what is there now:

- **gone** — deleted outside this tooling: the node comes off the entry, the event log records it as `vanished`,
  and a lab with no nodes left is forgotten.
- **stopped** — reported and left alone. Starting a VM is `vm_power.py`'s job and a deliberate one.
- **running and moved** — registered again at the address the provider reports now.

A provider that cannot be asked reports `unknown`, never `gone`: an expired credential must never be read as
"somebody deleted this lab".

## Why `overlay_dns` is keyed on (site, domain)

One line per **pair**, not per site. A cloud account's DNS VM is shared by every lab on that account and holds one
zone per lab, so two labs there state two true things about one server. Keyed on the site alone, the second lab to
run replaced the first's line, and the first lab's next run then found its own site registered for another domain,
left that server out through the domain filter in `list_dns_servers()`, and silently stopped writing its records to
the very server already holding its zone. It cost a **multi-site** lab its cross-site resolution; a single-site lab
was unaffected, because its own site is excluded from that lookup anyway.

One line per pair also means two labs never write the same line, which is why the alternative shape (one line per
site carrying a list of domains, storing the address once) was rejected: a lab's teardown would have to edit a
field inside a line another lab owns, which is the same failure one level down.

Consequences to preserve when touching either function:

- `register_dns_server()` replaces only the line for (this site, this domain), and additionally drops this site's
  own line from before the domain field existed, which cannot be told apart from it and would otherwise be read as
  every other lab's server for ever.
- `list_dns_servers()` returns **one entry per site**: the line naming `mydomain`, else a line naming no domain.
- `forget_dns_server()` removes exactly one lab's line, called by `destroy_lab.py`. `forget_site()` still removes
  every line of a site being retired, which is a different thing: there the server itself is going away.

## The address a record is written through comes from the zone, not the registry

`_preferred_dns_address()` resolves a site's DNS server out of **this host's own copy of the zone** and falls back
to the addresses the registry recorded. No field was added to the file for it: a site name already determines the
server's name, so `dns_vm_name_for_site()` is the inverse of `site_name_for()` composed with
`cloud_dns_vm_name()`, and `_register_dns_vm_record()` put that name in the zone.

So the private address comes from `site_file(mydomain, site)` and the public one from `forward_file(mydomain)`,
both local file reads with no query to anyone and no cloud credential, and a server whose address changed is still
reached without anything re-registering it. The recorded addresses stay as the fallback for the first run, before
that record exists, and for a reader holding no copy of the zone: a lookup that cannot fail today must not start
failing. The `home` site has no such VM, its DNS server being the automation node, so it keeps its recorded
address, which is `mydns` and does not move.

The lesson generalises. What this file holds that is **derivable** should be derived: the server's name from the
site, its address from the zone. What it holds that is genuinely **state** has to stay recorded: which sites exist
at all, and which zones each serves.

## The shared VMs' lifecycle

`ensure_cloud_dns_vm()` has always created the DNS VM when absent, reused it when present and started it when
stopped, so "one server per account, shared by every lab there" needs nothing further on the way up.

On the way down, `destroy_lab.py --with-infra` destroys a site's DNS VM and its overlay gateway, behind three
gates that each report and keep rather than destroy:

1. **The site's DNS server is this automation node itself** (`ipalloc.local_ips()`), so there is no VM of its own
   to destroy. That is every libvirt and Harvester site, and a cloud lab whose automation node serves its own DNS.
2. **Another recorded lab's nodes sit on the same site** (`libs/lab_registry.py`, reading each other lab's
   definition rather than a recorded copy of its sites).
3. **The DNS server still declares a zone that is not this lab's** (`DNSService.declared_zones()`). This is the
   only gate that can see a lab this automation node never deployed, because the server is per cloud **account**
   while a registry is per automation **node**. The gateway has no equivalent and rests on gates 1 and 2.

Two things that are easy to get wrong here:

- Destroy only through `backend.delete_vm()`. It is the only caller of `release_stable_address()`, and these VMs
  reserve their addresses unconditionally, billed whether or not anything uses them, so any other path leaks one
  per account per provider.
- `hub_vm_name()` names a site's gateway **and** the hub itself, which differ only by the account they live in. A
  site that is the `OVERLAY_HUB_ACCOUNT`'s own keeps its gateway: the hub serves every account rather than this
  lab's site, and retiring it is `overlay_ctl.py`'s business.

Refcounting the registry alone would not do. It is per automation node, so two nodes sharing one cloud account
would each count only their own labs, and the first to reach zero would destroy a server the other's labs resolve
through. Gate 3 is what closes that, and it needs no new backend primitive.

## Known gaps

**A destroyed node keeps its line in the hub's node registry.** `overlay.forget_node()` exists but is only ever
called by hand from `overlay_ctl.py`; `destroy_vm.py` does not call it. So `refresh_routes()` keeps SSHing to
nodes that no longer exist, which is what its "unreachable" count is mostly made of. The fix is for `destroy_vm.py`
to call `forget_node()` with `overlay.existing_hub_host()`, cached per process so a lab teardown does not look the
hub up once per node.

## Considered and deferred: delegation plus native replication

Give each site its own subdomain, `aws.mydemo.lab`, served by that site's own server as primary for that subdomain
only, with an NS record in the parent. Everything then composes: no zone has two primaries, the automation node
pulls each subdomain **outbound** (the direction that works), nobody pushes records anywhere, and
`_compare_zone`/`sync_zone`/`prune_zone` and the entire divergence failure class are deleted. A master that was
down catches up by itself.

The cost is real: node FQDNs change shape, existing labs need migrating, and views are still needed inside each
subdomain. Recorded here so it is not rediscovered and half-implemented. The two decisions, delegation and native
replication, are one decision.

## Rules when changing any of this

- A zone is created in three places and must have the same shape in all of them: `libs/services.py`,
  `backends.ensure_cloud_dns_vm()` and `setup_demo_server/automation-node-lib.sh`. `libs/dns_zone.py` exists so
  that shape is defined once. A zone written in one shape and compared in another reads as drift that is never
  resolved.
- A write must reach every master or say that it did not. `masters()` drops one that does not answer and warns;
  `del_from_dns()` warns by name, because a removal that missed a master cannot be repaired later by `sync_zone()`,
  which only adds.
- A zone goes in the directory that master's own named may read: `/etc/bind` on Debian and Ubuntu, whose AppArmor
  profile denies anywhere else, `/var/lib/named` elsewhere (`dns_zone.zone_dir()`). A stanza always names the file
  by absolute path.
- `records()` deliberately ignores the header, the serial and record order, since those are per server. Anything
  added to a header must stay invisible to it.
- Never test a DNS change against a live server's zone: `dns_sync.py --prune` operates on the real zone files.
