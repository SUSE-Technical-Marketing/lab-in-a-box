#!/usr/bin/env python3
# Part of lab-in-a-box — DNAT port-forwarding on the KVM hypervisor, for VMs
# (the automation VM included) attached to a NAT'd libvirt network instead
# of a real bridge (see setup_demo_server/lab.cfg.template's _network_mode).
# Author/s: Raul Mahiques
# License: GPLv3
#
# This is the shared, pure-function-plus-thin-apply core two callers use:
#   - setup_demo_server/setup_lab_automation.sh's configure_nat_port_forwarding(),
#     which runs ON the hypervisor at bootstrap time, forwarding the
#     automation VM's own ports (lab.cfg's _nat_forwarded_ports).
#   - libs/services.py's PortForwardService, which runs FROM the automation
#     VM and forwards each lab node's own "forwarded_ports" JSON field —
#     over SSH to the hypervisor, since the hypervisor is the actual NAT
#     boundary with the real external IP; the automation VM itself never
#     has one to forward from.
#
# Two dedicated iptables chains (CHAIN_DNAT in the nat table's PREROUTING,
# CHAIN_FWD in the filter table's FORWARD) are flushed and rebuilt from
# scratch on every apply_forwarded_ports() call — idempotent by
# construction (safe to rerun after adding/removing a node's ports) and
# scoped so this never touches any existing rule outside those two
# chains, matching this project's existing "declarative resync" style
# (e.g. BIND zone files are rewritten fresh, not incrementally patched).

import re
import shlex
import subprocess

from lab_creation import die, log, ssh_run

CHAIN_DNAT = "LAB_PORTFWD"
CHAIN_FWD = "LAB_PORTFWD_FWD"

_PORT_SPEC_RE = re.compile(r"^\s*(\d{1,5}):(\d{1,5})/(tcp|udp)\s*$", re.IGNORECASE)


def parse_port_spec(spec):
    """
    Parse one "<external>:<internal>/<protocol>" entry (Docker-style), e.g.
    "8080:80/TCP" -> (8080, 80, "tcp"). Dies clearly on malformed input
    rather than silently skipping or guessing at it.
    """
    m = _PORT_SPEC_RE.match(spec)
    if not m:
        die("invalid forwarded-port entry '{}' — expected \"<external>:<internal>/<protocol>\", "
            "e.g. \"8080:80/TCP\"".format(spec))
    external, internal, proto = m.groups()
    return int(external), int(internal), proto.lower()


def build_dnat_rules(port_map):
    """
    Build the full, ordered list of iptables argv lists (each a plain list
    of strings, no shell involved yet) needed to forward every port in
    port_map — {target_ip: [spec, ...]} — through the two dedicated chains.
    Pure function, no I/O: testable without root, mirrors
    libs/services.py's _dnsmasq_conf()/_build_dnat_rules-style precedent.
    """
    rules = []
    for target_ip, specs in port_map.items():
        for spec in specs:
            external, internal, proto = parse_port_spec(spec)
            rules.append(["-t", "nat", "-A", CHAIN_DNAT, "-p", proto, "--dport", str(external),
                          "-j", "DNAT", "--to-destination", "{}:{}".format(target_ip, internal)])
            rules.append(["-A", CHAIN_FWD, "-p", proto, "-d", target_ip, "--dport", str(internal),
                          "-j", "ACCEPT"])
    return rules


def _iptables_runner(remote_host):
    """
    Returns a run(args) -> CompletedProcess callable: local subprocess.run
    when remote_host is None (the hypervisor-local case,
    setup_lab_automation.sh's own bootstrap-time call), SSH to remote_host
    otherwise (the automation-VM-calling-out-to-the-hypervisor case,
    PortForwardService). Mirrors run_libvirt_tool()'s local-vs-SSH split.
    """
    if remote_host is None:
        def run(args):
            # stdout=PIPE/stderr=PIPE/universal_newlines=True, not
            # capture_output=/text= (Python 3.7+ only) — see
            # lab_creation.py's process_template() for the identical fix and
            # why (this project's own containerized test suite runs
            # Python 3.6).
            return subprocess.run(["iptables"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True)
        return run

    def run(args):
        cmd = "iptables " + " ".join(shlex.quote(a) for a in args)
        return ssh_run(remote_host, cmd, check=False, capture=True)
    return run


def _ensure_chain(run, table, chain, hook_table_chain):
    """Create `chain` in `table` if missing, and hook it into hook_table_chain
    (e.g. "PREROUTING") if not already hooked — both idempotent."""
    exists = run(["-t", table, "-L", chain])
    if exists.returncode != 0:
        create = run(["-t", table, "-N", chain])
        if create.returncode != 0:
            die("failed to create iptables chain {}/{}: {}".format(table, chain, create.stderr))
    hooked = run(["-t", table, "-C", hook_table_chain, "-j", chain])
    if hooked.returncode != 0:
        hook = run(["-t", table, "-I", hook_table_chain, "-j", chain])
        if hook.returncode != 0:
            die("failed to hook iptables chain {}/{} into {}: {}".format(
                table, chain, hook_table_chain, hook.stderr))


def apply_forwarded_ports(port_map, remote_host=None):
    """
    Flush and rebuild CHAIN_DNAT/CHAIN_FWD from port_map — {target_ip:
    [spec, ...]} — applying the rules locally (remote_host=None) or over
    SSH to remote_host (the hypervisor, from the automation VM's side).
    Safe to call with an empty port_map (just leaves both chains empty).
    """
    run = _iptables_runner(remote_host)

    _ensure_chain(run, "nat", CHAIN_DNAT, "PREROUTING")
    _ensure_chain(run, "filter", CHAIN_FWD, "FORWARD")

    for table, chain in (("nat", CHAIN_DNAT), ("filter", CHAIN_FWD)):
        flush = run(["-t", table, "-F", chain])
        if flush.returncode != 0:
            die("failed to flush iptables chain {}/{}: {}".format(table, chain, flush.stderr))

    for args in build_dnat_rules(port_map):
        result = run(args)
        if result.returncode != 0:
            die("iptables {} failed: {}".format(" ".join(args), result.stderr))

    total = sum(len(specs) for specs in port_map.values())
    log("Applied {} forwarded port(s) across {} target(s)".format(total, len(port_map)))
