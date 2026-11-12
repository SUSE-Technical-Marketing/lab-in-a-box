# Writing a new `scripts/install_<name>` addon

Every addon script is structurally the same file, whether it's a 400-line SMLM installer
or a 20-line Helm chart wrapper. Below is a real, trimmed skeleton — lifted directly from
`scripts/install_ollama` (a clean, modern, small Kubernetes/Helm addon) — annotated with
what each part is for. Copy the *closest existing addon*, not this skeleton verbatim, but
use this to understand what every part does and why it's there.

```python
#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install <Thing> <what it does, one line>
# Author/s: <you / the requester>
# License: GPLv3
#
# Schema version: 1.0   (mandatory to maintain — see references/schema-versions.md)
#
# JSON section: "<name>" — <one-line purpose>
#   <name>_version : [OPTIONAL] ...
#   <name>_shorthn  : [OPTIONAL] hostname prefix (default: "<name>")
#   ...                          <-- this header comment IS the field documentation;
#                                    nothing else documents these fields centrally.

__version__ = "<short hash or free-form version string>"

PLUGIN = {
    "name": "<name>",
    "targets": ["container"],       # or omit/adjust for non-Kubernetes addons
    "layers": ["kubernetes"],
    "requires_kubernetes": None,
    "requires_cluster": ["ingress"],   # add "storageclass" etc. if the addon needs it —
                                        # a real bug this project hit: forgetting
                                        # "storageclass" here means cluster_plan() never
                                        # provisions storage and pods hang Pending with
                                        # no obvious error
    "aux_services": [],
}

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac   # noqa: E402
import primary               # noqa: E402
import k8s                   # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run, die   # noqa: E402


def _validate(v):
    """Called by ac.handle_common_args before main logic runs. Validate the
    "<name>" section: required fields, credential resolution, URL/version/ns sanity.
    See addon_common.Validator (v.vreq_or_credential/v.vns/v.vver/v.vurl/...) for the
    available helpers — reuse them instead of hand-rolling checks."""
    cfg = v.definition.get("<name>", {}) or {}
    v.vns("<name>")
    v.vver("<name>")


def setup_<name>(hostname, clu_name, mydomain, **cfg_fields):
    """Do the actual install over SSH (helm upgrade -i ..., or ssh_run() for
    non-Kubernetes work). Return the FQDN (or whatever downstream needs) so main()
    can register DNS / print it."""
    fqdn = "{}.{}.{}".format(cfg_fields.get("shorthn") or "<name>", clu_name, mydomain)
    ssh_run(hostname, "helm upgrade -i <name> ... --set ingress.hosts[0].host={} ...".format(fqdn))
    return fqdn


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab-file>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    lab_file = sys.argv[1]
    definition = primary.load_definition(lab_file)

    target = k8s.first_server_node(definition)   # or whichever node this addon targets
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("<name>", {}) or {}

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = setup_<name>(vm_name, clu_name, clu_cfg.get("mydomain", ""), shorthn=cfg.get("<name>_shorthn"))
    ac.register_service_dns(definition, clu_name, fqdn)


if __name__ == "__main__":
    main()
```

## Checklist, in the order you'll actually hit each decision

1. **What does it receive as input?** Always `sys.argv[1]` = the lab JSON path, loaded
   via `primary.load_definition(lab_file)`. Never a second positional argument for "which
   VM" — the addon figures its own target out (`k8s.first_server_node`,
   `k8s.get_vm_kcluster`, or whatever node-selection helper fits).
2. **Container/Kubernetes or SSH-only or database-backed?**
   - Kubernetes/Helm: `setup_helm()` then `ssh_run(... "helm upgrade -i ...")`. Always
     `upgrade -i` (idempotent), never a bare `helm install`.
   - Plain SSH (installing a package, writing a config file, running a binary directly
     on the VM): skip `setup_helm`/`PLUGIN["targets"]`/`"layers"` entirely and just
     `ssh_run()`/`ssh_output()` from `lab_creation`.
   - Needs its own database: `from db_common import setup_mariadb_os` (or
     `setup_postgresql_os`) — never write a raw `CREATE DATABASE`/container setup inline.
3. **Validate inputs up front, in `_validate(v)`.** Use `addon_common.Validator`'s own
   helpers (`v.vreq_or_credential`, `v.vns`, `v.vver`, `v.vurl`, ...) rather than ad-hoc
   `if not cfg.get(...)` checks — they produce consistently-formatted errors and are
   already wired into `ac.handle_common_args`.
4. **Credentials**: if the addon needs a username/token (an Application Collection
   account, an API key, ...), support both inline fields (`<name>_user`/`<name>_password`)
   *and* a named credential-file account (`<name>_account`) via
   `ac.resolve_credential(...)` — look at `install_ollama`'s `appco_common` usage for the
   exact pattern if the credential is an Application Collection account specifically.
5. **DNS**: once the service has an FQDN, call `ac.register_service_dns(definition,
   clu_name, fqdn)` — don't construct DNS records by hand; that function already knows
   about the on-prem-vs-cloud-DNS-VM distinction (see `conventions.md`).
6. **Forever-retry**: if any step talks to SMLM/Uyuni and depends on server-side async
   state converging, wrap it per `conventions.md`'s SMLM section — do this while writing
   the addon, not as an afterthought once a live run hangs.
7. **`PLUGIN["requires_cluster"]`**: list every cluster capability the addon actually
   needs (`"ingress"`, `"storageclass"`, ...) so `cluster_plan()` provisions it
   automatically. Missing one here is a known failure mode (pods stuck Pending, no clear
   error) — double check against what the install step actually touches, don't just copy
   another addon's list unexamined.
8. **Templates**: only add `templates/addons/<name>/` if the addon needs rendered config
   files pushed to a node (Ansible playbooks, Helm values files, systemd units). A pure
   `helm upgrade --set ...` addon usually needs none.
9. **Tests**: see the Testing section in SKILL.md's Part B — mock the SSH/API boundary,
   never touch a real host, model the test file on an existing one with a similar shape
   rather than inventing a new test structure.
10. **Schema version — mandatory, not just the header comment**: a new addon starts at
    `1.0`; any later change to its fields bumps this. Read
    `references/schema-versions.md` for the full workflow — bumping the `# Schema
    version:` line alone is not enough, `scripts/schema_versions.py record` has to run too,
    and CI checks this on a clean tree even when your own local pre-commit run happened to
    pass with an untracked file still sitting in your working tree.
11. **Docs**: add a row to the README's addon table (and any other doc listing addons) in
    the same change.
