#!/usr/bin/env python3
# Part of lab-in-a-box — deployment-target model.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/targets.py — where things run: baremetal / VM / container.

A `nodes` entry marked `"existing": true` in the lab JSON is a pre-
provisioned host (baremetal or an already-running VM) — the tool never
creates or destroys it, only talks to it over SSH. Everything else keeps
behaving exactly as before: no `existing` key means today's default (a VM
this tool creates in phase_create_vms, backed by a compute backend).

An existing node is operationally indistinguishable from an already-running
VM this tool didn't create — both are just "a host reachable over SSH that
must not be provisioned or torn down" — so node_kind() returns the same
TARGET_BAREMETAL value for both. That's intentional, not a limitation: the
only thing that ever mattered to this tool is whether it owns the
create/destroy lifecycle.
"""

import subprocess

TARGET_BAREMETAL = "baremetal"
TARGET_VM = "vm"
TARGET_CONTAINER = "container"


def is_existing_node(node_cfg):
    """True when this node entry is a pre-provisioned host the tool must not create or destroy."""
    return bool((node_cfg or {}).get("existing"))


def node_kind(definition, node_name):
    """
    TARGET_VM (default — this tool creates/destroys it) or TARGET_BAREMETAL
    (existing/pre-provisioned; see module docstring for why the two
    "already there" cases share one value).
    """
    node_cfg = (definition.get("nodes", {}) or {}).get(node_name, {}) or {}
    return TARGET_BAREMETAL if is_existing_node(node_cfg) else TARGET_VM


def check_ssh_only_reachability(node_name, timeout=5):
    """
    One-shot SSH reachability probe for an existing node, for use in
    validate_lab_definition()'s preflight.

    Deliberately NOT check_ssh_conn(): that function is a blocking retry
    loop (~200s by default) meant for "wait for a freshly (re)booted VM to
    come up", and it die()s (hard-aborts the whole process) on timeout.
    Preflight validation must stay fast and must report an unreachable host
    as one more cataloged error, not hang for minutes and then abort
    everything else being checked. Runtime code that actually waits for an
    existing node to be reachable (setup_lab.py's phase_create_vms) calls
    check_ssh_conn() directly instead — see MIGRATION_TODO.md Phase 5 §5.2.

    Returns True/False; never raises or dies.
    """
    # stdout=PIPE/stderr=PIPE/universal_newlines=True, not capture_output=/
    # text= (Python 3.7+ only) — this project's containerized test suite
    # (and even the real automation VM's own bare `python3`) runs Python
    # 3.6. Not currently reachable from any bare-python3 entry point today
    # Kept consistent with the rest of the codebase, so a future bare-python3 caller
    # does not hit a version mismatch.
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout={}".format(timeout), "-q", "root@{}".format(node_name), "echo ok"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    return (result.stdout or "").strip() == "ok"
