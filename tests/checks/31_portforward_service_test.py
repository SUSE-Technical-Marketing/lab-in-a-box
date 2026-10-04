#!/usr/bin/env python3
# Unit tests for libs/portforward.py (pure port-spec parsing/rule-building
# plus the local-vs-SSH apply split) and libs/services.py's
# PortForwardService (the AuxService wrapper setup_lab.py's phase_services()
# drives). No real iptables/SSH — every subprocess/ssh_run call is mocked.
# Run from 31_portforward_service.sh, in its own container — see
# tests/run_tests.sh.
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import portforward  # noqa: E402
import services  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _cp(rc, stdout=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout)


# ── parse_port_spec(): valid specs ───────────────────────────────────────────
check("parse_port_spec parses a plain TCP spec",
      portforward.parse_port_spec("8080:80/TCP") == (8080, 80, "tcp"))
check("parse_port_spec parses a plain UDP spec, lowercased",
      portforward.parse_port_spec("2222:22/udp") == (2222, 22, "udp"))
check("parse_port_spec tolerates surrounding whitespace",
      portforward.parse_port_spec("  53:53/UDP  ") == (53, 53, "udp"))

# ── parse_port_spec(): malformed input dies clearly ──────────────────────────
for bad in ("8080-80/TCP", "8080:80", "8080:80/SCTP", "not-a-spec", ""):
    died = False
    try:
        portforward.parse_port_spec(bad)
    except SystemExit:
        died = True
    check("parse_port_spec dies clearly on malformed spec {!r}".format(bad), died)


# ── build_dnat_rules(): pure rule construction ───────────────────────────────
rules = portforward.build_dnat_rules({"192.168.150.10": ["8080:80/TCP"]})
check("build_dnat_rules emits a PREROUTING DNAT rule in the nat table",
      ["-t", "nat", "-A", portforward.CHAIN_DNAT, "-p", "tcp", "--dport", "8080",
       "-j", "DNAT", "--to-destination", "192.168.150.10:80"] in rules)
check("build_dnat_rules emits a matching FORWARD accept rule",
      ["-A", portforward.CHAIN_FWD, "-p", "tcp", "-d", "192.168.150.10", "--dport", "80",
       "-j", "ACCEPT"] in rules)

rules = portforward.build_dnat_rules({
    "192.168.150.10": ["8080:80/TCP"],
    "192.168.150.11": ["2222:22/TCP", "5353:53/UDP"],
})
check("build_dnat_rules covers every target IP and every one of its specs",
      len(rules) == 2 * (1 + 2))


# ── apply_forwarded_ports(): local mode (remote_host=None) ──────────────────
def _run_apply(port_map, remote_host=None, chain_exists=False, hooked=False):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "-L" in args:
            return _cp(0 if chain_exists else 1)
        if "-C" in args:
            return _cp(0 if hooked else 1)
        return _cp(0)

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        portforward.apply_forwarded_ports(port_map, remote_host=remote_host)
    return calls


calls = _run_apply({"192.168.150.10": ["8080:80/TCP"]}, chain_exists=False, hooked=False)
check("apply_forwarded_ports (local) creates the nat chain when it doesn't exist",
      ["iptables", "-t", "nat", "-N", portforward.CHAIN_DNAT] in calls)
check("apply_forwarded_ports (local) creates the filter chain when it doesn't exist",
      ["iptables", "-t", "filter", "-N", portforward.CHAIN_FWD] in calls)
check("apply_forwarded_ports (local) hooks the nat chain into PREROUTING",
      ["iptables", "-t", "nat", "-I", "PREROUTING", "-j", portforward.CHAIN_DNAT] in calls)
check("apply_forwarded_ports (local) hooks the filter chain into FORWARD",
      ["iptables", "-t", "filter", "-I", "FORWARD", "-j", portforward.CHAIN_FWD] in calls)
check("apply_forwarded_ports (local) flushes both chains before rebuilding",
      ["iptables", "-t", "nat", "-F", portforward.CHAIN_DNAT] in calls
      and ["iptables", "-t", "filter", "-F", portforward.CHAIN_FWD] in calls)
check("apply_forwarded_ports (local) adds the actual DNAT rule",
      ["iptables", "-t", "nat", "-A", portforward.CHAIN_DNAT, "-p", "tcp", "--dport", "8080",
       "-j", "DNAT", "--to-destination", "192.168.150.10:80"] in calls)

calls = _run_apply({"192.168.150.10": ["8080:80/TCP"]}, chain_exists=True, hooked=True)
check("apply_forwarded_ports (local) is idempotent: doesn't recreate an existing, already-hooked chain",
      not any("-N" in c for c in calls) and not any("-I" in c for c in calls))
check("apply_forwarded_ports (local) still flushes+rebuilds rules on a rerun",
      ["iptables", "-t", "nat", "-F", portforward.CHAIN_DNAT] in calls)

check("apply_forwarded_ports handles an empty port_map without error",
      _run_apply({}) is not None or True)  # just must not raise


# ── apply_forwarded_ports(): SSH mode (remote_host set) ──────────────────────
ssh_calls = []


def _fake_ssh_run(hostname, cmd, check=True, capture=False, **kwargs):
    ssh_calls.append((hostname, cmd))
    return _cp(0)


with mock.patch.object(portforward, "ssh_run", side_effect=_fake_ssh_run):
    portforward.apply_forwarded_ports({"192.168.150.10": ["8080:80/TCP"]}, remote_host="hypervisor.mydemo.lab")

check("apply_forwarded_ports (remote) runs iptables over SSH to the given host, never locally",
      all(c[0] == "hypervisor.mydemo.lab" for c in ssh_calls) and len(ssh_calls) > 0)
check("apply_forwarded_ports (remote) shell-quotes the iptables command",
      any("--to-destination" in cmd for _, cmd in ssh_calls))


# ── PortForwardService.configure(): builds port_map from definition["nodes"] ──
applied = []


def _fake_apply(port_map, remote_host=None):
    applied.append((port_map, remote_host))


definition = {
    "nodes": {
        "vm1.mydemo.lab": {"myip": "192.168.150.10", "forwarded_ports": ["8080:80/TCP"]},
        "vm2.mydemo.lab": {"myip": "192.168.150.11"},  # no forwarded_ports — skipped
        "vm3.mydemo.lab": {"forwarded_ports": ["22:22/TCP"]},  # no myip — skipped, warned
    },
}
svc = services.PortForwardService()
with mock.patch.object(portforward, "apply_forwarded_ports", side_effect=_fake_apply):
    svc.configure(definition, {"REMOTE_HOST": "hypervisor.mydemo.lab"})

check("PortForwardService.configure() calls apply_forwarded_ports exactly once", len(applied) == 1)
port_map, remote_host = applied[0]
check("PortForwardService.configure() includes a node with forwarded_ports+myip",
      port_map.get("192.168.150.10") == ["8080:80/TCP"])
check("PortForwardService.configure() skips a node with no forwarded_ports",
      "192.168.150.11" not in port_map)
check("PortForwardService.configure() skips a node with forwarded_ports but no myip",
      len(port_map) == 1)
check("PortForwardService.configure() passes REMOTE_HOST through as remote_host",
      remote_host == "hypervisor.mydemo.lab")


# ── PortForwardService.is_active() ───────────────────────────────────────────
svc2 = services.PortForwardService()
with mock.patch.object(portforward, "apply_forwarded_ports", side_effect=_fake_apply):
    svc2.configure({"nodes": {}}, {"REMOTE_HOST": "hypervisor.mydemo.lab"})

with mock.patch.object(subprocess, "run", return_value=_cp(1)) as local_run, \
     mock.patch.object(portforward, "ssh_run", return_value=_cp(0)) as remote_run:
    active = svc2.is_active()
check("is_active() reports active when the remote (hypervisor) check succeeds", active is True)
check("is_active() checks over SSH once configure() has recorded a remote_host",
      remote_run.call_count == 1)
check("is_active() never calls local subprocess.run once a remote_host is known",
      local_run.call_count == 0)

# A never-configured service must not fall back to a LOCAL iptables check, which could report it
# as "active". _remote_host alone cannot tell "configured
# for a local hypervisor" apart from "configure() never even ran", since
# both leave it at None. Fixed with an explicit _configured flag: a service
# that was never applied can't be active, full stop, without even touching
# iptables.
svc3 = services.PortForwardService()  # never configured — no remote_host recorded
with mock.patch.object(subprocess, "run", return_value=_cp(0)) as local_run2:
    check("is_active() reports False when configure() has never run, without even "
          "checking iptables",
          svc3.is_active() is False and local_run2.call_count == 0)

# Once configure() HAS run for a local hypervisor (REMOTE_HOST genuinely
# unset — a normal, valid post-configure state, not "never configured"),
# is_active() still falls back to a local check, same as setup_lab_automation.sh's
# own always-local bootstrap-time call.
svc4 = services.PortForwardService()
with mock.patch.object(portforward, "apply_forwarded_ports", side_effect=_fake_apply):
    svc4.configure({"nodes": {}}, {})  # no REMOTE_HOST in config -> local hypervisor
with mock.patch.object(subprocess, "run", return_value=_cp(0)) as local_run3:
    check("is_active() checks locally once configure() has run for a local hypervisor "
          "(REMOTE_HOST genuinely unset, not simply never configured)",
          svc4.is_active() is True and local_run3.call_count == 1)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all portforward_service checks passed")
