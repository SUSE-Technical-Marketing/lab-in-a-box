#!/usr/bin/env python3
# Part of lab-in-a-box — addon plugin-capability model.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/apps.py: the plugin registry for install_<addon> executables.

An add-on is any executable named install_<addon>, with or without a file extension (install_<addon>.py,
install_<addon>.sh, a compiled binary, ...), written in any language. Its contract:

    install_<addon> --schema json      prints its schema as JSON, with a "capabilities" object:
                                       {"targets": [...], "layers": [...], "requires_kubernetes": [...] or null,
                                        "aux_services": [...]}
    install_<addon> --capabilities     prints the same capabilities as JSON
    install_<addon> --validate <json>  exits non-zero, printing [ERROR] lines, when its config in the lab is invalid
    install_<addon> --version | --help
    install_<addon> <lab.json>         installs it; the target VM is in $_vm_name, the cluster in $clu_name

Python add-ons get all of this from libs/addon_common.handle_common_args() and a module-level PLUGIN dict:

    PLUGIN = {
        "name": "mariadb",
        "targets": ["container"],               # subset of "container", "vm", "baremetal": where it may be placed (libs/targets.py)
        "layers": ["kubernetes"],               # subset of libs/layers.py's LAYER_*: how it can be installed (descriptive only)
        "requires_kubernetes": ["rke2", "k3s"], # or None if not a container addon
        "aux_services": [],                     # names from the services registry
    }

describe() runs `--schema json` and caches the output per file signature; load_plugin() returns the capabilities from it.
An add-on whose output is missing or invalid falls back to a conservative default, which still validates it as before the
registry existed.
"""

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional

from lab_creation import die

DEFAULT_PLUGIN = {
    "name": None,
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

_cache = {}


_DESCRIBE_TIMEOUT = 120
_LIBS = os.path.dirname(os.path.abspath(__file__))


def addon_name(filename: str) -> str:
    """Return the add-on executable's name without its file extension: install_x.py -> install_x."""
    return os.path.basename(filename).split(".", 1)[0]


def addon_files(directory: str) -> Dict[str, str]:
    """
    Return {install_<addon>: path} for every add-on executable in directory. When install_<addon> exists both with and
    without an extension, the last one in sorted order wins, the same file install_automation_node_scripts.sh deploys.
    """
    found = {}
    if not os.path.isdir(directory):
        return found
    for fname in sorted(os.listdir(directory)):
        path = os.path.join(directory, fname)
        if fname.startswith("install_") and os.path.isfile(path):
            found[addon_name(fname)] = path
    return found


def _cache_file() -> str:
    """Return the describe() cache path: $LAB_ADDON_CACHE, else lab_creation/addons.json in the user's cache directory."""
    return os.environ.get("LAB_ADDON_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "lab_creation", "addons.json")


def _signature(path: str) -> List[int]:
    """Return what invalidates a cached describe() result: the add-on file, and the lab_schema and libs it may use."""
    sig = []
    for p in (path, os.path.join(os.path.dirname(path), "lab_schema"),
              os.path.join(_LIBS, "addon_common.py"), os.path.join(_LIBS, "apps.py")):
        try:
            st = os.stat(p)
            sig += [st.st_mtime_ns, st.st_size]
        except OSError:
            sig += [0, 0]
    return sig


def _load_cache() -> dict:
    try:
        with open(_cache_file()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(data: dict) -> None:
    path = _cache_file()
    tmp = "{}.{}".format(path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _run_schema(path: str) -> dict:
    """Return the add-on's `--schema json` output, or {} when it is not executable, fails or prints no JSON object."""
    if not os.access(path, os.X_OK):
        return {}
    try:
        r = subprocess.run([path, "--schema", "json"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=_DESCRIBE_TIMEOUT)
        out = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return out if isinstance(out, dict) else {}


def describe_many(paths: Iterable[str]) -> Dict[str, dict]:
    """Return {path: describe(path)}, running the uncached add-ons in parallel."""
    paths = [os.path.realpath(p) for p in paths]
    cache = _load_cache()
    result, todo = {}, []
    for p in paths:
        key = hashlib.sha256(p.encode()).hexdigest()
        entry = cache.get(key)
        if entry and entry.get("sig") == _signature(p):
            result[p] = entry["out"]
        else:
            todo.append((p, key))
    if todo:
        with ThreadPoolExecutor(max_workers=min(len(todo), os.cpu_count() or 4)) as pool:
            outs = list(pool.map(lambda item: _run_schema(item[0]), todo))
        for (p, key), out in zip(todo, outs):
            result[p] = out
            if out:
                cache[key] = {"sig": _signature(p), "out": out}
        _save_cache(cache)
    return result


def describe(path: str) -> dict:
    """Return the add-on executable's `--schema json` output (cached), or {} when it provides none."""
    real = os.path.realpath(path)
    return describe_many([real])[real]


def load_plugin_from_path(path: Optional[str], name: Optional[str] = None) -> dict:
    """
    Return the capabilities of the add-on executable at `path`, an explicit filesystem path, from its `--schema json`
    output. load_plugin() uses a PATH lookup instead, and every CLI call site uses that. In a development checkout the
    scripts are not on PATH, so webui/lib/discovery.py calls this function.

    Returns a copy of DEFAULT_PLUGIN, with "name" filled in, when the file does not exist or prints no capabilities.
    """
    plugin = dict(DEFAULT_PLUGIN, name=name)
    if not path or not os.path.isfile(str(path)):
        return plugin
    caps = describe(str(path)).get("capabilities")
    if isinstance(caps, dict) and caps:
        plugin.update(caps)
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
    Return the addon name from one addons[] entry. An entry is either a plain "<addon>" string, where the addon uses only the
    shared top-level config, or a single-key {"<addon>": {...}} mapping. The mapping gives one node its own override of that addon's
    config. For example, several nodes can register against one server, each with its own activation key.

    Every consumer of an addons[] list uses this function, so they agree on the shape. These are collect_addon_names(),
    k8s.addon_nodes(), k8s.addon_node_config() and phase_vm_addons() in setup_lab.py.
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
