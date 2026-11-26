#!/usr/bin/env python3
# Part of lab-in-a-box — which labs this automation node has deployed, and what happened to them.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/lab_registry.py: the record of the labs this automation node has deployed, and the log of what it did to them.

Nothing else in the project knows which labs exist. Every command takes a definition file as an argument, so a
scheduled job has nothing to walk, and the shared cloud DNS VM of an account cannot tell whether any lab still
needs it. This is that list: one file per lab under registry_dir(), written when a lab is created and removed when
it is destroyed.

    lab_name(lab_file)              -> the name a lab is recorded under, which is its definition's base name
    record(definition, lab_file)    -> create or update this lab's entry, and log it
    entry(lab) / entries()          -> one lab's entry, or every lab's, newest first
    forget(lab)                     -> remove a lab's entry, and log it
    drop_nodes(lab, nodes)          -> take nodes off a lab's entry, for ones that no longer exist
    log_event(action, lab, nodes)   -> append one line to event_log()

An entry is {"schema_version", "lab", "definition", "mydomain", "nodes", "created", "updated"}: enough for a
scheduled run to load the definition again and ask each node's provider about it, and no copy of the definition
itself, which would go stale the moment the operator edited the real one.

The event log is one line per action, "<timestamp> <action> <lab> <fqdn> ...", appended and never rewritten. It
answers what happened to a lab and when, including nodes that were deleted outside this tooling, which the entry
itself cannot show because it only ever holds what is there now.
"""

__version__ = "__LABVERSION__"

import json
import os
import time
from pathlib import Path

# Raised with any change to the entry shape, and recorded in schemas/compatibility.json against
# "config:lab-registry.json" (templates/lab-registry.json.example is the documented form).
LAB_REGISTRY_SCHEMA_VERSION = "1.0"

# Beside the credential store, which is the same kind of per-automation-node state, with the same local fallback so
# this works in a checkout and in a test container as well as on an installed node.
_DEFAULT_REGISTRY_DIRS = ["/etc/lab_creation/labs", "lab_creation-labs"]
_DEFAULT_EVENT_LOG = "/var/log/lab-in-a-box/labs.log"

# Every action the log records. "vanished" is the one no command causes: a scheduled check found the node gone from
# its provider, which means somebody deleted it outside this tooling.
ACTIONS = ("created", "updated", "destroyed", "removed", "vanished")


def registry_dir(config=None) -> Path:
    """
    Where this automation node records its labs: config["LAB_REGISTRY_DIR"] when set, else the first of the
    built-in locations that exists, else the first one, created on the next write.
    """
    custom = (config or {}).get("LAB_REGISTRY_DIR")
    if custom:
        return Path(custom)
    for candidate in _DEFAULT_REGISTRY_DIRS:
        if Path(candidate).is_dir():
            return Path(candidate)
    return Path(_DEFAULT_REGISTRY_DIRS[0])


def event_log(config=None) -> Path:
    """The file every lab action is appended to: config["LAB_EVENT_LOG"] when set, else the built-in location."""
    return Path((config or {}).get("LAB_EVENT_LOG") or _DEFAULT_EVENT_LOG)


def lab_name(lab_file) -> str:
    """
    The name a lab is recorded under: its definition file's base name without the extension.

    The definition's own path is what the operator types and what a scheduled run has to load again, but it is not
    a name: two labs can sit in different directories and a path is a poor file name. The base name is what the
    operator already calls the lab everywhere else, the LAB SUMMARY included.
    """
    return Path(str(lab_file or "")).name.rsplit(".", 1)[0]


def _timestamp() -> str:
    """Now, as the log and the entries write it: UTC, to the second, sortable as text."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _entry_path(lab: str, config=None) -> Path:
    return registry_dir(config) / "{}.json".format(lab)


def node_names(definition) -> list:
    """Every node name a definition declares, in its own order."""
    return list((definition or {}).get("nodes") or {})


def _domain(definition) -> str:
    """
    The DNS zone a definition's records go in: common.mydomain, or the first one any of its kclusters names.

    A lab states it in either place, and a scheduled run needs it to read what the zone holds for a node.
    """
    common = (definition or {}).get("common") or {}
    if common.get("mydomain"):
        return str(common["mydomain"])
    for cluster in ((definition or {}).get("kclusters") or {}).values():
        if (cluster or {}).get("mydomain"):
            return str(cluster["mydomain"])
    cluster = (definition or {}).get("cluster") or {}
    return str(cluster.get("mydomain") or "")


def log_event(action: str, lab: str, nodes=(), config=None) -> bool:
    """
    Append one line to event_log(): "<timestamp> <action> <lab> <fqdn> ...". Returns whether it was written.

    Never fatal and never raises: a lab that was built is not unbuilt because its log line could not be written,
    and this is called from the middle of create and destroy paths.
    """
    if action not in ACTIONS:
        raise ValueError("not a lab event: {!r} (one of {})".format(action, ", ".join(ACTIONS)))
    line = " ".join([_timestamp(), action, lab or "-"] + sorted(nodes or []))
    path = event_log(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(line + "\n")
        return True
    except OSError:
        return False


def entry(lab: str, config=None) -> dict:
    """Lab `lab`'s entry, or {} when it has none, or its file is not readable JSON."""
    try:
        return json.loads(_entry_path(lab, config).read_text())
    except (OSError, ValueError):
        return {}


def entries(config=None) -> list:
    """Every lab's entry, most recently updated first; [] when nothing is recorded."""
    directory = registry_dir(config)
    try:
        names = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    found = [entry(path.name[:-len(".json")], config) for path in names]
    return sorted((e for e in found if e.get("lab")), key=lambda e: e.get("updated", ""), reverse=True)


def record(definition, lab_file, config=None) -> dict:
    """
    Create or update lab `lab_file`'s entry from `definition`, and log it; returns the entry as written.

    Idempotent: a rerun over the same lab rewrites the entry with the nodes the definition now declares and keeps
    the moment it was first recorded. The entry holds the definition's path rather than its content, so an operator
    editing the real file does not leave a stale copy here.
    """
    lab = lab_name(lab_file)
    existing = entry(lab, config)
    nodes = node_names(definition)
    written = {
        "schema_version": LAB_REGISTRY_SCHEMA_VERSION,
        "lab": lab,
        "definition": str(Path(str(lab_file)).resolve()),
        "mydomain": _domain(definition),
        "nodes": nodes,
        "created": existing.get("created") or _timestamp(),
        "updated": _timestamp(),
    }
    path = _entry_path(lab, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".new")
    staged.write_text(json.dumps(written, indent=1, sort_keys=True) + "\n")
    os.replace(str(staged), str(path))
    log_event("updated" if existing else "created", lab, nodes, config)
    return written


def drop_nodes(lab: str, nodes, config=None, action: str = "removed") -> dict:
    """
    Take `nodes` off lab `lab`'s entry and log them under `action`; returns the entry as it now stands, or {} when
    the lab has no entry or none of `nodes` was on it.

    A lab left with no nodes is forgotten entirely: an entry naming nothing would have a scheduled run load a
    definition every time to ask about no nodes at all.
    """
    current = entry(lab, config)
    going = [node for node in (nodes or []) if node in (current.get("nodes") or [])]
    if not current or not going:
        return {}
    remaining = [node for node in current["nodes"] if node not in going]
    log_event(action, lab, going, config)
    if not remaining:
        _entry_path(lab, config).unlink()
        log_event("destroyed", lab, (), config)
        return {}
    current["nodes"] = remaining
    current["updated"] = _timestamp()
    path = _entry_path(lab, config)
    staged = path.with_name(path.name + ".new")
    staged.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    os.replace(str(staged), str(path))
    return current


def forget(lab_file, config=None) -> bool:
    """
    Remove lab `lab_file`'s entry and log it destroyed. Returns whether there was an entry to remove.

    Called by destroy_lab.py once the lab is gone. A lab that was never recorded, which is every lab built before
    this registry existed, is not an error: there is simply nothing to remove.
    """
    lab = lab_name(lab_file)
    path = _entry_path(lab, config)
    nodes = (entry(lab, config).get("nodes") or [])
    try:
        path.unlink()
    except OSError:
        return False
    log_event("destroyed", lab, nodes, config)
    return True
