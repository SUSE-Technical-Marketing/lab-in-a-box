#!/usr/bin/env python3
# Part of lab-in-a-box — addon plugin-capability model.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/apps.py — plugin capability registry for install_<addon> scripts.

Every scripts/install_<addon>.py declares a module-level PLUGIN dict:

    PLUGIN = {
        "name": "mariadb",
        "targets": ["container"],              # subset of "container"/"vm"/"baremetal"
                                                 # (libs/targets.py — WHERE it may be placed)
        "layers": ["kubernetes"],               # subset of libs/layers.py's LAYER_* — HOW it
                                                 # can be installed; descriptive only, doesn't
                                                 # gate validation the way "targets" does
        "requires_kubernetes": ["rke2", "k3s"], # or None if not a container addon
        "aux_services": [],                     # names from the (future) services registry
    }

load_plugin() imports the addon script as a module (without running its
main()) and returns that dict, falling back to a conservative default for
any addon script found in PATH that hasn't been given a PLUGIN yet — so an
addon nobody has classified still validates exactly as before this model
existed, rather than breaking.
"""

import importlib.util
import os
from importlib.machinery import SourceFileLoader
import shutil
import sys

from lab_creation import die

DEFAULT_PLUGIN = {
    "name": None,
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

_cache = {}


def load_plugin_from_path(path, name=None):
    """
    Return the PLUGIN dict for the addon script at `path` (an explicit
    filesystem path, not a PATH lookup — see load_plugin() below for the
    PATH-based variant every CLI call site actually uses). This is what a
    dev-mode caller needs: scripts/install_<x>.py isn't on
    $PATH in a repo checkout, so shutil.which()-based lookup can't find it,
    which webui/lib/discovery.py otherwise runs into every time. Returns a
    copy of DEFAULT_PLUGIN (with "name" filled in) if the file doesn't exist
    or has no PLUGIN dict of its own — matches load_plugin()'s same
    graceful fallback, including for a non-Python addon script that raises
    on import (install_ds389 was the last real example of this until it
    was ported 2026-09-21; the fallback path itself stays, for whatever
    addon is next to arrive mid-port or genuinely broken).
    """
    plugin = dict(DEFAULT_PLUGIN, name=name)
    if not path or not os.path.isfile(str(path)):
        return plugin
    try:
        # A deployed script has no .py suffix (install_automation_node_scripts.sh
        # copies it to /usr/local/bin/install_<addon>), so
        # spec_from_file_location can't infer a loader from the extension —
        # an explicit SourceFileLoader is required, extension or not.
        mod_name = "install_{}_plugin".format(name or os.path.basename(str(path)))
        loader = SourceFileLoader(mod_name, str(path))
        spec = importlib.util.spec_from_loader(mod_name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        found = getattr(mod, "PLUGIN", None)
        if found:
            plugin = found
    except Exception:
        # Any import-time failure (missing dependency, syntax error in an
        # addon under development, a bash script that isn't valid Python at
        # all — install_ds389 was the standing example of this until it was
        # ported 2026-09-21) falls back to the default rather than breaking
        # validation/orchestration/discovery over one script.
        pass
    return plugin


def load_plugin(name):
    """
    Return the PLUGIN dict for addon `name` (looks up install_<name> in
    PATH, same resolution setup_lab.py already uses via shutil.which). Thin
    wrapper around load_plugin_from_path() — every CLI/orchestration call
    site (validate_lab_definition, setup_lab.py) resolves addons via PATH,
    so this is what they use; discovery.py uses load_plugin_from_path()
    directly instead, since it already has the addon's real file path from
    its own directory-scanning discovery.
    """
    if name in _cache:
        return _cache[name]

    exe = shutil.which("install_{}".format(name))
    plugin = load_plugin_from_path(exe, name=name)
    _cache[name] = plugin
    return plugin


def addon_entry_name(entry):
    """
    The addon name from one addons[] list entry. An entry is either a plain
    "<addon>" string (today's shape, unchanged — the addon runs with only
    its shared top-level config section), or a single-key
    {"<addon>": {...fields...}} mapping for a node that needs its own
    per-node override of that addon's config (added 2026-09-11 — e.g. a lab
    registering many different OSes against one Uyuni/SMLM server, each
    node needing its own client_registration_activation_key). Every
    consumer of an addons[] list (collect_addon_names below, k8s.
    addon_nodes(), k8s.addon_node_config(), setup_lab.py's phase_vm_addons)
    goes through this one function so they can never disagree on the shape.
    """
    if isinstance(entry, dict):
        if len(entry) != 1:
            die("addons[] entry {!r} must have exactly one key (the addon name) — "
                "got {}".format(entry, len(entry)))
        return next(iter(entry))
    return entry


def addon_entry_overrides(entry):
    """The per-node override dict from one addons[] list entry — {} for a
    plain-string entry (nothing to override), or the entry's own single
    value for a {"<addon>": {...}} entry. See addon_entry_name()'s
    docstring for the full shape."""
    if isinstance(entry, dict):
        return entry[addon_entry_name(entry)] or {}
    return {}


def collect_addon_names(definition):
    """
    Every addon name referenced anywhere in a lab definition — both
    kclusters[x].addons (cluster-level) and nodes[x].addons (VM-level) —
    as a sorted list of uniques. setup_lab.py's own two addon-install
    loops (_install_cluster_addons/phase_vm_addons) walk these same two
    places independently, once addon-config validation needed to walk
    them too (--validate every addon up front, before any VM/cluster
    work starts) it made sense to have one shared place doing the
    walking rather than a third copy of the same two loops.
    """
    names = set()
    for clu_cfg in (definition.get("kclusters", {}) or {}).values():
        names.update(addon_entry_name(e) for e in (clu_cfg or {}).get("addons") or [])
    for node_cfg in (definition.get("nodes", {}) or {}).values():
        names.update(addon_entry_name(e) for e in (node_cfg or {}).get("addons") or [])
    return sorted(names)


def attach_capabilities(schema_dict, plugin_dict):
    """
    Merge an addon's PLUGIN capabilities into its --schema output (or any
    other dict), as a "capabilities" key — the single place both
    addon_common.py's --schema dispatch and webui/lib/discovery.py's
    schema()/discover() build this from, so the CLI and the webui can never
    disagree about the shape. Mutates and returns schema_dict.
    """
    schema_dict["capabilities"] = {
        "targets": plugin_dict.get("targets") or [],
        "layers": plugin_dict.get("layers") or [],
        "requires_kubernetes": plugin_dict.get("requires_kubernetes"),
        "aux_services": plugin_dict.get("aux_services") or [],
    }
    return schema_dict


def supports(plugin, target_kind):
    """True when plugin declares support for target_kind ("container"/"vm"/"baremetal")."""
    return target_kind in (plugin.get("targets") or [])


def requirement_issue(plugin, target_kind, clu_type=None):
    """
    Returns a human-readable problem string if placing `plugin` at
    `target_kind` (and, for a container placement, on a kcluster of
    `clu_type`) is invalid — or None when the placement is fine.

    This is the non-raising half of check_requirements(), split out so
    validate_lab_definition() can catalog the problem as one more preflight
    [ERROR] alongside everything else, rather than depend on recovering a
    message from a caught SystemExit (die() only prints its message to
    stderr — the raised SystemExit itself carries no text, just an exit
    code, so catching it can't recover what went wrong).
    """
    name = plugin.get("name") or "?"

    if not supports(plugin, target_kind):
        return "does not support target '{}' — supported: {}".format(
            target_kind, ", ".join(plugin.get("targets") or []) or "none")

    if target_kind == "container":
        required = plugin.get("requires_kubernetes")
        if required and clu_type not in required:
            return "requires a Kubernetes distribution in {} — cluster is '{}'".format(required, clu_type)

    return None


def check_requirements(plugin, target_kind, clu_type=None):
    """
    die() with a clear message if placing `plugin` at `target_kind` (and, for
    a container placement, on a kcluster of `clu_type`) is invalid. Returns
    normally when the placement is fine. For orchestration call sites
    (setup_lab.py) that want a hard stop; validate_lab_definition() uses
    requirement_issue() directly instead so it can catalog the problem
    rather than abort.
    """
    issue = requirement_issue(plugin, target_kind, clu_type=clu_type)
    if issue:
        die("Addon '{}' {}".format(plugin.get("name") or "?", issue))
