---
name: lab-builder
description: Build and extend lab-in-a-box labs — write a new lab definition JSON (nodes/common/kclusters/addons) to stand up a KVM/cloud lab, or add a brand-new feature to the project itself (a new scripts/install_<name> addon, a templates/addons/<name>/ directory, its tests/checks/ coverage). Use this whenever the user asks to create, design, or extend a lab-in-a-box lab file, add an addon/integration/service to lab-in-a-box, or write a new install_<something> script — even if they just describe the service they want ("add Grafana to the lab", "I want a lab with three RKE2 nodes and Rancher", "write an installer for X"). This skill encodes this repo's hard operational rules (container-only tests, install-script-only deploys, forever-retry for SMLM, no unrequested commits) so a session without prior context on this codebase doesn't violate them.
---

# lab-in-a-box: build labs, build features

`lab-in-a-box` deploys nested KVM/cloud lab environments from a JSON definition. This
skill covers the two things people ask for most: **(A) writing a new lab definition**
and **(B) adding a new feature** (almost always a new addon) **to the toolkit itself**.
Both are mechanical if you follow the existing patterns — the project has dozens of
`scripts/install_*` files that are all shaped the same way, and departing from that shape
is the single biggest source of avoidable bugs and review friction.

Read `references/conventions.md` once per session before touching anything — it holds
the non-negotiable operational rules (how to test, how to deploy, when it's safe to
commit, the forever-retry requirement for SMLM). They're kept there instead of inline
here because they apply to *both* halves of this skill and to almost anything else done
in this repo; violating them is the most common way this kind of task goes wrong.

If the task touches any schema's fields — a new addon's own JSON section (Part B), the lab
definition itself, or a config file's schema — also read `references/schema-versions.md`
before editing. It's a separate file because it's mandatory and CI-enforced (unlike most of
this skill, which is about doing things well; this one is about not shipping something CI
will reject), and because it applies to edits to *existing* schemas just as much as new ones,
not only to Part B's "add a new addon" path.

## Part A — Writing a new lab definition

A lab is one JSON file (commonly named `<something>-lab.json`, kept untracked — it's a
runtime artifact, not source code, so never commit one unless the user explicitly asks).
Read `references/lab-json-schema.md` for the full field reference and a worked example.
Short version of the shape:

```jsonc
{
  "nodes": { "<fqdn>": { "myip": "...", "mymac": "...", "kcluster": "...", ... } },
  "common": { "mymask": "24", "mygw": "...", "mydns": "...", "ISO_IMAGE": "...", "VM_MEM": "...", ... },
  "kclusters": { "<name>": { "clu_type": "rke2", "mydomain": "mydemo.lab", "addons": ["rancher", "longhorn"] } },
  "rancher": { ... }   // one top-level section per addon that needs config, keyed by addon name
}
```

Steps:

1. **Clarify the topology with the user** if it's not already obvious: how many nodes,
   single cluster or several `kclusters`, which addons, which backend (bare KVM vs. a
   cloud backend like AWS — ask if unstated, since cloud nodes behave differently for
   DNS/IP/security groups; see `references/conventions.md`).
2. **Look at `examples/*.json.template`** and any lab JSON already in the repo root for a
   close topology match — copy the closest one as a starting point rather than building
   from nothing.
3. **One `kclusters` entry per Kubernetes cluster**, even if there's only one — the old
   flat `"cluster": {...}` format still works but new labs should use `kclusters` since
   it's the only form that supports more than one cluster.
4. **Addons go in the `kclusters.<name>.addons` list** (or the old top-level `addons` for
   the single-cluster format) as plain strings, or `{"<addon>": {...overrides...}}` to
   override that addon's config for specific context — check `apps.addon_entry_name()` /
   `addon_entry_overrides()` in `libs/apps.py` before assuming the override shape; don't
   guess it.
5. **Give every addon that needs config its own top-level JSON section**, named exactly
   after the addon (`"rancher": {...}`, `"longhorn": {...}`) — read the addon's own
   script header comment (`scripts/install_<name>`) for its field names; they're
   documented there, not in a central schema doc.
6. **Run `scripts/check_lab.py` (or whatever this version's lint/validate entrypoint is
   named — check `scripts/` for a `check_*` script matching "lab") against the new file**
   before handing it back, and fix anything it flags.
7. Tell the user the file is ready and how to run it (`setup_lab.sh <file>.json`) — don't
   run it yourself unless they ask; standing up a lab is a real, resource-consuming,
   potentially billable (cloud backends) action.

## Part B — Adding a new feature (usually a new addon)

Almost every "add X to the lab" request is a new `scripts/install_<name>` addon script.
Read `references/addon-pattern.md` before writing one — it has the real skeleton (copied
from a live addon in this repo) plus the checklist below in full detail. Summary:

1. **Pick the shape**: does it run as a container/Kubernetes workload (`"targets":
   ["container"]`), straight on the VM over SSH, or does it need a database companion
   (delegate to `libs/db_common.py`'s `setup_mariadb_os()`/`setup_postgresql_os()` —
   never hand-roll a database container; that's an explicit project convention, not a
   suggestion)?
2. **Copy the closest existing addon** as your starting skeleton — `scripts/install_ollama`
   is a clean, modern, small example for a Helm/Kubernetes addon; grep `scripts/` for one
   closer to what you're building (SSH-only addons, Ansible-addon style, etc.) if the
   target isn't Kubernetes-shaped.
3. **If the new feature needs logic that a second addon might also need** (anything
   beyond this one script's own glue), put it in `libs/`, not inside the script — this is
   the same rule that produced `libs/db_common.py` out of what used to be duplicated
   MariaDB-install code in two different addons.
4. **If any step depends on SMLM/Uyuni server state** (channel sync, activation keys,
   client registration, anything that can be mid-async on the server), it MUST use the
   forever-retry pattern (`launch_forever_retry`/`run_forever_retry_worker` in
   `libs/spacecmd_common.py`) — a bounded retry or timeout on these is a known, previously
   live-reported bug class in this project, not a style preference.
5. **Add template files under `templates/addons/<name>/`** only if the addon needs
   rendered config (Ansible playbooks, Helm values, systemd units, etc.) — plain
   SSH-command addons often need none.
6. **Write tests** in `tests/checks/` mirroring an existing addon's test file (e.g.
   `tests/checks/48_client_registration_test.py` is a good structural template: mock the
   SSH/API boundary, assert on what commands/config would have been sent, never touch a
   real host). Number the new file to slot in near related tests; a matching `.sh` wrapper
   may be needed too (check how existing `NN_name.sh` + `NN_name_test.py` pairs call into
   each other).
7. **Document it**: add the new addon to the README's addon table and any other doc that
   lists addons, in the same change — not as a follow-up. This is a standing project rule
   because addon docs have drifted out of sync before.
8. **Verify, deploy, commit — in that order, and only as far as asked**:
   - Verify: `./tests/run_tests.sh` (the whole suite, in containers — see
     `references/conventions.md` for why there is no other sanctioned way to run a test).
   - Deploy (only if asked to make it live): `LAB_DEPLOY_UNTRACKED=1 bash
     install_automation_node_scripts.sh` — never copy files into
     `/usr/local/lib/lab_creation/` or `/usr/local/bin/` by hand.
   - Commit/push: only when the user explicitly says to. Stage the new addon's own files
     by name, never `git add -A` (runtime JSON/log artifacts sitting in the repo root are
     common and must not get swept into a commit by accident).

## When the request is ambiguous

If the user's ask could mean either "design me a lab" or "build a feature so the lab can
do X", ask — the two paths produce different deliverables (a JSON file vs. new
scripts/templates/tests). If they mention a cloud backend, an SMLM/Uyuni dependency, or
something destructive/live (opening firewall ports, deploying, pushing), check
`references/conventions.md`'s relevant section before acting — several of those are
gated behind explicit user confirmation by design, not an oversight to work around.
