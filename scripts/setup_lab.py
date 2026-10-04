#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will setup a Lab
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/setup_lab.sh — calls the python libraries
# (lab_creation, k8s, primary) directly, in-process. No bash is sourced or
# executed by this script for the DNS/VM/Kubernetes/addon phases below.
# destroy_vm.py/setup_vm.py are still invoked as separate processes for VM
# create/destroy, matching bash's own architecture (setup_lab.sh always called
# destroy_vm.sh/setup_vm.sh as separate scripts too, never sourced them).
# install_<addon> scripts are likewise separate processes, exactly as in bash.

"""
setup_lab.py — provision all VMs defined in a lab JSON, set up Kubernetes
clusters, and install cluster-level and VM-level addons in order.

Usage:
    setup_lab.py [--keep] [--debug] [--parallel[=N]] <lab.json>
"""

__version__ = "fdfe335"
_SCHEMA_VERSION = "1.0"

import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation as lc  # noqa: E402
import k8s  # noqa: E402
import targets  # noqa: E402
import apps  # noqa: E402
import services  # noqa: E402
import backends  # noqa: E402
from destroy_vm import destroy_vm  # noqa: E402
from setup_vm import provision_vm  # noqa: E402

_HELP_TEXT = """\
Usage: setup_lab.py [--keep] [--debug] [--parallel[=N]] <lab.json>

Provisions all VMs defined in the lab JSON, sets up Kubernetes clusters, and
installs cluster-level and VM-level addons in order.

Options:
  --keep    Skip VMs that already exist, are running, match the defined IP and
            MAC address, and are accessible via SSH with default credentials.
            Without this flag (default) every VM is destroyed and recreated.
  --debug   Stream the full output of every command that is run. Without it
            (default) only lab-in-a-box's own messages are shown; a command's
            output is still shown if it fails or emits a warning/error.
  --parallel[=N]
            Create up to N VMs at once instead of one at a time (default
            without this flag: strictly sequential). Bare --parallel uses a
            default of 4 concurrent workers. Only the VM-CREATION phase runs
            in parallel — Kubernetes cluster setup and addon installation
            still run sequentially, in JSON declaration order, since several
            addons depend on another node's own addon having completed
            first (e.g. client_registration needs its server's own smlm/
            uyuni addon already installed).

The lab definition JSON must contain:
  nodes      — map of VM hostname → node config (myip, mymac, kcluster, …)
  common     — shared VM settings (ISO_IMAGE, VM_MEM, VM_DSK, VM_CPU, …)
  kclusters  — map of cluster name → cluster config (clu_type, addons, …)
  <addon>    — one section per addon listed in kclusters[x].addons or nodes[x].addons

Run 'install_<addon> --help' for the options accepted by each addon section.
Run 'setup_lab.py --input-definition [json|yaml]' for the machine-readable schema.
"""


class _RunReport:
    """
    Collects the outcome of each node, cluster and addon during a setup_lab() run, so print_summary() can show what succeeded and what
    needs a fix. Without it, a failed node or addon appeared only as a warning in the middle of the run.

    Statuses: nodes are "created", "reused", "existing" or "FAILED". clusters are "ok" or "FAILED". addons are "ok" or "FAILED (exit N)".

    Warnings and errors are free-text lines from the preflight and from non-fatal failures. The summary repeats them, so the log need
    not be searched for what to fix.
    """

    def __init__(self):
        self.nodes = []      # list of (name, status)
        self.clusters = []   # list of (name, status)
        self.addons = []     # list of (scope, name, status)
        self.warnings = []   # list of str
        self.errors = []     # list of str
        self.resources = None  # (cpu, mem_mib, disk_gib, node_count) or None

    def add_node(self, name, status):
        self.nodes.append((name, status))

    def add_cluster(self, name, status):
        self.clusters.append((name, status))

    def add_addon(self, scope, name, status):
        self.addons.append((scope, name, status))

    def add_warning(self, msg):
        self.warnings.append(msg)

    def add_error(self, msg):
        self.errors.append(msg)

    @property
    def failed(self):
        return (bool(self.errors)
                or any(s == "FAILED" for _, s in self.nodes)
                or any(s == "FAILED" for _, s in self.clusters)
                or any(s.startswith("FAILED") for _, _, s in self.addons))


# Module-level: reset at the top of every setup_lab() run. The phase
# functions append to this as they go; main() reads .failed for its exit code.
_report = _RunReport()


_RULE = "═" * 64


def print_summary():
    """Print a simple end-of-run summary of _report, grouped by outcome, with
    anything that FAILED in red so it stands out. Set apart from the execution
    log above by a full-width rule. Called once at the end of setup_lab() (and
    by main() if a preflight check aborts the run early)."""
    r = _report
    print("\n\n{}{}\n  LAB SUMMARY\n{}{}".format(lc._WHITE, _RULE, _RULE, lc._RESET))

    if r.resources:
        cpu, mem, disk, ncount = r.resources
        print("Resources: {} vCPU, {} MiB RAM, {} GiB disk across {} node(s)".format(
            cpu, mem, disk, ncount))

    if not (r.nodes or r.clusters or r.addons):
        print("  (nothing was provisioned)")

    def _grouped(pairs):
        out = {}
        for label, status in pairs:
            out.setdefault(status, []).append(label)
        return out

    if r.nodes:
        g = _grouped(r.nodes)
        print("Nodes ({}):".format(len(r.nodes)))
        seen = []
        for status in ("created", "reused", "existing", "FAILED"):
            if status in g:
                seen.append(status)
                col = lc._RED if status == "FAILED" else (lc._GREEN if status == "created" else "")
                rst = lc._RESET if col else ""
                print("  {}{:<8}{} {}".format(col, status, rst, ", ".join(sorted(g[status]))))
        for status in g:  # any unexpected status value, don't hide it
            if status not in seen:
                print("  {:<8} {}".format(status, ", ".join(sorted(g[status]))))

    if r.clusters:
        g = _grouped(r.clusters)
        print("Clusters ({}):".format(len(r.clusters)))
        for status in sorted(g):
            col = lc._RED if status == "FAILED" else lc._GREEN
            print("  {}{:<8}{} {}".format(col, status, lc._RESET, ", ".join(sorted(g[status]))))

    if r.addons:
        g = {}
        for scope, name, status in r.addons:
            g.setdefault(status, []).append("{} ({})".format(name, scope))
        print("Addons ({}):".format(len(r.addons)))
        for status in sorted(g):
            col = lc._GREEN if status == "ok" else lc._RED
            print("  {}{:<16}{} {}".format(col, status, lc._RESET, ", ".join(g[status])))

    if r.warnings:
        print("{}Warnings ({}):{}".format(lc._ORANGE, len(r.warnings), lc._RESET))
        for w in r.warnings:
            print("  {}•{} {}".format(lc._ORANGE, lc._RESET, w))

    if r.errors:
        print("{}Errors ({}):{}".format(lc._RED, len(r.errors), lc._RESET))
        for e in r.errors:
            print("  {}•{} {}".format(lc._RED, lc._RESET, e))

    print("")
    if r.failed:
        print("{}✗ Lab setup finished WITH FAILURES{} — see the Errors list and the entries "
              "marked FAILED above for what to fix.".format(lc._RED, lc._RESET))
    elif r.warnings:
        print("{}✓ Lab setup finished — OK, with {} warning(s){} (see the Warnings list above).".format(
            lc._GREEN, len(r.warnings), lc._RESET))
    else:
        print("{}✓ Lab setup finished — everything OK{}.".format(lc._GREEN, lc._RESET))
    print("{}{}{}".format(lc._WHITE, _RULE, lc._RESET))


def _lab_resources(definition):
    """(cpu, mem_mib, disk_gib, node_count) for the whole lab — thin wrapper over
    lc.total_lab_resources() that also carries the node count, for the summary."""
    try:
        cpu, mem, disk = lc.total_lab_resources(definition)
    except Exception:
        cpu, mem, disk = 0, 0, 0
    return (cpu, mem, disk, len(definition.get("nodes", {}) or {}))


def _run_addon(cmd, env):
    """
    Run an addon installer (`install_<addon> [--validate] <lab.json>`).

    Default (no --debug): its stdout/stderr are captured and only printed if it
    exits non-zero or its output looks like it contains a warning/error — so a
    clean run shows only lab-in-a-box's own progress lines. With --debug the
    output streams through live. Returns the CompletedProcess either way.
    """
    if lc.debug_enabled():
        return subprocess.run(cmd, env=env)
    r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    out = (r.stdout or "").rstrip()
    if out and (r.returncode != 0 or lc._looks_noisy(out)):
        print(out)
    return r


def _merged_env(definition, config, defaults, vm_name):
    """
    Merge defaults + cfg + per-VM JSON vars, in bash's actual precedence
    order (JSON wins — load_vm_vars runs last in the bash pipeline).
    """
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(lc.load_vm_vars(definition, vm_name))
    return env


def validate_addon_configs(definition, json_file, issues_out=None):
    """
    Run `install_<addon> --validate <json_file>` for every addon in the lab definition: kclusters[x].addons and nodes[x].addons,
    collected by apps.collect_addon_names(). This happens before any VM or cluster work starts.

    Each addon's validators check its configuration values, such as a namespace. Without this step, the checks run only when an operator
    runs them by hand, and an invalid value surfaces after VMs and clusters already exist. The issues are collected and reported once,
    as validate_lab_definition() does. One bad addon configuration does not leave a half-deployed lab.

    An addon with nothing to validate exits 0, as it does when run by hand.

    Returns True if every addon validates. Raw issue lines are appended to issues_out when it is given. setup_lab.py adds them to the
    Errors list of the summary.
    """
    addon_names = apps.collect_addon_names(definition)
    if not addon_names:
        return True

    issues = []
    for addon in addon_names:
        installer = shutil.which("install_{}".format(addon))
        if not installer:
            issues.append("  {}[ERROR]{} addon '{}': install_{} not found on PATH".format(
                lc._RED, lc._RESET, addon, addon))
            continue
        r = subprocess.run([installer, "--validate", json_file],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode != 0:
            for line in (r.stdout or "").splitlines():
                issues.append("  addon '{}': {}".format(addon, line))

    if issues_out is not None:
        issues_out.extend(issues)

    if issues:
        print("\n".join(issues))
        print("{}✗ Addon-config preflight FAILED{} — fix the above before proceeding.".format(
            lc._RED, lc._RESET))
        return False
    print("{}✓ Addon-config preflight passed{} — {} addon(s) checked.".format(
        lc._GREEN, lc._RESET, len(addon_names)))
    return True


def phase_fetch_images(definition, config, iso_loc):
    """
    Download every base image a node declares with ISO_URL (+ ISO_SHA256 or
    ISO_SHA256_URL) into ISO_LOC on each KVM host that node can be created on,
    before the preflight looks for it there. Already-verified copies are kept;
    see lc.fetch_source_image().
    """
    requests = lc.image_download_requests(definition)
    if not requests:
        return
    lc.log("Fetching base images")
    lc._level += 1
    done = set()
    for vm_name, image, url, sha256, sha256_url in requests:
        for host in lc.image_source_hosts(definition, vm_name, config):
            if (host, image) in done:
                continue
            state = lc.fetch_source_image(host, iso_loc, image, url, sha256, sha256_url)
            lc.log("  {} on {}: {}".format(image, host, state))
            done.add((host, image))
    lc._level -= 1


def phase_services(definition, config, defaults):
    """
    Configure+enable every service listed in common.services (optional;
    absent means today's implicit default of nothing new — DNS/HTTP are
    already running from the automation VM's own bootstrap). Order runs
    before phase_create_vms so PXE/DHCP infrastructure is ready before any
    node might try to boot from it.
    """
    service_names = definition.get("common", {}).get("services") or []
    if not service_names:
        return
    lc.log("Configuring lab services")
    lc._level += 1
    for name in service_names:
        svc = services.get(name, lab_setup_path=defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation"))
        lc.log("Service \"{}{}{}\"".format(lc._RED, name, lc._RESET))
        svc.install()
        svc.configure(definition, config)
        svc.enable()
    lc._level -= 1


def phase_dns(definition, remote_dns_servers):
    lc.log("Add Kubernetes cluster DNS entries")
    lc._level += 1
    for clu_name in k8s.list_kclusters(definition):
        clu_cfg = k8s.load_kclu_vars(definition, clu_name)
        k8s.add_kclu_dns(definition, clu_name, clu_cfg.get("clu_type", ""), clu_cfg.get("mydomain", ""),
                          remote_dns_servers=remote_dns_servers)
    lc._level -= 1


def _create_one_vm(definition, config, defaults, json_file, keep, vm_name, node_cfg, log_prefix=None):
    """
    Create one node's VM: check an existing host, check --keep reusability, destroy the VM if it must be recreated, then provision
    it. phase_create_vms() runs this sequentially or in parallel, so both paths share one implementation.

    log_prefix, when given, is the per-thread log prefix for the duration of the call (see lab_creation.set_log_prefix()). It keeps the
    output of concurrent workers attributable. The sequential path passes None.
    """
    if log_prefix is not None:
        lc.set_log_prefix(log_prefix)
    try:
        lc.log("Node: \"{}{}{}\"".format(lc._RED, vm_name, lc._RESET))

        if targets.is_existing_node(node_cfg):
            lc.log("  Using existing host \"{}{}{}\" — not creating a VM for it".format(
                lc._RED, vm_name, lc._RESET))
            lc.check_ssh_conn(vm_name)
            _report.add_node(vm_name, "existing")
            return

        env = _merged_env(definition, config, defaults, vm_name)
        # The --keep reusability check asks the backend that owns the VM, through get_backend(for_existing=True). It does not assume
        # libvirt. A backend that cannot reach its own API is a failure for that node. It is never read as "VM does not exist", because
        # that could destroy a running VM. The node is skipped, as in the provisioning error handling below.
        keep_reusable = False
        keep_backend_error = None
        if keep:
            try:
                keep_backend = backends.get_backend(definition, config, vm_name, for_existing=True)
                keep_reusable = keep_backend.vm_is_reusable(
                    vm_name, env.get("mymac", ""), env.get("myip", ""))
            except SystemExit:
                keep_reusable = False  # genuinely not found anywhere (e.g. no configured host has it)
            except RuntimeError as e:
                keep_backend_error = e

        if keep_reusable:
            lc.log("  Skipping \"{}{}{}\" — existing VM matches definition".format(lc._RED, vm_name, lc._RESET))
            _report.add_node(vm_name, "reused")
            return

        if keep_backend_error is not None:
            msg = "--keep check for '{}' could not reach its backend (leaving it untouched, " \
                  "continuing with the remaining nodes): {}".format(vm_name, keep_backend_error)
            lc.error(msg)
            _report.add_node(vm_name, "FAILED")
            _report.add_error(msg)
            return

        lc.purge_known_host(vm_name)
        # bash's `destroy_vm.sh "${inputFile}" "${_vm_name}"` here has no `||`
        # error check — a failed/no-op destroy (e.g. the VM never existed on
        # a first run) must NOT stop the pipeline. Mirror that explicitly,
        # since the python destroy_vm() raises on real ssh/virsh failures.
        try:
            destroy_vm(definition, config, defaults, vm_name)
        except SystemExit:
            # destroy_vm() called die() — a refusal ("existing" node) or a
            # nothing-to-do no-op on a first run. Not an error, don't record it.
            pass
        except RuntimeError as e:
            # A genuine command/API failure in the pre-recreate destroy (e.g.
            # expired cloud credentials) — real enough to flag as an ERROR and
            # fail the run's exit code, even though the remaining nodes still
            # get their turn.
            msg = "destroy before recreate failed for '{}' (continuing): {}".format(vm_name, e)
            lc.error(msg)
            _report.add_error(msg)

        # A boot-wait timeout on one node is logged as an error for that node, and the run continues with the other nodes.
        # Like destroy_vm() above, the error handling logs and moves on.
        try:
            provision_vm(definition, config, defaults, vm_name)
            _report.add_node(vm_name, "created")
        except SystemExit:
            msg = "provisioning '{}' failed (continuing with the remaining nodes)".format(vm_name)
            lc.error(msg)
            _report.add_node(vm_name, "FAILED")
            _report.add_error(msg)
        except RuntimeError as e:
            msg = "provisioning '{}' failed (continuing with the remaining nodes): {}".format(vm_name, e)
            lc.error(msg)
            _report.add_node(vm_name, "FAILED")
            _report.add_error(msg)
    finally:
        if log_prefix is not None:
            lc.set_log_prefix(None)


def phase_create_vms(definition, config, defaults, json_file, keep, parallel=0):
    """
    Create the VMs for all nodes.

    parallel=0, the default, creates one node at a time, in the order of the JSON file.

    parallel=N runs up to N nodes at once, through a thread pool. These are I/O-bound SSH and subprocess calls, so threads are enough. The
    shared state that the workers touch is protected. DNS zone-file writes use libs/services.py's _dns_lock, and MAC generation and conflict
    resolution use libs/backends.py's _mac_lock. Each lock is held for the whole critical section. Each worker has its own log prefix (see
    _create_one_vm()).

    The result list is not locked. list.append is atomic under the GIL, so no lock is needed there.
    """
    lc.log("Creating VMs")
    lc._level += 1
    nodes = list(definition.get("nodes", {}).items())

    if not parallel:
        for vm_name, node_cfg in nodes:
            _create_one_vm(definition, config, defaults, json_file, keep, vm_name, node_cfg)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [
                pool.submit(_create_one_vm, definition, config, defaults, json_file, keep,
                            vm_name, node_cfg, log_prefix="[{}] ".format(vm_name))
                for vm_name, node_cfg in nodes
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()  # re-raise anything _create_one_vm's own try/except didn't already catch

    lc._level -= 1


def phase_reboot_and_wait_kept_nodes(definition, config, keep):
    # bash gates both of these loops on the GLOBAL --keep flag alone (not on
    # whether any given node was actually reused vs. recreated in phase 2) —
    # matched literally here, even though it means a just-recreated node
    # (which setup_vm.py already rebooted once) gets rebooted again.
    if not keep:
        return

    lc.log("Rebooting kept cluster nodes")
    lc._level += 1
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if not node_cfg.get("kcluster"):
            continue
        if targets.is_existing_node(node_cfg):
            # Not a VM this tool owns — nothing to reboot via libvirt.
            continue
        lc.log("Restart node {}{}{} (cluster {}{}{})".format(
            lc._RED, vm_name, lc._RESET, lc._RED, node_cfg["kcluster"], lc._RESET))
        # These are existing (kept) VMs — locate_kvm_host(), not resolve_kvm_host().
        remote_host, virt_srv = lc.locate_kvm_host(definition, vm_name, config)
        lc.reboot_vm(virt_srv, vm_name, remote_host=remote_host)
    lc._level -= 1

    time.sleep(5)
    lc.log("Waiting for cluster nodes to come back online")
    lc._level += 1
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if not node_cfg.get("kcluster"):
            continue
        lc.check_ssh_conn(vm_name)
    lc._level -= 1


def _install_k8s_on_cluster(definition, clu_name, clu_type, clu_cfg):
    lc.log("Installing \"{}{}{}\" cluster \"{}{}{}\"".format(
        lc._RED, clu_type, lc._RESET, lc._RED, clu_name, lc._RESET))
    lc._level += 1
    distro = k8s.get_distro(clu_type)
    token = None
    rancher1_ip = None
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("kcluster") != clu_name:
            continue
        # Support both KUBERNETES_NODE_TYPE (new) and INSTALL_RKE2_TYPE (deprecated)
        node_type = node_cfg.get("KUBERNETES_NODE_TYPE") or node_cfg.get("INSTALL_RKE2_TYPE", "server")
        if node_type == "agent":
            token, rancher1_ip = distro.install_agent(vm_name, clu_name, clu_cfg, token, rancher1_ip)
        else:
            token, rancher1_ip = distro.install_server(vm_name, clu_name, clu_cfg, token=token, rancher1_ip=rancher1_ip)
    lc._level -= 1


def _install_cluster_addons(definition, config, defaults, json_file, clu_name, clu_cfg):
    # The cluster addons list is read from the definition, not from clu_cfg. load_kclu_vars() keeps only scalar fields, so a list is
    # dropped there. Per-node addons use phase_vm_addons() instead.
    addons = (definition.get("kclusters", {}).get(clu_name, {}) or {}).get("addons", []) or []
    if not addons:
        lc.log("No Kubernetes cluster addons for \"{}{}{}\"".format(lc._RED, clu_name, lc._RESET))
        return

    mgm_node = clu_cfg.get("mgm_node", "")
    vm_name = mgm_node
    if not vm_name:
        for name, node_cfg in definition.get("nodes", {}).items():
            if node_cfg.get("kcluster") == clu_name:
                vm_name = name
                break

    clu_type = clu_cfg.get("clu_type", "")

    lc.log("Installing cluster \"{}{}{}\" addon/s ( {} ) from \"{}{}{}\"".format(
        lc._RED, clu_name, lc._RESET, " ".join(addons), lc._RED, vm_name, lc._RESET))
    lc._level += 1
    installed = set()
    for addon in addons:
        if addon in installed:
            continue
        installer = shutil.which("install_{}".format(addon))
        if not installer:
            lc.die("FAILED! Addon script \"install_{}\" not found".format(addon))
        apps.check_requirements(apps.load_plugin(addon), targets.TARGET_CONTAINER, clu_type=clu_type)
        lc.log("Running addon \"{}{}{}\" on \"{}{}{}\" for cluster \"{}{}{}\"".format(
            lc._RED, addon, lc._RESET, lc._RED, vm_name, lc._RESET, lc._RED, clu_name, lc._RESET))
        env = dict(os.environ)
        env["_vm_name"] = vm_name
        env["clu_name"] = clu_name
        # bash never checks this call's exit code (no `||` on either addon
        # invocation in setup_lab.sh) — a failing addon does not stop the
        # pipeline. Matched exactly: run it, move on — but record the exit
        # code so the end-of-run summary can flag an addon that failed.
        r = _run_addon([installer, json_file], env)
        installed.add(addon)
        rc = getattr(r, "returncode", 0)
        if rc == 0:
            _report.add_addon("cluster:{}".format(clu_name), addon, "ok")
        else:
            _report.add_addon("cluster:{}".format(clu_name), addon, "FAILED (exit {})".format(rc))
            msg = "cluster addon '{}' on '{}' exited {}".format(addon, clu_name, rc)
            lc.error(msg)
            _report.add_error(msg)
        lc.log("Installed addon \"{}{}{}\" on cluster \"{}{}{}\"".format(
            lc._RED, addon, lc._RESET, lc._RED, clu_name, lc._RESET))
    lc._level -= 1
    # bash prints its "No more addons" message from inside the per-addon loop
    # (bash:209), so it fires after every addon rather than once at the end —
    # clearly a misplaced statement, not intentional per-addon behaviour.
    # Fixed here to print once, after all of this cluster's addons are done.
    lc.log("No more addons for cluster \"{}{}{}\"".format(lc._RED, clu_name, lc._RESET))


def phase_install_k8s_and_addons(definition, config, defaults, json_file):
    delay_min = int(definition.get("common", {}).get("delay_min", defaults.get("delay_min", 2)))
    for clu_name in k8s.list_kclusters(definition):
        clu_cfg = k8s.load_kclu_vars(definition, clu_name)
        clu_type = clu_cfg.get("clu_type", "")

        # Same resilience as phase_create_vms' per-node handling: one cluster's
        # Kubernetes install blowing up should be recorded and skipped, not take
        # the whole run (and its end-of-run summary) down with it.
        try:
            _install_k8s_on_cluster(definition, clu_name, clu_type, clu_cfg)
            _report.add_cluster(clu_name, "ok")
        except (SystemExit, RuntimeError) as e:
            msg = "Kubernetes install for cluster '{}' failed (skipping its addons): {}".format(clu_name, e)
            lc.error(msg)
            _report.add_cluster(clu_name, "FAILED")
            _report.add_error(msg)
            continue

        total_wait = 2 + delay_min
        lc.log("Wait {} min for cluster \"{}{}{}\" to stabilise".format(total_wait, lc._RED, clu_name, lc._RESET))
        time.sleep(60 * total_wait)

        _install_cluster_addons(definition, config, defaults, json_file, clu_name, clu_cfg)


def phase_vm_addons(definition, json_file):
    # A node whose VM creation failed has no host, so its addons are skipped. Each addon would otherwise fail with a misleading error.
    # The creation failure is already listed in the Errors section of the summary.
    failed_nodes = {name for name, status in _report.nodes if status == "FAILED"}
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        addons = node_cfg.get("addons", [])
        if not addons:
            continue
        if vm_name in failed_nodes:
            lc.log("Skipping \"{}{}{}\" addons — its own VM creation failed (see the Errors above)".format(
                lc._RED, vm_name, lc._RESET))
            continue
        lc.log("Installing VM \"{}{}{}\" addons".format(lc._RED, vm_name, lc._RESET))
        lc._level += 1
        node_target = targets.node_kind(definition, vm_name)
        for addon_entry in addons:
            # addon_entry is a plain "<addon>" string, or a single-key
            # {"<addon>": {...}} mapping carrying this node's own override of
            # that addon's config (read by the addon script itself, via
            # k8s.addon_node_config() — this loop only needs the name).
            addon = apps.addon_entry_name(addon_entry)
            installer = shutil.which("install_{}".format(addon))
            if not installer:
                lc.die("Addon script \"install_{}\" not found".format(addon))
            apps.check_requirements(apps.load_plugin(addon), node_target)
            lc.log("Running addon \"{}{}{}\" on \"{}{}{}\"".format(
                lc._RED, addon, lc._RESET, lc._RED, vm_name, lc._RESET))
            env = dict(os.environ)
            env["_vm_name"] = vm_name
            # Same as the cluster-addon loop: bash never checks this call's
            # exit code either, so a failing addon must not stop the pipeline —
            # but record it for the end-of-run summary.
            r = _run_addon([installer, json_file], env)
            rc = getattr(r, "returncode", 0)
            if rc == 0:
                _report.add_addon("node:{}".format(vm_name), addon, "ok")
            else:
                _report.add_addon("node:{}".format(vm_name), addon, "FAILED (exit {})".format(rc))
                msg = "node addon '{}' on '{}' exited {}".format(addon, vm_name, rc)
                lc.error(msg)
                _report.add_error(msg)
        lc._level -= 1


def setup_lab(definition, config, defaults, json_file, keep=False, fresh=True, parallel=0):
    # fresh=False is used by main(), which builds _report itself and seeds it
    # with the preflight's warnings/errors before this runs.
    global _report
    if fresh:
        _report = _RunReport()
    if _report.resources is None:
        _report.resources = _lab_resources(definition)

    lab_name = definition.get("common", {}).get("lab_name") or Path(json_file).name
    has_k8s = bool(definition.get("kclusters"))
    kind = "VMs + Kubernetes clusters" if has_k8s else "VMs only"
    lc.log("\nSetup lab \"{}{}{}\" ({})".format(lc._RED, lab_name, lc._RESET, kind))

    remote_dns_servers = config.get("REMOTE_DNS_SERVERS", "").split() or None

    phase_services(definition, config, defaults)

    if has_k8s:
        phase_dns(definition, remote_dns_servers)

    phase_create_vms(definition, config, defaults, json_file, keep, parallel=parallel)

    if has_k8s:
        phase_reboot_and_wait_kept_nodes(definition, config, keep)
        phase_install_k8s_and_addons(definition, config, defaults, json_file)

    phase_vm_addons(definition, json_file)

    lc.log("LAB setup completed")
    print_summary()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _fold_issues_into_report(lines):
    """Sort raw preflight / addon-validate issue lines into _report.warnings
    and _report.errors so print_summary() can re-surface them at the end."""
    for raw in lines:
        s = _ANSI_RE.sub("", raw).strip()
        if not s:
            continue
        if "[ERROR]" in s:
            _report.add_error(s.replace("[ERROR]", "").strip())
        elif "[WARN]" in s:
            _report.add_warning(s.replace("[WARN]", "").strip())
        else:
            # e.g. an addon --validate continuation line with no tag of its own
            _report.add_error(s)


def main():
    global _report
    args = sys.argv[1:]

    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if args and args[0] == "--help":
        print(_HELP_TEXT)
        sys.exit(0)

    if args and args[0] in ("--input-definition", "--schema"):
        fmt = args[1] if len(args) > 1 else "json"
        sys.exit(subprocess.run(["lab_schema", "--base", fmt]).returncode)

    keep = "--keep" in args
    debug = "--debug" in args
    lc.set_debug(debug)

    # --parallel / --parallel=N: bare form defaults to 4 concurrent workers.
    # 0 (no flag at all) means the original strictly-sequential behavior —
    # see phase_create_vms()'s own docstring for exactly what this does and
    # doesn't parallelize.
    parallel = 0
    for a in args:
        if a == "--parallel":
            parallel = 4
        elif a.startswith("--parallel="):
            try:
                parallel = int(a.split("=", 1)[1])
            except ValueError:
                lc.die("--parallel=N: '{}' is not a valid integer".format(a.split("=", 1)[1]))
            if parallel < 1:
                lc.die("--parallel=N: N must be at least 1 (got {})".format(parallel))

    positional = [a for a in args if a != "--keep" and a != "--debug" and a != "--parallel"
                  and not a.startswith("--parallel=")]
    if not positional:
        lc.die("Usage: setup_lab.py [--keep] [--debug] [--parallel[=N]] <lab.json>")
    json_file = positional[0]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    iso_loc        = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")
    vm_img_loc     = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")

    # One report for the whole run, seeded here so the preflight's own
    # warnings/errors (including the VM_DSK auto-raise warning) show up in the
    # end-of-run summary alongside anything that fails during provisioning.
    _report = _RunReport()
    _report.resources = _lab_resources(definition)

    # Resource total up front, too — same numbers, shown before the run starts.
    cpu, mem, disk, ncount = _report.resources
    lc.log("This lab needs {} vCPU, {} MiB RAM, {} GiB disk in total across {} node(s)".format(
        cpu, mem, disk, ncount))

    phase_fetch_images(definition, config, iso_loc)

    preflight_issues = []
    ok = lc.validate_lab_definition(definition, config, iso_loc, lab_setup_path,
                                    vm_img_loc=vm_img_loc, issues_out=preflight_issues)
    _fold_issues_into_report(preflight_issues)
    if not ok:
        print_summary()
        sys.exit(1)

    addon_issues = []
    if not validate_addon_configs(definition, json_file, issues_out=addon_issues):
        _fold_issues_into_report(addon_issues)
        print_summary()
        sys.exit(1)

    setup_lab(definition, config, defaults, json_file, keep=keep, fresh=False, parallel=parallel)

    # Non-zero exit if any node / cluster / addon failed (or the preflight
    # raised an error), so a scripted caller (or a glance at $?) can tell a
    # clean run from one that needs a fix — the human-readable breakdown is
    # print_summary()'s "Lab summary" block above.
    sys.exit(1 if _report.failed else 0)


if __name__ == "__main__":
    main()
