#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will register a host as a Salt client of an
# existing Uyuni/SMLM server
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://www.uyuni-project.org/uyuni-docs/en/uyuni/client-configuration/registration-bootstrap.html
#            https://documentation.suse.com/multi-linux-manager/5.2/en/docs/client-configuration/registration-bootstrap.html
# (verified live 2026-08-28 — identical mechanism across Uyuni and every
# current SMLM version; see libs/spacecmd_common.py's
# module docstring, "Client registration" section, for the full research
# notes and caveats)
#
# This is the CLIENT side — install_uyuni.py/install_smlm.py install the
# SERVER. This addon runs on a plain VM/baremetal node (nodes[x].addons, not
# a kcluster addon) and points it at an already-running server elsewhere in
# the lab (or outside it entirely — client_registration_server just needs to
# be reachable by FQDN).
#
# ─── JSON section: "client_registration" ────────────────────────────────────
#
# MANDATORY
#   client_registration_server           : FQDN of the target Uyuni/SMLM server
#                                           (the bootstrap URL is built from this:
#                                           https://<server>/pub/bootstrap/bootstrap.sh)
#   client_registration_activation_key   : activation key label to bootstrap with
#                                           (e.g. "1-mykey") — created on the server
#                                           if it doesn't already exist, see below
#
# OPTIONAL – server access (for the "ensure the key/channels exist" preflight
#            and salt-key acceptance; same two deployment shapes install_uyuni.py/
#            install_smlm.py themselves use)
#   client_registration_server_type      : "uyuni" (default, single-VM podman/mgradm)
#                                           or "smlm" (Kubernetes, kubectl exec)
#                                           (options: uyuni, smlm)
#   client_registration_server_node      : SSH host to run spacecmd/mgrctl/kubectl
#                                           commands on (default: same as
#                                           client_registration_server for "uyuni";
#                                           REQUIRED for "smlm" — the k8s node
#                                           running kubectl isn't necessarily the
#                                           server's own ingress FQDN)
#   client_registration_server_ns        : Kubernetes namespace ("smlm" only,
#                                           default: uyuni-server)
#   client_registration_admin_user       : spacecmd admin user (default: admin)
#   client_registration_admin_pass       : spacecmd admin password
#                                           (default: Uyuni12345 for "uyuni",
#                                           admin123 for "smlm" — each product's
#                                           own install default)
#
# OPTIONAL – activation key auto-creation, if client_registration_activation_key
#            doesn't already exist on the server (same fields
#            ensure_activation_key already expects under any prefix — see
#            install_uyuni.py's own uyuni_activation_key_* for the full set)
#   client_registration_activation_key_base_channel   : required to CREATE the key
#   client_registration_activation_key_child_channels : space-separated
#   client_registration_activation_key_desc           : default: the key name
#   client_registration_sync_channels                 : space-separated channels
#                                           to mgr-sync before creating the key,
#                                           if they aren't already synced
#   client_registration_background_retry_delay        : if the channels this
#                                           registration depends on (sync_channels
#                                           PLUS the activation key's own base/child
#                                           channels) aren't ALREADY fully synced —
#                                           not just "exists" — registration is
#                                           handed off to a detached background
#                                           worker instead of blocking the rest of
#                                           the deployment; this is the seconds
#                                           between that worker's retry attempts
#                                           (default: 60). It never gives up on its
#                                           own — only stops once registration
#                                           actually succeeds. See
#                                           _launch_background_retry()/
#                                           _retry_until_registered() and
#                                           spacecmd_common.wait_for_channels_synced().
#
# OPTIONAL – bootstrap behavior
#   client_registration_reactivation_key : passed as REACTIVATION_KEY, for
#                                           re-registering a previously
#                                           registered system
#   client_registration_retry_limit      : how many times to poll for the
#                                           minion's key to appear pending
#                                           after bootstrap (default: 30)
#   client_registration_retry_interval   : seconds between polls (default: 10)

__version__ = "526bc48"

PLUGIN = {
    "name": "client_registration",
    "targets": ["vm", "baremetal"],
    "layers": ["os-native"],
    "requires_kubernetes": None,
    "aux_services": [],
}

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
import spacecmd_common as sc  # noqa: E402
from lab_creation import die  # noqa: E402


def _validate(v):
    v.vreq("client_registration", "client_registration_server")
    v.vreq("client_registration", "client_registration_activation_key")
    cfg = v.definition.get("client_registration", {}) or {}
    server_type = cfg.get("client_registration_server_type") or "uyuni"
    if server_type not in ("uyuni", "smlm"):
        v.errors.append(
            "[ERROR] client_registration.client_registration_server_type='{}': "
            "must be 'uyuni' or 'smlm'".format(server_type))
    if server_type == "smlm":
        v.vreq("client_registration", "client_registration_server_node")


def _server_access(cfg):
    """
    Returns (server_node, exec_prefix, admin_user, admin_pass) for the two
    deployment shapes install_uyuni.py/install_smlm.py themselves use.
    """
    server_type = cfg.get("client_registration_server_type") or "uyuni"
    server_fqdn = cfg.get("client_registration_server")
    admin_user = cfg.get("client_registration_admin_user") or "admin"

    if server_type == "smlm":
        server_node = cfg.get("client_registration_server_node")
        if not server_node:
            die("client_registration_server_node is required when "
                "client_registration_server_type is 'smlm'")
        ns = cfg.get("client_registration_server_ns") or "uyuni-server"
        exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
        admin_pass = cfg.get("client_registration_admin_pass") or "admin123"
    else:
        server_node = cfg.get("client_registration_server_node") or server_fqdn
        exec_prefix = "mgrctl exec --"
        admin_pass = cfg.get("client_registration_admin_pass") or "Uyuni12345"

    return server_node, exec_prefix, admin_user, admin_pass


_RETRY_LOG_DIR = "/var/log/lab-in-a-box"
_RETRY_WORKER_FLAG = "--retry-worker"


def _wait_channels(cfg):
    """
    The sorted list of channel labels this registration actually depends
    on: the explicit client_registration_sync_channels list, plus the
    activation key's own base/child channels when auto-created here (see
    ensure_activation_key()'s own cfg fields) — those are what the client
    will actually consume, whether or not they were separately named in
    sync_channels too.
    """
    wait_channels = set((cfg.get("client_registration_sync_channels") or "").split())
    base_channel = cfg.get("client_registration_activation_key_base_channel")
    if base_channel:
        wait_channels.add(base_channel)
    wait_channels.update((cfg.get("client_registration_activation_key_child_channels") or "").split())
    return sorted(wait_channels)


def _register_now(vm_name, cfg, server_node, exec_prefix, server_fqdn, activation_key):
    """The actual, unconditional registration call — no channel-readiness check of its
    own. Shared by register_client()'s fast synchronous path and the background retry
    worker's own loop below."""
    sc.ensure_client_registered(
        server_node, exec_prefix, vm_name, server_fqdn, activation_key,
        reactivation_key=cfg.get("client_registration_reactivation_key"),
        retry_limit=int(cfg.get("client_registration_retry_limit") or 30),
        retry_interval=int(cfg.get("client_registration_retry_interval") or 10),
    )


def _launch_background_retry(json_file, vm_name):
    """
    Spawns a DETACHED background worker (this same script, re-invoked with
    _RETRY_WORKER_FLAG and _vm_name set) that keeps retrying vm_name's
    registration — no artificial timeout, it only stops once registration
    has actually succeeded — while THIS process returns immediately so the
    rest of the deployment keeps moving. Matches this project's existing
    nohup'd-background-worker convention (see e.g. install_smlm.py's own
    channel-sync monitor). Output goes to a per-node log file rather than
    setup_lab.py's own captured output, since this worker outlives the
    addon invocation that launched it.

    Added 2026-09-21 per explicit user requirement, replacing a first
    version of this fix that blocked synchronously with a timeout instead:
    with many nodes sharing the same still-syncing channel, each would
    have waited out its own full timeout in turn, potentially adding hours
    to a lab deployment that's already going to fail on all of them
    anyway. A detached per-node background retry lets the deployment
    finish its OWN timeline while each pending registration finishes (or
    keeps trying) independently, on its own.
    """
    Path(_RETRY_LOG_DIR).mkdir(parents=True, exist_ok=True)
    log_path = str(Path(_RETRY_LOG_DIR) / "client-registration-retry-{}.log".format(vm_name))
    resolved_json = str(Path(json_file).resolve())
    env = dict(os.environ)
    env["_vm_name"] = vm_name
    with open(log_path, "a") as logf:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), resolved_json, _RETRY_WORKER_FLAG],
            stdout=logf, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    print("  Background retry log: {}".format(log_path))


def _retry_until_registered(vm_name, cfg):
    """
    The background worker's own loop — runs forever (no deadline), waiting
    for this registration's required channels to finish syncing
    (sc.wait_for_channels_synced(..., timeout=None)) and then registering,
    catching ANY failure along the way (a transient spacecmd hiccup, a
    channel that fails and needs another sync cycle, etc.) and simply
    trying the whole sequence again after a real pause — until it
    genuinely succeeds, then returns. Intended to be run standalone, as
    it's own detached process (see _launch_background_retry()), never
    inline in the main deployment path.
    """
    server_fqdn = cfg.get("client_registration_server")
    activation_key = cfg.get("client_registration_activation_key")
    server_node, exec_prefix, admin_user, admin_pass = _server_access(cfg)
    wait_channels = _wait_channels(cfg)
    retry_delay = int(cfg.get("client_registration_background_retry_delay") or 60)

    attempt = 0
    while True:
        attempt += 1
        try:
            sc.ensure_spacecmd_config(server_node, exec_prefix, admin_user, admin_pass)
            if wait_channels:
                sc.wait_for_channels_synced(server_node, exec_prefix, wait_channels, timeout=None)
            _register_now(vm_name, cfg, server_node, exec_prefix, server_fqdn, activation_key)
            print("[{}] '{}' registered successfully on attempt {}".format(
                datetime.now(timezone.utc).isoformat(), vm_name, attempt))
            return
        except SystemExit as e:
            print("[{}] attempt {} for '{}' failed (exit {}) — retrying in {}s".format(
                datetime.now(timezone.utc).isoformat(), attempt, vm_name, e.code, retry_delay))
        except Exception as e:  # noqa: BLE001 — deliberately broad: this loop must never die
            print("[{}] attempt {} for '{}' raised {}: {} — retrying in {}s".format(
                datetime.now(timezone.utc).isoformat(), attempt, vm_name, type(e).__name__, e, retry_delay))
        time.sleep(retry_delay)


def register_client(vm_name, cfg, json_file=None):
    server_fqdn = cfg.get("client_registration_server")
    activation_key = cfg.get("client_registration_activation_key")
    if not server_fqdn or not activation_key:
        die("client_registration_server and client_registration_activation_key are required")

    server_node, exec_prefix, admin_user, admin_pass = _server_access(cfg)

    sc.ensure_spacecmd_config(server_node, exec_prefix, admin_user, admin_pass)
    sync_channels = (cfg.get("client_registration_sync_channels") or "").split()
    sc.ensure_channels_synced(server_node, exec_prefix, sync_channels)
    sc.ensure_activation_key(server_node, exec_prefix, cfg, "client_registration")

    # If every channel this registration depends on is ALREADY fully synced
    # (not just "exists" — ensure_channels_synced() above only checks
    # existence and kicks off a sync, it never waits for one to finish),
    # register right now, synchronously, exactly as before. Otherwise,
    # don't block the rest of the deployment waiting for a sync that could
    # take a long time — hand off to a detached background retry (see
    # _launch_background_retry()) and return immediately.
    wait_channels = _wait_channels(cfg)
    pending = sc.pending_channels(server_node, exec_prefix, wait_channels) if wait_channels else set()
    if pending:
        if not json_file:
            die("register_client() needs json_file to launch a background retry")
        _launch_background_retry(json_file, vm_name)
        print("  Channel(s) not yet fully synced ({}) — launched a background retry that will "
              "keep trying until registration succeeds; continuing with the rest of the "
              "deployment".format(", ".join(sorted(pending))))
        return

    _register_now(vm_name, cfg, server_node, exec_prefix, server_fqdn, activation_key)


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    # _RETRY_WORKER_FLAG mode: this is a detached background worker spawned
    # by _launch_background_retry(), re-invoking this same script — runs
    # ONLY the single node named by _vm_name (required here), in an
    # unconditional retry-forever loop, never the normal per-node
    # dispatch/registration path below. See _retry_until_registered()'s
    # own docstring.
    if len(sys.argv) > 2 and sys.argv[2] == _RETRY_WORKER_FLAG:
        vm_name = os.environ.get("_vm_name")
        if not vm_name:
            die("{} requires _vm_name to be set".format(_RETRY_WORKER_FLAG))
        eff_cfg = k8s.addon_node_config(definition, "client_registration", vm_name)
        _retry_until_registered(vm_name, eff_cfg)
        return

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "client_registration", vm_name=env_vm_name):
        # Per-node override: a lab that registers many different OSes against
        # one shared server needs a different activation_key (and, in
        # principle, any other client_registration_* field) per node — e.g.
        # solar-system-lab.json's 15 different distros all registering
        # against the same smlm52beta.mydemo.lab, each with its own key. A
        # node expresses this via its own addons[] entry:
        # {"client_registration": {"client_registration_activation_key": "..."}}
        # instead of a plain "client_registration" string — see
        # k8s.addon_node_config()'s docstring (added 2026-09-11; this used to
        # be a flat nodes[x].client_registration_* field, replaced by the
        # nested addons[]-scoped form so it's unambiguous which addon a
        # per-node override belongs to, and so the same mechanism works for
        # any addon, not just this one).
        eff_cfg = k8s.addon_node_config(definition, "client_registration", vm_name)
        register_client(vm_name, eff_cfg, json_file=json_file)


if __name__ == "__main__":
    main()
