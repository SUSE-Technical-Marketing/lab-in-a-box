#!/usr/bin/env python3
# Part of lab-in-a-box — VM/hypervisor backend abstraction.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/backends.py — hypervisor/VM-backend abstraction.

VMBackend is the interface a compute backend implements to create, destroy and manage the VMs a
lab definition describes. LibvirtBackend is the default and holds the libvirt/KVM logic. The flat
functions in lab_creation.py (create_vm, delete_vm, copy_vm_image, vm_is_reusable, reboot_vm,
copy_to_hypervisor, _host_resources) are thin wrappers that build a LibvirtBackend and delegate to
it, so existing callers keep working.

get_backend() is the factory callers use. It resolves the KVM host (resolve_kvm_host and
locate_kvm_host) and returns a ready VMBackend, so callers never handle host selection or
connection URIs. setup_vm.py and destroy_vm.py call resolve_kvm_host and locate_kvm_host directly.
"""

import base64
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import primary
from lab_creation import (
    _RED, _YELLOW, _RESET,
    _empty,
    log, die,
    ssh_run, run_libvirt_tool,
    resolve_install_type, setup_salt,
    resolve_kvm_host, locate_kvm_host,
    ensure_iso_install_tree,
)
import lab_creation as _lc


def _read_conflict_confirmation():
    """
    Reads the y/N answer for a MAC-conflict prompt from the controlling TTY.
    Factored out of _check_or_generate_mac() so tests can monkeypatch it
    without needing a real /dev/tty (matches this codebase's own convention
    of reassigning a module-level name to fake something un-mockable
    otherwise — see e.g. lc.subprocess.run in the test suite).
    """
    with open("/dev/tty") as tty:
        return tty.readline().strip()


def _parse_sku_table(raw, config_key):
    """
    Parses the optional config-file override of a cloud backend's fixed (name, cores, mem_gb)
    sizing table. Format: "name:cores:mem_gb,name:cores:mem_gb,..." (e.g. "t3.medium:2:4,t3.large:2:8").
    Returns None when `raw` is empty or unset, so the caller keeps its built-in table. Otherwise returns
    the parsed list of (name, cores, mem_gb) tuples. That list replaces the built-in table entirely: it
    is a full override, not a merge, so an entry added this way must repeat the entries still wanted.

    Dies with a message that names the config key and the malformed entry on any parse error, rather
    than falling back silently or failing later with a KeyError or ValueError inside _pick_*().
    """
    if not raw:
        return None
    table = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            die("{} entry '{}' is invalid — expected \"name:cores:mem_gb\" (e.g. "
                "\"t3.medium:2:4\")".format(config_key, entry))
        name, cores_s, mem_s = parts
        try:
            cores = int(cores_s)
            mem_gb = float(mem_s)
        except ValueError:
            die("{} entry '{}' has a non-numeric cores/mem_gb field — expected "
                "\"name:cores:mem_gb\" (e.g. \"t3.medium:2:4\")".format(config_key, entry))
        table.append((name, cores, mem_gb))
    if not table:
        die("{} is set but contains no valid entries".format(config_key))
    return table


def _poll_for_ip(fetch_fn, vm_name, timeout=180, interval=5):
    """
    Shared polling loop for a cloud backend's create_vm(): repeatedly calls fetch_fn() (a
    zero-arg closure, typically a bound get_ip(vm_name)) until it returns a real IP, or dies with
    a clear message once `timeout` seconds have passed. Plain time.sleep() polling, matching this
    codebase's own established convention (see check_ssh_conn() in lab_creation.py) — no
    cancellation token needed here, unlike rodeo-cli's unrelated convention some contributors may
    know from that other project.
    """
    waited = 0
    while waited < timeout:
        ip = fetch_fn()
        if ip:
            return ip
        time.sleep(interval)
        waited += interval
    die("timed out after {}s waiting for '{}' to be assigned a real IP address".format(timeout, vm_name))


def _cloud_no_mac(mymac):
    """
    Shared check_or_generate_mac() body for the cloud backends (Hetzner, AWS, GCP, Alibaba,
    Scaleway, UpCloud, OVHcloud, Exoscale). A cloud provider assigns networking itself, so there is no
    MAC address to check, generate, resolve or persist. The function returns the mymac value from the
    lab JSON, or an empty string, with no generation, no conflict check and no definition save. The
    second return value is None, matching HarvesterBackend's libvirt-only network. No cloud create_vm()
    reads its `network` parameter.
    """
    return mymac or "", None


# Serializes MAC generation and conflict resolution across threads. The check in
# _check_or_generate_mac() is not atomic, and an explicit mymac already claimed by
# another VM can prompt on the tty and mutate the shared definition. The lock is held
# for the whole call, so the decision and any resulting definition update form one
# critical section.
_mac_lock = threading.Lock()


def _check_or_generate_mac(mac_by_domain, vm_name, mymac, definition, bridge, vm_net_model):
    """
    Shared MAC generate/validate/conflict-resolution logic — every backend's
    check_or_generate_mac() calls this with its own {domain: mac} map from
    its own list_used_macs(), so this one implementation isn't duplicated
    per backend. Returns (mymac, network) — the resolved MAC and the
    libvirt-style NETWORK string built from it (bridge=.../mac.address=...
    /model=...; used as-is by LibvirtBackend, ignored by HarvesterBackend,
    which builds its own KubeVirt interface spec instead).

    `definition` is the lab definition, already loaded once by the caller
    (primary.load_definition()) — a LabDefinition, so it already knows its
    own source path and format (see primary.py). This function has no
    business knowing either: it mutates `definition` in place and, on a
    conflict resolved by regenerating the MAC, calls
    primary.save_definition(definition) — which figures out where and in
    what format to write entirely on its own. There is no re-reading of the
    source file anywhere in this function, and no input_file/format
    parameter to thread through for a caller to get wrong.

    Behaviour:
      - mymac empty                              → generate a random locally-
        administered MAC not already in use.
      - mymac set, not in use (or used by this same VM) → use as-is.
      - mymac set, already used by a DIFFERENT VM → prompt on the controlling
        TTY to regenerate; on 'y'/'Y' update definition's nodes.<vm_name>.mymac
        in memory and save it (primary.save_definition — a new sibling file,
        the original source is never overwritten), then continue; on
        anything else, die().
    """
    used_macs = set(mac_by_domain.values())

    if _empty(mymac):
        mymac = _lc._generate_unused_mac(used_macs)
        log("- No MAC specified for \"{}{}{}\" — generated {}".format(_RED, vm_name, _RESET, mymac))
    else:
        mymac_lower = mymac.lower()
        owner = next((dom for dom, mac in mac_by_domain.items() if mac == mymac_lower), None)

        if owner and owner != vm_name:
            old_mac = mymac
            print("{}WARNING:{} MAC {} is already used by VM '{}'.".format(_YELLOW, _RESET, old_mac, owner),
                  file=sys.stderr)
            print("  Generate a new MAC and update {}? [y/N] ".format(definition.source_path), end="", flush=True)
            answer = _read_conflict_confirmation()
            if re.match(r"^[Yy]$", answer):
                mymac = _lc._generate_unused_mac(used_macs)
                definition["nodes"][vm_name]["mymac"] = mymac
                output_path = primary.save_definition(definition)
                log("- MAC updated to {} for \"{}{}{}\" — '{}' left untouched; updated copy written "
                    "to '{}' (merge it back by hand to keep this MAC on the next run)".format(
                        mymac, _RED, vm_name, _RESET, definition.source_path, output_path))
            else:
                die("MAC conflict on \"{}{}{}\" ({} owned by '{}') — aborting".format(
                    _RED, vm_name, _RESET, old_mac, owner))
        else:
            log("- MAC {} is available for \"{}{}{}\"".format(mymac, _RED, vm_name, _RESET))

    network = "bridge={},mac.address={},model={}".format(bridge, mymac, vm_net_model or "virtio")
    return mymac, network


def _parse_k8s_cpu(value):
    """Parse a Kubernetes CPU quantity ("500m", "2") into millicores."""
    value = str(value).strip()
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def _parse_k8s_memory(value):
    """Parse a Kubernetes memory quantity ("512Mi", "2Gi", "1024Ki", "2G", bare bytes) into KiB."""
    value = str(value).strip()
    units = {"Ki": 1.0, "Mi": 1024.0, "Gi": 1024.0 ** 2, "Ti": 1024.0 ** 3,
             "K": 1000.0 / 1024, "M": (1000.0 ** 2) / 1024, "G": (1000.0 ** 3) / 1024}
    for suffix in sorted(units, key=len, reverse=True):
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * units[suffix])
    return int(float(value) / 1024)  # bare bytes


def parse_open_ports(ports):
    """[(port, protocol)] from a lab-JSON open_ports list ("443", "69/udp", 8080, ...)."""
    out = []
    for entry in ports or []:
        port_s, _, proto = str(entry).partition("/")
        out.append((int(port_s), (proto or "tcp").lower()))
    return out


def _gce_name(text):
    """A GCE resource name (lowercase letters, digits, '-', starting with a letter)."""
    name = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return ("lab-" + name)[:63].rstrip("-")


class VMBackend(object):
    """Interface every compute backend implements."""

    # Name of the cloud account (see resolve_cloud_account()) this instance was
    # built for, or "" for the single default account / a non-cloud backend.
    # Set by get_backend() after resolve(); read by ensure_cloud_dns_vm() so the
    # per-account DNS VM gets a per-account name.
    account = ""

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        """
        Resolve wherever this backend's compute target actually is (a KVM
        host for LibvirtBackend, a kubeconfig/cluster for HarvesterBackend)
        and return a ready instance — the one place backend-specific
        connection/target resolution lives, so get_backend() itself never
        needs to know how a given backend finds its target.
        """
        raise NotImplementedError

    def create_vm(self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network, **kwargs):
        """
        Create the VM. Cloud backends return the real, reachable IP address assigned to it, as a string, once the
        instance is running. They poll the provider's API or CLI when the create call does not return the address.
        LibvirtBackend and HarvesterBackend return None, because the caller already has a static `myip` from the lab
        JSON. setup_vm.py's provision_vm() uses a non-None return value in place of `myip` for DNS registration.
        """
        raise NotImplementedError

    def get_ip(self, vm_name):
        """
        Return the real IP address of the existing instance named vm_name, or None if it does not exist. Only cloud
        backends implement this. ensure_cloud_dns_vm() uses it to find an existing cloud DNS VM's address on a later run
        without recreating the VM. LibvirtBackend and HarvesterBackend raise NotImplementedError, because their addresses
        are the static `myip` from the lab JSON.
        """
        raise NotImplementedError

    def delete_vm(self, vm_name):
        raise NotImplementedError

    def vm_exists(self, vm_name):
        raise NotImplementedError

    def vm_is_reusable(self, vm_name, mymac, myip):
        raise NotImplementedError

    def reboot_vm(self, vm_name):
        raise NotImplementedError

    def vm_state(self, vm_name):
        """
        Power state of an existing VM, in the provider's own words (e.g. "running", "stopped"), or "not found".
        Backends that do not implement it raise NotImplementedError, and scripts/vm_power.py reports "unsupported".
        """
        raise NotImplementedError

    def start_vm(self, vm_name):
        """Power on an existing, stopped VM (see vm_state)."""
        raise NotImplementedError

    def stop_vm(self, vm_name):
        """Shut down an existing VM without deleting it (see vm_state)."""
        raise NotImplementedError

    def open_vm_ports(self, vm_name, ports):
        """
        Make `ports` (a lab-JSON open_ports list such as "443" or "69/udp"; tcp by default) reachable from anywhere
        on an existing VM. The operation is best effort and idempotent. The default does nothing, because libvirt has no
        firewall of its own and the host forwards ports. Cloud backends override it with their own firewall or
        security-group mechanism.
        """
        log("- open_ports: nothing to open on the '{}' backend".format(type(self).__name__))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        raise NotImplementedError

    def list_used_macs(self):
        raise NotImplementedError

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        raise NotImplementedError

    def host_resources(self):
        raise NotImplementedError

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        raise NotImplementedError

    def ensure_ports_open(self, open_ports):
        """
        Best effort: open `open_ports` (same shape as create_vm()'s open_ports argument, e.g. ["51820/udp"], tcp by
        default) for this account's compute, without creating a VM. Used when the overlay hub is an existing host
        (OVERLAY_HUB_HOST), which never goes through create_vm().

        No-op by default, like the other backends that ignore open_ports. Only AWSBackend overrides this, delegating to
        _ensure_security_group_access().
        """
        pass

    def get_private_ip(self, vm_name):
        """
        Return the private (internal) IP of the existing instance named vm_name, or None. The overlay's site-gateway
        routing uses it: another node in the same site reaches the gateway through this address, not the public IP.

        No-op (returns None) by default. Only AWSBackend overrides it.
        """
        return None

    def get_subnet_cidr(self):
        """
        Return this account's configured subnet CIDR (e.g. "172.31.0.0/20"), or None if the backend has no such concept
        or is not configured with one. A site gateway advertises this CIDR to the overlay hub as the routable subnet that
        other sites reach it through.

        No-op (returns None) by default. Only AWSBackend overrides it.
        """
        return None

    def disable_source_dest_check(self, vm_name):
        """
        Best effort: disable the instance's source/destination check. This is a cloud-provider packet filter that drops
        any packet not addressed to or from the instance's own IP, and it runs below the guest's net.ipv4.ip_forward. A
        gateway that forwards traffic for other nodes needs the check disabled, or the packets are dropped before they
        reach the guest kernel.

        No-op by default. Only AWSBackend overrides it, using EC2's SourceDestCheck attribute. No equivalent is implemented
        for the other cloud backends.
        """
        pass


class LibvirtBackend(VMBackend):
    """
    The default backend. Talks to a libvirt hypervisor over `virsh --connect <virt_srv>` and
    provisions files and images over SSH to `remote_host`. The constructor keeps virt_srv, remote_host,
    vm_img_loc, lab_setup_path and iso_loc as instance state.

    remote_host, iso_loc, vm_img_loc and lab_setup_path are optional. Operations that only need virt_srv
    (delete_vm, reboot_vm, vm_is_reusable, check_or_generate_mac, list_used_macs) do not require them.
    """

    def __init__(self, virt_srv, remote_host=None, iso_loc=None, vm_img_loc=None, lab_setup_path=None):
        self.virt_srv = virt_srv
        self.remote_host = remote_host
        self.iso_loc = iso_loc
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path

    def _virsh(self, *args, **kwargs):
        """See run_libvirt_tool()'s docstring: local virsh with --connect
        when available (unchanged, everywhere it already is), SSH to
        self.remote_host running against qemu:///system otherwise."""
        return run_libvirt_tool("virsh", self.remote_host, self.virt_srv, args, **kwargs)

    def _virt_install(self, *args, **kwargs):
        """Same fallback as _virsh(), for virt-install."""
        return run_libvirt_tool("virt-install", self.remote_host, self.virt_srv, args, **kwargs)

    def _virt_xml(self, *args, **kwargs):
        """Same fallback as _virsh(), for virt-xml (edits an already-defined domain's
        XML in place, as used by create_vm()'s autoinstall branch)."""
        return run_libvirt_tool("virt-xml", self.remote_host, self.virt_srv, args, **kwargs)

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        """
        for_existing=False (default) → placing a NEW VM: uses resolve_kvm_host()
        (resource-based selection across KVM_HOSTS, or the sole configured host).
        for_existing=True → an operation on an EXISTING VM (destroy/reboot/reuse
        check): uses locate_kvm_host() instead, which asks each host directly
        rather than re-running selection (see locate_kvm_host()'s docstring for
        why the two must never be conflated). Moved here verbatim from
        get_backend() — zero behavior change, just relocated to where a
        second backend's own resolve() can now live alongside it.
        """
        if for_existing:
            remote_host, virt_srv = locate_kvm_host(definition, vm_name, config)
        else:
            remote_host, virt_srv = resolve_kvm_host(definition, vm_name, config, vm_img_loc)
        return cls(virt_srv, remote_host=remote_host, iso_loc=iso_loc,
                   vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    # ── MAC / domain introspection (moved from _list_domain_macs) ──────────

    def list_used_macs(self):
        """
        Returns (all_domain_lines, mac_by_domain) — the raw `virsh list --all
        --name` output lines and a {domain: lowercased_mac} map built from
        each domain's first vnet interface.
        """
        domains = self._virsh(
            "list", "--all", "--name",
            capture_output=True, text=True,
        ).stdout.splitlines()
        domains = [d.strip() for d in domains if d.strip()]

        mac_by_domain = {}
        for dom in domains:
            domif = self._virsh(
                "domiflist", dom,
                capture_output=True, text=True,
            ).stdout
            for line in domif.splitlines():
                if not line[:1].isspace():
                    continue
                fields = line.split()
                if len(fields) >= 5 and re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", fields[4].lower()):
                    mac_by_domain[dom] = fields[4].lower()
                    break
        return domains, mac_by_domain

    def vm_exists(self, vm_name):
        """True when a domain by this name is defined on this backend's host."""
        result = self._virsh(
            "dominfo", vm_name,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        """Validate or generate the MAC for a VM — see _check_or_generate_mac()'s
        docstring (this backend's own list_used_macs() supplies the map of
        MACs already in use)."""
        with _mac_lock:
            _, mac_by_domain = self.list_used_macs()
            return _check_or_generate_mac(mac_by_domain, vm_name, mymac, definition, bridge, vm_net_model)

    def vm_is_reusable(self, vm_name, mymac, myip):
        """
        Returns True when the VM should be kept, False when it must be destroyed
        and recreated. Checks in order: running on hypervisor, MAC matches (only
        when mymac is set), DNS resolves to expected IP, SSH accessible with
        default credentials. Any failed check returns False (safe default =
        recreate).
        """
        # stdout=PIPE/stderr=PIPE/universal_newlines=True, not
        # capture_output=/text= (Python 3.7+ only) — see targets.py's
        # check_ssh_only_reachability() for the identical fix and why.
        state = self._virsh(
            "domstate", vm_name, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        ).stdout.strip()
        if state != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on hypervisor (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        if not _empty(mymac):
            _, mac_by_domain = self.list_used_macs()
            actual_mac = mac_by_domain.get(vm_name)
            if mymac.lower() != (actual_mac or "NOT_FOUND"):
                log("  {}KEEP CHECK{} \"{}{}{}\": MAC mismatch (want \"{}\", got \"{}\") — will recreate".format(
                    _YELLOW, _RESET, _RED, vm_name, _RESET, mymac, actual_mac or "none"))
                return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        """
        Reboot a VM. A guest-side reboot is preferred over virsh or ACPI, because in this nested-virt environment
        `virsh reset` can boot back into an older transactional-update snapshot, and ACPI signals often do not reach the
        guest in time.

        If the guest is reachable over SSH, the method runs `reboot` inside the guest and returns at once. The resulting
        broken pipe or connection reset is the expected outcome. Callers poll for the guest to come back with
        check_ssh_conn(). The virsh-mediated ACPI and reset sequence is used only when the guest is not reachable over SSH,
        since there is no other way to intervene in that case.
        """
        try:
            probe = socket.create_connection((vm_name, 22), timeout=3)
            probe.close()
            reachable = True
        except OSError:
            reachable = False

        if reachable:
            ssh_run(vm_name, "sync && reboot", check=False)
            return

        self._virsh("reboot", vm_name, check=False)
        event = self._virsh(
            "event", vm_name, "--event", "lifecycle", "--timeout", "120",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if event.returncode == 0:
            return

        log("- \"{}{}{}\" did not reboot — trying a graceful shutdown+start cycle".format(_RED, vm_name, _RESET))
        self._virsh("shutdown", vm_name, check=False)
        stopped = self._virsh(
            "event", vm_name, "--event", "lifecycle", "--timeout", "120",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        # stdout=PIPE/stderr=PIPE/universal_newlines=True, not
        # capture_output=/text= (Python 3.7+ only) — same fix as
        # vm_is_reusable() above.
        state = self._virsh(
            "domstate", vm_name,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
        )
        if stopped.returncode == 0 or "shut off" in (state.stdout or ""):
            self._virsh("start", vm_name, check=False)
            return

        log("- \"{}{}{}\" did not shut down cleanly either — forcing a hard power cycle "
            "(last resort; may lose a just-written transactional-update snapshot)".format(
                _RED, vm_name, _RESET))
        self._virsh("reset", vm_name, check=False)

    def delete_vm(self, vm_name):
        """
        Remove a VM and all its storage from the hypervisor, in two calls:

          1. destroy: force power-off if the domain is running. It does not remove the definition, so calling it on a
             stopped domain is harmless.
          2. undefine --nvram --remove-all-storage: deletes the disk images and the NVRAM/UEFI vars file, and removes the
             definition.

        The definition must still exist when step 2 runs. An undefine issued before destroy would remove the definition
        while the domain is running, and the later --remove-all-storage call would then fail with "domain not found" and
        leave the disk image behind.
        """
        log("Deleting VM '{}'".format(vm_name))
        self._virsh("destroy", vm_name,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self._virsh("undefine", vm_name, "--nvram", "--remove-all-storage",
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method="", disk_format="qcow2"):
        """
        Copy a QCOW2 source image and resize it on the hypervisor, landing
        it at the exact path create_vm()'s own disk_format expects
        (<vm_name>.qcow2, or <vm_name>.raw — see create_vm's docstring for
        why "raw" exists at all).

        install_iso: the disk is created empty by virt-install, so there's
        nothing to copy or resize.

        disk_format="raw": the source is genuinely QCOW2 content — a plain
        `cp` renamed to .raw would NOT be a valid raw disk (QCOW2 has its
        own container format/header), so this converts via `qemu-img
        convert -O raw`, not cp, when raw is requested. This is the one
        place the QCOW2->raw conversion cost is ever paid — once, here, at
        creation time — never later against an already-built multi-GB
        appliance image (see build_lab_usb.py / the plan's own reasoning
        for choosing raw from the start instead of converting at the end).
        """
        if config_method == "install_iso":
            log("- install_iso: skipping base image copy (disk created by virt-install)")
            return

        ext = "raw" if disk_format == "raw" else "qcow2"
        dest = "{}/{}.{}".format(self.vm_img_loc, vm_name, ext)
        # iso_image is the lab JSON's ISO_IMAGE (free text); dest embeds vm_name
        # (a node hostname); vm_dsk_gb comes from the JSON too — shell-quote all
        # of them so none can inject into the remote command string.
        src_q = shlex.quote("{}/{}".format(self.iso_loc, iso_image))
        dest_q = shlex.quote(dest)

        log("- Copy the image for the new VM \"{}{}{}\"".format(_RED, vm_name, _RESET))
        if disk_format == "raw":
            result = ssh_run(self.remote_host, "qemu-img convert -O raw {} {}".format(src_q, dest_q), check=False)
            if result.returncode != 0:
                die("Failed to convert image for vm \"{}\" to raw".format(vm_name))
        else:
            result = ssh_run(self.remote_host, "cp {} {}".format(src_q, dest_q), check=False)
            if result.returncode != 0:
                die("Failed to copy image for vm  \"{}\"".format(vm_name))

        log("- Resize to {}G".format(vm_dsk_gb))
        result = ssh_run(self.remote_host, "qemu-img resize -f {} {} {}".format(
            ext, dest_q, shlex.quote("{}G".format(vm_dsk_gb))), check=False)
        if result.returncode != 0:
            die("Failed to resize VM image \"{}\" to \"{}G\"".format(vm_name, vm_dsk_gb))

        if disk_format == "raw":
            # GPT keeps a backup header and partition table at the end of the disk. Growing a raw
            # image with qemu-img resize leaves that backup in the middle of the disk, so the raw
            # path relocates it. qcow2 disks are created at their final size and never grown, so
            # this applies to raw images only.
            log("- Repair GPT backup header/table after resize (raw disks only)")
            result = ssh_run(self.remote_host, "sgdisk -e {}".format(dest_q), check=False)
            if result.returncode != 0:
                die("Failed to repair GPT backup header on \"{}\" after resize".format(vm_name))

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        os_variant="slem5.4", boot="uefi", config_method="",  # boot: "uefi", "firmware=bios", "hd", …
        extra_disks=None, extra_filesystems=None, vm_dsk_bus="virtio",
        ign_file=None, com_file=None, salt_states="",
        install_type="", iso_image="", iso_loc="", mydns="",
        vcluster="", mymac=None,  # unused here — already embedded in `network` by check_or_generate_mac()
        disk_format="qcow2",  # "qcow2" (default, unchanged) or "raw" — see below
        vm_machine="",  # "" (default, unchanged) lets virt-install pick its own
                         # machine type (currently q35) — see below for why this
                         # ever needs overriding
        cloud_instance_type="",  # unused here — a cloud-backend-only override (see setup_vm.py's
                                  # own call site); accepted and ignored so setup_vm.py can pass
                                  # it unconditionally without needing to know which backend it's
                                  # talking to.
        open_ports=None,  # unused here — an AWS-only override (see AWSBackend.create_vm()'s own
                           # _ensure_security_group_access()); accepted and ignored for the same
                           # reason as cloud_instance_type above — this is the only backend
                           # without a trailing **kwargs, so it needs every cloud-only kwarg
                           # listed explicitly or setup_vm.py's unconditional call breaks it.
    ):
        """
        Create a VM on a KVM hypervisor with virt-install. Each config_method has its own branch:

            ""              → Ignition + Combustion (SLE Micro default)
            "install_iso"   → full OS install from an installer ISO: autoyast, kickstart or preseed (via --location and
                              --extra-args, blocks with --wait -1), or Ubuntu autoinstall (via --cdrom and a seed CDROM
                              built with mkisofs, also --wait -1)
            "iso-cloud-init"→ a stub: this branch computes an unused _boot_params value and creates no VM
            "virt_customize"→ the image is already configured by prepare_virt_customize_for_vm(); boot it directly
            "cloud-init"    → cloud-init ISO attached as a cdrom, then a 3-minute wait, optional salt state apply,
                              eject, reboot

        extra_disks entries look like "/dev/sdb,bus=scsi" or "UUID=xxx,bus=sata": a path or UUID= reference, with an
        optional per-disk bus override.

        vm_machine overrides the machine type. The default is q35, chosen by virt-install and libosinfo. Some old guests
        do not boot under q35: a 2015-era CentOS 7 GenericCloud image stops in a dracut emergency shell, because its
        virtio-blk root disk does not appear in time under the PCIe topology. Setting `--machine pc` (the legacy i440fx
        chipset) boots the same disk. The field is empty by default, so images that already boot are unchanged.
        """
        vm_img_loc = self.vm_img_loc
        remote_host = self.remote_host
        lab_setup_path = self.lab_setup_path

        log("Creating VM '{}'".format(vm_name))

        # Normalise boot flag: "uefi=off" / "bios" / "legacy" → "firmware=bios"
        _BIOS_ALIASES = {"uefi=off", "bios", "legacy"}
        boot_flag = "firmware=bios" if boot in _BIOS_ALIASES else boot

        extra_disk_args = []
        for dsk in (extra_disks or []):
            bus_match = re.search(r",bus=([a-z]+)", dsk)
            dsk_bus_override = bus_match.group(1) if bus_match else ""
            dsk_path = dsk.split(",")[0]
            if "UUID" in dsk_path:
                lookup = ssh_run(
                    remote_host,
                    "lsblk -o UUID,PATH | grep {} | cut -d' ' -f2".format(dsk_path.replace("UUID=", "")),
                    capture=True, check=False,
                )
                dsk_path = lookup.stdout.strip()
            extra_bus = dsk_bus_override or vm_dsk_bus or "virtio"
            extra_disk_args += ["--disk", "path={},bus={}".format(dsk_path, extra_bus)]

        extra_fs_args = []
        for fs in (extra_filesystems or []):
            extra_fs_args += ["--filesystem", fs]

        # disk_format="raw" is a deliberate, narrow exception to this
        # project's usual QCOW2-everywhere convention — used only for the
        # USB-delivery lab-host VM, whose own disk needs to be `dd`-able
        # directly onto a USB block device afterward (QCOW2's own container
        # format isn't). ".raw" filename + an explicit driver.type, same
        # dotted virt-install syntax already used for sparse=/boot.order=
        # above.
        disk_ext = "raw" if disk_format == "raw" else "qcow2"
        disk_type_arg = ",driver.type=raw" if disk_format == "raw" else ""
        base_args = [
            "--name", vm_name, "--autostart",
            "--boot", boot_flag, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
            "--os-variant", os_variant, "--import",
            "--disk", "size={},path={}/{}.{},sparse=no,bus={},boot.order=1{}".format(
                vm_dsk_gb, vm_img_loc, vm_name, disk_ext, vm_dsk_bus or "virtio", disk_type_arg),
            "--graphics", "spice,listen=0.0.0.0",
            "--network", network, "--noautoconsole",
        ]
        if vm_machine:
            base_args += ["--machine", vm_machine]

        if config_method == "":
            ign = ign_file or vm_name
            com = com_file or vm_name
            qemu_args = (
                "-fw_cfg name=opt/com.coreos/config,"
                "file={}/ignition/{} "
                "-fw_cfg name=opt/org.opensuse.combustion/script,"
                "file={}/combustion/{}".format(lab_setup_path, ign, lab_setup_path, com)
            )
            # "--qemu-commandline", qemu_args (two argv elements) makes
            # argparse (virt-install's CLI parser) treat qemu_args as a new
            # option rather than this one's value, since it starts with "-"
            # (-fw_cfg ...) — "expected one argument". The single
            # --qemu-commandline=<value> form (bash's own
            # libs/lab_creation.bash uses this exact form) avoids the
            # ambiguity entirely.
            r = self._virt_install(
                *(base_args + extra_fs_args + extra_disk_args + ["--qemu-commandline={}".format(qemu_args)]))
            if r.returncode != 0:
                die("virt-install failed for '{}'".format(vm_name))

        elif config_method == "install_iso":
            itype = resolve_install_type(install_type, iso_image)

            if itype == "autoinstall":
                # Ubuntu 22+ subiquity: boot from --cdrom + a second "cidata" seed
                # CDROM. --wait -1 blocks until the installer powers the VM off.
                seed_local = tempfile.mktemp(prefix="seed_{}_".format(vm_name), suffix=".iso")
                seed_remote = "{}/seed_{}.iso".format(vm_img_loc, vm_name)
                mkiso = subprocess.run([
                    "mkisofs", "-J", "-l", "-R", "-V", "cidata", "-iso-level", "3",
                    "-o", seed_local,
                    "{}/install_iso/{}/user-data".format(lab_setup_path, vm_name),
                    "{}/install_iso/{}/meta-data".format(lab_setup_path, vm_name),
                ])
                if mkiso.returncode != 0:
                    die("mkisofs seed failed for '{}'".format(vm_name))
                scp = subprocess.run([
                    "scp", "-o", "StrictHostKeyChecking=accept-new", seed_local,
                    "root@{}:{}".format(remote_host, seed_remote),
                ])
                os.unlink(seed_local)
                if scp.returncode != 0:
                    die("scp seed failed for '{}'".format(vm_name))

                # A bare --cdrom leaves the install ISO without boot priority, so the firmware falls
                # through to the empty disk. A domain-level --boot cdrom,hd device list sets the order.
                #
                # Subiquity (Ubuntu's installer) runs unattended only when 'autoinstall' is on the kernel
                # command line, regardless of the seed config. --location cannot inject it here:
                # --extra-args applies only to --location, and --location needs an install tree the
                # client can read, not a path on the remote hypervisor. The ISO's casper/vmlinuz and
                # initrd are therefore extracted on the hypervisor with xorriso and booted directly via
                # --boot kernel=,initrd=,cmdline=autoinstall. --cdrom stays attached because the
                # extracted initrd mounts it as the install source.
                vmlinuz_remote = "{}/{}_vmlinuz".format(vm_img_loc, vm_name)
                initrd_remote = "{}/{}_initrd".format(vm_img_loc, vm_name)
                extract = ssh_run(
                    remote_host,
                    "rm -f '{v}' '{i}' && xorriso -osirrox on -indev '{iso_loc}/{iso_image}' "
                    "-extract /casper/vmlinuz '{v}' -extract /casper/initrd '{i}'".format(
                        v=vmlinuz_remote, i=initrd_remote, iso_loc=iso_loc, iso_image=iso_image),
                    check=False)
                if extract.returncode != 0:
                    die("failed to extract installer kernel/initrd from '{}' for '{}'".format(
                        iso_image, vm_name))

                log("- Installing Ubuntu via autoinstall + seed CDROM (blocks until installer finishes)…")
                r = self._virt_install(
                    "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
                    "--os-variant", os_variant or "ubuntu24.04",
                    "--boot", "kernel={},initrd={},cmdline=autoinstall".format(vmlinuz_remote, initrd_remote),
                    "--cdrom", "{}/{}".format(iso_loc, iso_image),
                    "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={}".format(
                        vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
                    "--disk", "path={},device=cdrom,readonly=on".format(seed_remote),
                    *(extra_disk_args + [
                        "--graphics", "spice,listen=0.0.0.0",
                        "--network", network, "--noautoconsole", "--wait", "-1",
                    ]))
                ssh_run(remote_host, "rm -f '{}' '{}' '{}'".format(
                    seed_remote, vmlinuz_remote, initrd_remote), check=False)
                if r.returncode != 0:
                    die("virt-install (autoinstall) failed for '{}'".format(vm_name))

                # The direct kernel/initrd boot is valid only for the installer's first boot.
                # virt-install restarts the domain itself once the install finishes and reuses the same
                # kernel, initrd and cmdline, which would boot the installer again. Before the real start,
                # reset the domain to a plain disk boot: clear kernel, initrd and cmdline, set dev=hd, and
                # detach the now-stale seed cdrom. This runs on a stopped domain. --edit on a running domain
                # changes only the persistent definition, so the next start would boot the old config;
                # hence the explicit destroy first.
                self._virsh("destroy", vm_name, check=False)
                edit = self._virt_xml(vm_name, "--edit", "--boot", "kernel=,initrd=,cmdline=,hd", check=False)
                if edit.returncode != 0:
                    die("failed to reset '{}' to disk boot after autoinstall".format(vm_name))
                self._virt_xml(vm_name, "--remove-device", "--disk", "path={}".format(seed_remote), check=False)
                self._virsh("autostart", vm_name)
                self._virsh("start", vm_name)
                return

            # A bare hypervisor-local path here fails with "Cannot access install
            # tree on remote connection" whenever virt-install runs on a
            # different host than the hypervisor (this project's own default
            # architecture) — see ensure_iso_install_tree()'s own docstring for
            # the full explanation and why Ubuntu's --cdrom-based autoinstall
            # branch above never hit this. Serves the ISO over HTTP directly
            # from the hypervisor instead, which works the same way regardless
            # of where virt-install itself runs.
            location_arg = ensure_iso_install_tree(remote_host, iso_loc, iso_image)
            extra_args_by_type = {
                "autoyast": "autoyast=http://{}/lab_creation/install_iso/{}.xml".format(mydns, vm_name),
                # inst.text is a kernel command-line argument, separate from the kickstart's own 'text'
                # directive, which only selects the UI style. Without it RHEL 10's Anaconda starts its
                # graphical/WebUI path, which never completes under --noautoconsole with no display.
                # RHEL 8 and later need the argument because the kickstart 'text' line alone is not enough.
                "kickstart": "inst.ks=http://{}/lab_creation/install_iso/{}.ks inst.sshd inst.text".format(
                    mydns, vm_name),
                "preseed": "auto=true priority=critical url=http://{}/lab_creation/install_iso/{}.preseed".format(mydns, vm_name),
            }
            extra_args = extra_args_by_type[itype]

            log("- Installing via {} (this will block until the installer finishes)…".format(itype))
            r = self._virt_install(
                "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
                "--os-variant", os_variant,
                # This call builds its own argv, so it must pass --boot itself. Without it virt-install
                # falls back to its legacy-BIOS default, while the kickstart bootloader and the domain
                # firmware must agree with VM_BOOT (uefi by default).
                "--boot", boot_flag,
                "--location", location_arg,
                # Anaconda's text UI queries the terminal's capabilities at startup and blocks until the
                # reply arrives. With --noautoconsole nothing answers that query, so TERM=vt100 is set
                # to skip it.
                "--extra-args", "{} console=ttyS0,115200n8 TERM=vt100".format(extra_args),
                "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=1".format(
                    vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
                *(extra_disk_args + [
                    "--graphics", "spice,listen=0.0.0.0",
                    "--network", network, "--noautoconsole", "--wait", "-1",
                ]))
            if r.returncode != 0:
                die("virt-install (install_iso) failed for '{}'".format(vm_name))
            # Installer powered off the VM — bring it back up and mark autostart
            self._virsh("autostart", vm_name)
            self._virsh("start", vm_name)

        elif config_method == "iso-cloud-init":
            # Computes an unused _boot_params value (a Harvester config_url kernel arg) and never
            # calls virt-install. This is an incomplete stub, left as a no-op.
            if vcluster == "harvester":
                pass  # _boot_params = "harvester.install.config_url=http://10.100.0.10/harvester/config-create.yaml"

        elif config_method == "virt_customize":
            # Image already fully configured by prepare_virt_customize_for_vm() —
            # boot it directly, no provisioning kernel args, no extra cdrom.
            r = self._virt_install(*(base_args + extra_fs_args + extra_disk_args))
            if r.returncode != 0:
                die("virt-install failed for '{}'".format(vm_name))

        elif config_method == "cloud-init":
            ci_iso = "{}/{}_ci.iso".format(vm_img_loc, vm_name)
            r = self._virt_install(*(base_args + extra_fs_args + extra_disk_args +
                                      ["--disk", "{},device=cdrom".format(ci_iso)]))
            if r.returncode != 0:
                die("virt-install for cloud-init failed for '{}'".format(vm_name))

            log("  - Waiting 3 minutes")
            time.sleep(180)

            if salt_states:
                log("  - applying salt states")
                setup_salt(vm_name, salt_states, lab_setup_path)
                for state in salt_states.split():
                    subprocess.run(["salt-ssh", "-i", "-v", "--update-roster", vm_name, "state.apply", state])

            log("  - eject media")
            self._virsh("change-media", vm_name, "--eject", ci_iso, check=False)

            log("- reboot node")
            self._virsh("reboot", vm_name, check=False)

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """
        Copy the provisioning materials needed for the install to the
        hypervisor.

        config_method:
          "virt_customize" / "install_iso" → nothing to copy — both are already
            entirely hypervisor-side (virt-customize) or automation-VM-HTTP-side
            (install_iso answer files), same early return as bash.
          ""  (ignition+combustion, the default) → rsync the per-VM combustion
            file and ignition file, then chmod them world-readable.
          anything else (e.g. "cloud-init") → rsync the per-VM template_* output
            files, then build a NoCloud cidata ISO from them on the hypervisor.
        """
        remote_host = self.remote_host
        lab_setup_path = self.lab_setup_path
        vm_img_loc = vm_img_loc or self.vm_img_loc

        log("- Copy accross the lab setup materials")
        mkdir_test = ssh_run(remote_host, "[[ -d {0}/ ]] || mkdir -p {0}/".format(lab_setup_path), check=False)
        if mkdir_test.returncode != 0:
            die("failed creating new folder {}".format(lab_setup_path))

        if config_method in ("virt_customize", "install_iso"):
            return

        if config_method == "":
            r = ssh_run(remote_host, "mkdir -p {}/{{combustion,ignition}}".format(lab_setup_path), check=False)
            if r.returncode != 0:
                die("failed creating combustion/ignition folders on {}".format(remote_host))

            r = subprocess.run(["rsync", "-aqv",
                                 "{}/combustion/{}".format(lab_setup_path, vm_name),
                                 "root@{}:{}/combustion/".format(remote_host, lab_setup_path)])
            if r.returncode != 0:
                die("failed to rsync combustion file for '{}'".format(vm_name))

            r = subprocess.run(["rsync", "-aqv",
                                 "{}/ignition/{}.ign".format(lab_setup_path, vm_name),
                                 "root@{}:{}/ignition/".format(remote_host, lab_setup_path)])
            if r.returncode != 0:
                die("failed to rsync ignition file for '{}'".format(vm_name))

            r = ssh_run(remote_host, "chmod 0644 {0}/ignition/* {0}/combustion/*".format(lab_setup_path), check=False)
            if r.returncode != 0:
                die("failed to chmod ignition/combustion files on {}".format(remote_host))
        else:
            r = ssh_run(remote_host, "mkdir -p {}/{}".format(lab_setup_path, config_method), check=False)
            if r.returncode != 0:
                die("failed creating '{}' folder on {}".format(config_method, remote_host))

            # bash relied on an unquoted shell glob (${_vm_name}*) which bash itself
            # expands before invoking rsync — expand it the same way here.
            sources = sorted(str(p) for p in Path(lab_setup_path, config_method).glob("{}*".format(vm_name)))
            if not sources:
                die("no '{}' files found for '{}' in {}/{}".format(config_method, vm_name, lab_setup_path, config_method))
            r = subprocess.run(["rsync", "-aqv"] + sources +
                                ["root@{}:{}/{}/".format(remote_host, lab_setup_path, config_method)])
            if r.returncode != 0:
                die("failed to rsync '{}' files for '{}'".format(config_method, vm_name))

            # vm_name is interpolated unquoted into the 'for i in {vm}*' and '${i/{vm}_/}' patterns on
            # purpose: bash pattern expansion needs the unquoted glob. Every other use of vm_name here
            # is shlex.quote()'d, so a name with spaces or shell metacharacters cannot break or inject
            # into the remote command.
            ci_iso = shlex.quote("{}/{}_ci.iso".format(vm_img_loc, vm_name))
            tmp_iso = shlex.quote("/tmp/ci_{}.iso".format(vm_name))
            remote_cmd = (
                "cd {lsp_q}; "
                "for i in {vm}*; do cp \"${{i}}\" \"/tmp/${{i/{vm}_/}}\"; done ; "
                "rm -f {ci_iso}; "
                "mkisofs -J -l -R -V cidata -iso-level 3 -o {tmp_iso} "
                "/tmp/user-data /tmp/meta-data /tmp/network-config "
                "&& mv {tmp_iso} {ci_iso}"
            ).format(lsp_q=shlex.quote("{}/{}/".format(lab_setup_path, config_method)),
                      vm=vm_name, ci_iso=ci_iso, tmp_iso=tmp_iso)
            r = ssh_run(remote_host, remote_cmd, check=False)
            if r.returncode != 0:
                die("failed to build cidata ISO for '{}'".format(vm_name))

    def host_resources(self):
        """
        Return free vCPUs, free memory (MiB) and free disk (MiB) on self.vm_img_loc for this backend's host, over SSH.
        Raises RuntimeError or ValueError on any query failure. The caller, typically select_kvm_host, treats that host as
        disqualified instead of failing the whole selection.

        virsh runs locally on `host` (qemu:///system), not through self.virt_srv's qemu+ssh:// URI. The command already runs
        on that host over SSH, and a second SSH hop back to the same host would wait for an unaccepted host key when run
        unattended.
        """
        host = self.remote_host
        vm_img_loc = self.vm_img_loc
        total_cpus = int(_lc.ssh_output(host, "nproc"))

        running = [d for d in _lc.ssh_output(
            host, "virsh --connect qemu:///system list --name").splitlines() if d.strip()]
        used_cpus = 0
        for dom in running:
            used_cpus += int(_lc.ssh_output(
                host, "virsh --connect qemu:///system vcpucount --current {}".format(dom.strip())))
        free_cpu = max(total_cpus - used_cpus, 0)

        free_mem = int(_lc.ssh_output(host, "free -m | awk '/^Mem:/{print $7}'"))
        free_disk = int(re.sub(r"[^0-9]", "", _lc.ssh_output(
            host, "df -BM --output=avail {} | tail -1".format(vm_img_loc))))

        return free_cpu, free_mem, free_disk


class HarvesterBackend(VMBackend):
    """
    Provisions guest VMs on an existing, externally managed Harvester cluster instead of a
    KVM/libvirt hypervisor. This backend consumes a cluster that already exists, as LibvirtBackend
    consumes an existing KVM host. It is separate from scripts/install_harvester.py, which installs
    Harvester components inside an RKE2 or K3s cluster.

    Requirements and limits:
      - config_method="cloud-init" only. Ignition and Combustion have no KubeVirt equivalent.
      - Single cluster. The kubeconfig and namespace come from /etc/lab_creation.cfg
        (HARVESTER_KUBECONFIG and HARVESTER_NAMESPACE), not from per-node lab-JSON fields.
      - The cluster must share the bridge and L2 segment of the automation VM, so the existing DNS and
        BIND logic applies unchanged. This backend does not configure cluster networking.
      - copy_vm_image() does not import an image. The operator imports a VirtualMachineImage named after
        ISO_IMAGE beforehand. The backend exits with a clear error if it is missing, rather than creating
        a VM without a boot disk.
      - With HARVESTER_NETWORK set in /etc/lab_creation.cfg, create_vm() attaches the VM to an existing
        Multus NetworkAttachmentDefinition ("<namespace>/<name>", or a bare name in HARVESTER_NAMESPACE).
        The VM then gets a LAN-routable IP, which the DNS and SSH conventions need. The backend exits with a
        clear error if the NAD is missing and never creates one. The physical network (which NIC, which
        VLAN) is a cluster-level decision for the operator. Without HARVESTER_NETWORK the VM uses the pod
        network, and that address is not reachable through the DNS and SSH conventions.

    Resource shapes come from the current Kubernetes and Harvester CRDs. VirtualMachine is kubevirt.io/v1.
    VirtualMachineImage is harvesterhci.io/v1beta1. NetworkAttachmentDefinition is k8s.cni.cncf.io/v1,
    referenced from the VM's networks[] entry through multus.networkName as "<namespace>/<name>". The VM
    interface keeps the bridge binding for both pod and Multus networks. A DataVolume that boots from an
    existing image needs the harvesterhci.io/imageId annotation ("<namespace>/<image-name>") and the
    image's status.storageClassName, which Harvester generates per image ("longhorn-image-<suffix>").
    create_vm() reads that value from the VirtualMachineImage at run time. virtctl start, stop and
    restart control the VM lifecycle.
    """

    def __init__(self, kubeconfig, namespace="default", vm_img_loc=None, lab_setup_path=None,
                 network_attachment=None):
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        # <namespace>/<name> of an existing Multus NetworkAttachmentDefinition
        # (k8s.cni.cncf.io/v1) — see create_vm()'s docstring for why this backend
        # doesn't create one itself. None (the default) preserves the original
        # pod-network behavior — backward compatible, no config change required.
        self.network_attachment = network_attachment

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        kubeconfig = config.get("HARVESTER_KUBECONFIG")
        if not kubeconfig:
            die("backend 'harvester' requires HARVESTER_KUBECONFIG to be set in /etc/lab_creation.cfg "
                "(VM '{}')".format(vm_name))
        namespace = config.get("HARVESTER_NAMESPACE") or "default"
        network_attachment = config.get("HARVESTER_NETWORK") or None
        return cls(kubeconfig, namespace=namespace, vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path,
                   network_attachment=network_attachment)

    def _kubectl(self, *args, **kwargs):
        return subprocess.run(
            ["kubectl", "--kubeconfig", self.kubeconfig, "-n", self.namespace] + list(args), **kwargs)

    def _virtctl(self, *args, **kwargs):
        return subprocess.run(
            ["virtctl", "--kubeconfig", self.kubeconfig, "-n", self.namespace] + list(args), **kwargs)

    @staticmethod
    def _image_name(iso_image):
        """Derive a DNS-1123-safe VirtualMachineImage name from an ISO_IMAGE filename."""
        base = Path(iso_image or "").stem
        return re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "image"

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("HarvesterBackend only supports config_method=\"cloud-init\" (got '{}') for VM '{}'".format(
                config_method or "<empty>", vm_name))

    def vm_exists(self, vm_name):
        result = self._kubectl("get", "virtualmachine", vm_name,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0

    def list_used_macs(self):
        """Returns (vm_names, {vm_name: lowercased_mac}) from every VirtualMachineInstance's
        first interface with a MAC address."""
        result = self._kubectl("get", "vmi", "-o", "json", capture_output=True, text=True)
        if result.returncode != 0:
            return [], {}
        items = json.loads(result.stdout or "{}").get("items", [])
        names = []
        mac_by_name = {}
        for item in items:
            name = item.get("metadata", {}).get("name")
            if not name:
                continue
            names.append(name)
            interfaces = item.get("spec", {}).get("domain", {}).get("devices", {}).get("interfaces", []) or []
            for iface in interfaces:
                mac = iface.get("macAddress")
                if mac:
                    mac_by_name[name] = mac.lower()
                    break
        return names, mac_by_name

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        with _mac_lock:
            _, mac_by_name = self.list_used_macs()
            return _check_or_generate_mac(mac_by_name, vm_name, mymac, definition, bridge, vm_net_model)

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as LibvirtBackend's: True = keep, False = destroy and
        recreate. Checks the VirtualMachine's own printStatus (Running),
        then falls through to the same MAC/DNS/SSH checks."""
        result = self._kubectl("get", "virtualmachine", vm_name,
                                "-o", "jsonpath={.status.printableStatus}",
                                capture_output=True, text=True)
        status = (result.stdout or "").strip()
        if result.returncode != 0 or status != "Running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not Running on the Harvester cluster (status: {}) — "
                "will recreate".format(_YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        if not _empty(mymac):
            _, mac_by_name = self.list_used_macs()
            actual_mac = mac_by_name.get(vm_name)
            if mymac.lower() != (actual_mac or "NOT_FOUND"):
                log("  {}KEEP CHECK{} \"{}{}{}\": MAC mismatch (want \"{}\", got \"{}\") — will recreate".format(
                    _YELLOW, _RESET, _RED, vm_name, _RESET, mymac, actual_mac or "none"))
                return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        result = self._virtctl("restart", vm_name)
        if result.returncode != 0:
            die("virtctl restart failed for '{}'".format(vm_name))

    def delete_vm(self, vm_name):
        """Graceful virtctl stop first (short timeout), then kubectl delete —
        mirrors this project's existing graceful-then-forceful pattern
        (reboot_vm's SSH-first, ACPI-then-hard-reset escalation)."""
        log("Deleting VM '{}'".format(vm_name))
        self._virtctl("stop", vm_name, timeout=30)
        result = self._kubectl("delete", "virtualmachine", vm_name, "--ignore-not-found",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            die("kubectl delete virtualmachine failed for '{}'".format(vm_name))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        image_name = self._image_name(iso_image)
        result = self._kubectl("get", "virtualmachineimage", image_name,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            die("Harvester VirtualMachineImage '{}' not found in namespace '{}' — pre-import it "
                "before deploying VM '{}' (HarvesterBackend does not auto-import images)".format(
                    image_name, self.namespace, vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Applies a cloud-init Secret (userdata/networkdata) that create_vm()'s
        VirtualMachine manifest references via BOTH cloudInitNoCloud.secretRef
        (userdata) and cloudInitNoCloud.networkDataSecretRef (networkdata) —
        the cloud-init files themselves are generated the same
        backend-agnostic way as for LibvirtBackend (prepare_cloud_init()),
        just delivered differently."""
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        networkdata_path = base / "{}_network-config".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))

        secret_manifest = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "{}-cloudinit".format(vm_name), "namespace": self.namespace},
            "stringData": {
                "userdata": userdata_path.read_text(),
                "networkdata": networkdata_path.read_text() if networkdata_path.is_file() else "",
            },
        }
        result = self._kubectl("apply", "-f", "-", input=json.dumps(secret_manifest), text=True)
        if result.returncode != 0:
            die("failed to apply cloud-init Secret for '{}'".format(vm_name))

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        image_name = self._image_name(iso_image)

        image_result = self._kubectl("get", "virtualmachineimage", image_name,
                                      "-o", "json", capture_output=True, text=True)
        if image_result.returncode != 0:
            die("Harvester VirtualMachineImage '{}' not found for VM '{}'".format(image_name, vm_name))
        image_status = json.loads(image_result.stdout or "{}").get("status", {}) or {}
        storage_class = image_status.get("storageClassName")
        if not storage_class:
            die("VirtualMachineImage '{}' has no status.storageClassName yet — it may still be "
                "importing; wait for it to become Ready before deploying VM '{}'".format(
                    image_name, vm_name))

        if self.network_attachment:
            nad_namespace, _, nad_name = self.network_attachment.rpartition("/")
            nad_namespace = nad_namespace or self.namespace
            nad_result = self._kubectl("get", "network-attachment-definitions.k8s.cni.cncf.io", nad_name,
                                        "-n", nad_namespace,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if nad_result.returncode != 0:
                die("Harvester NetworkAttachmentDefinition '{}' not found — pre-create it (a VLAN "
                    "network, in Harvester's own terms) before deploying VM '{}' with "
                    "HARVESTER_NETWORK set (HarvesterBackend does not create one itself)".format(
                        self.network_attachment, vm_name))
            vm_network = {"name": "default", "multus": {"networkName": self.network_attachment}}
        else:
            vm_network = {"name": "default", "pod": {}}

        secret_name = "{}-cloudinit".format(vm_name)
        # "bridge" is the KubeVirt interface binding for both pod and Multus networks. Only the
        # networks[] entry above (pod or multus) decides which network the VM gets.
        interface = {"name": "default", "bridge": {}}
        if mymac:
            interface["macAddress"] = mymac

        vm_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {"name": vm_name, "namespace": self.namespace},
            "spec": {
                "running": True,
                "dataVolumeTemplates": [{
                    "metadata": {
                        "name": "{}-rootdisk".format(vm_name),
                        "annotations": {"harvesterhci.io/imageId": "{}/{}".format(self.namespace, image_name)},
                    },
                    "spec": {
                        "pvc": {
                            "accessModes": ["ReadWriteMany"],
                            "volumeMode": "Block",
                            "storageClassName": storage_class,
                            "resources": {"requests": {"storage": "{}Gi".format(vm_dsk_gb)}},
                        },
                        # A Harvester VirtualMachineImage import does not create a clonable PVC. The image is
                        # stored as a Longhorn BackingImage instead. The per-image storageClassName is backed by
                        # that BackingImage, so a source.blank PVC provisioned under it comes up pre-populated
                        # with the image content through Longhorn's CSI driver. source.pvc would need a PVC of the
                        # same name, which does not exist.
                        "source": {"blank": {}},
                    },
                }],
                "template": {
                    "metadata": {"labels": {"kubevirt.io/vm": vm_name}},
                    "spec": {
                        "domain": {
                            "cpu": {"cores": int(vm_cpu)},
                            # KubeVirt requires memory.guest or resources.limits.memory; requests alone are rejected.
                            # limits and requests are set to the same value, the memory the VM is sized for.
                            "resources": {"requests": {"memory": "{}Mi".format(vm_mem)},
                                          "limits": {"memory": "{}Mi".format(vm_mem)}},
                            "devices": {
                                "disks": [
                                    {"name": "rootdisk", "disk": {"bus": "virtio"}},
                                    {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                                ],
                                "interfaces": [interface],
                            },
                        },
                        "networks": [vm_network],
                        "volumes": [
                            {"name": "rootdisk", "dataVolume": {"name": "{}-rootdisk".format(vm_name)}},
                            # cloudInitNoCloud reads networkdata only from networkDataSecretRef, not from the
                            # 'networkdata' key inside secretRef's target. Both fields point at the same Secret, which
                            # push_provisioning_files() populates with both keys. Without networkDataSecretRef,
                            # cloud-init falls back to DHCP on every detected interface.
                            {"name": "cloudinitdisk", "cloudInitNoCloud": {
                                "secretRef": {"name": secret_name},
                                "networkDataSecretRef": {"name": secret_name},
                            }},
                        ],
                    },
                },
            },
        }

        log("Creating VM '{}' on Harvester".format(vm_name))
        result = self._kubectl("apply", "-f", "-", input=json.dumps(vm_manifest), text=True)
        if result.returncode != 0:
            die("kubectl apply failed for VirtualMachine '{}'".format(vm_name))

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking
        logic: allocatable capacity (kubectl get nodes) minus current usage
        (kubectl top nodes, needs metrics-server), summed across the
        cluster. free_disk_mb has no cluster-wide equivalent via kubectl
        alone — returns 0 (never disqualifies a Harvester target on disk)
        until a real cluster is available to wire up Harvester's own
        storage-capacity API instead.
        """
        nodes_result = self._kubectl("get", "nodes", "-o", "json", capture_output=True, text=True)
        if nodes_result.returncode != 0:
            raise RuntimeError("kubectl get nodes failed: {}".format(nodes_result.stderr))
        nodes = json.loads(nodes_result.stdout or "{}").get("items", [])

        total_cpu_m = 0
        total_mem_ki = 0
        for n in nodes:
            alloc = n.get("status", {}).get("allocatable", {})
            total_cpu_m += _parse_k8s_cpu(alloc.get("cpu", "0"))
            total_mem_ki += _parse_k8s_memory(alloc.get("memory", "0Ki"))

        used_cpu_m = 0
        used_mem_ki = 0
        top_result = self._kubectl("top", "nodes", "--no-headers", capture_output=True, text=True)
        if top_result.returncode == 0:
            for line in top_result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 5:
                    used_cpu_m += _parse_k8s_cpu(fields[1])
                    used_mem_ki += _parse_k8s_memory(fields[3])

        free_cpu = max((total_cpu_m - used_cpu_m) // 1000, 0)
        free_mem_mb = max((total_mem_ki - used_mem_ki) // 1024, 0)
        free_disk_mb = 0
        return free_cpu, free_mem_mb, free_disk_mb


class HetznerBackend(VMBackend):
    """
    Talks to the Hetzner Cloud API (https://api.hetzner.cloud/v1) directly over HTTPS from wherever
    this code runs. Hetzner's API is the hypervisor, so there is no separate host to SSH into or
    kubectl. Auth: a Hetzner Cloud API token (HETZNER_TOKEN in lab_creation.cfg), scoped to one Hetzner
    Cloud project. The project is the boundary where the VMs live, as HARVESTER_NAMESPACE is for
    Harvester. The backend uses the stdlib urllib, like the other HTTP backends.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this
    backend only consumes them:

      - config_method: only "cloud-init". Hetzner takes a `user_data` field at server creation, the same
        mechanism HarvesterBackend uses. Ignition and Combustion are not supported.
      - ISO_IMAGE: Hetzner has no upload for arbitrary qcow2 or ISO files. ISO_IMAGE is either a curated
        system image name (e.g. "ubuntu-24.04") or the numeric ID of a private snapshot the operator
        created beforehand. It is never a qcow2 filename. The backend does not create or upload images.
      - vm_cpu and vm_mem: Hetzner sells fixed (cpu, memory) server_type SKUs (cx22, cx32, cpx31, ...)
        and no custom sizes. create_vm() picks the smallest server_type whose cores and memory meet or
        exceed the request. No new per-node instance-type field is added.
      - vm_dsk_gb: each server_type has a fixed local disk bundled with it, and it cannot be resized at
        creation. The backend does not attach a separate Volume to make up the difference. It exits with a
        clear error if vm_dsk_gb is larger than the chosen server_type's disk.
      - MAC addresses: the Hetzner API has no customer-assigned MAC field. check_or_generate_mac() still
        uses the shared _check_or_generate_mac() helper, so a mymac value is resolved and saved to the lab
        definition like on every backend. The value is never sent to Hetzner or read back from it.
        list_used_macs() always returns an empty list.
    """

    API_BASE = "https://api.hetzner.cloud/v1"

    # Smallest-to-largest by (cores, memory_gb), covering the shared-vCPU lineup used for
    # typical lab-sized nodes. Used only when HETZNER_SERVER_TYPES is not set. The README's
    # Compute backends table gives the override format and the URL of Hetzner's current lineup.
    SERVER_TYPES = [
        ("cx22", 2, 4), ("cx32", 4, 8), ("cx42", 8, 16), ("cx52", 16, 32),
    ]

    def __init__(self, token, location=None, server_types=None, vm_img_loc=None, lab_setup_path=None):
        self.token = token
        self.location = location  # e.g. "nbg1"/"fsn1"/"hel1"/"ash"/"hil" — None lets Hetzner pick
        self.server_types = server_types  # HETZNER_SERVER_TYPES override, or None -> SERVER_TYPES
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}  # populated by push_provisioning_files(), read by create_vm()

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        token = config.get("HETZNER_TOKEN")
        if not token:
            die("backend 'hetzner' requires HETZNER_TOKEN to be set in /etc/lab_creation.cfg "
                "(VM '{}')".format(vm_name))
        location = config.get("HETZNER_LOCATION") or None
        # HETZNER_SERVER_TYPES: optional full override of the built-in SERVER_TYPES table, in the
        # form "name:cores:mem_gb,...", e.g. "cx22:2:4,cx32:4:8". See _parse_sku_table() for the
        # exact format and the README for Hetzner's current lineup.
        server_types = _parse_sku_table(config.get("HETZNER_SERVER_TYPES"), "HETZNER_SERVER_TYPES")
        return cls(token, location=location, server_types=server_types,
                   vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _api(self, method, path, body=None):
        """One JSON request against the Hetzner Cloud API. Returns the parsed response body
        (or None for a 204/empty body); raises RuntimeError with the real API error message on
        failure rather than a bare HTTPError, so callers' die() messages stay meaningful."""
        url = "{}{}".format(self.API_BASE, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer {}".format(self.token),
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("Hetzner API {} {} failed ({}): {}".format(method, path, e.code, detail))

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("HetznerBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_server(self, vm_name):
        result = self._api("GET", "/servers?name={}".format(vm_name))
        servers = (result or {}).get("servers", [])
        return servers[0] if servers else None

    def _pick_server_type(self, vm_cpu, vm_mem_mb, vm_dsk_gb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.server_types or self.SERVER_TYPES
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("HetznerBackend has no known server_type big enough for VM '{}' ({} vCPU / {} MiB) — "
            "extend HetznerBackend.SERVER_TYPES or set HETZNER_SERVER_TYPES in "
            "/etc/lab_creation.cfg".format(vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_server(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the public IPv4 address of the server named vm_name, from public_net.ipv4.ip. Returns None if the server
        does not exist, or has no public IPv4 assigned yet (an IPv4-less server, or one still initializing).
        """
        server = self._find_server(vm_name)
        if not server:
            return None
        return ((server.get("public_net") or {}).get("ipv4") or {}).get("ip") or None

    def list_used_macs(self):
        """Hetzner has no MAC-address concept at all — nothing to check a new one against."""
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)  # Hetzner assigns its own networking — see _cloud_no_mac()'s docstring

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as the other backends': True = keep, False = destroy and recreate. Checks
        the server's own status (running), then falls through to the same DNS/SSH checks — MAC is
        deliberately skipped (see this class's own docstring: Hetzner has no MAC concept)."""
        server = self._find_server(vm_name)
        status = (server or {}).get("status")
        if not server or status != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on Hetzner (status: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        server = self._find_server(vm_name)
        if not server:
            die("Hetzner server '{}' not found — cannot reboot".format(vm_name))
        try:
            self._api("POST", "/servers/{}/actions/reboot".format(server["id"]))
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        server = self._find_server(vm_name)
        return (server or {}).get("status", "unknown") if server else "not found"

    def start_vm(self, vm_name):
        self._server_action(vm_name, "poweron")

    def stop_vm(self, vm_name):
        self._server_action(vm_name, "shutdown")  # ACPI, like the other backends' graceful stop

    def _server_action(self, vm_name, action):
        server = self._find_server(vm_name)
        if not server:
            die("Hetzner server '{}' not found — cannot {}".format(vm_name, action))
        try:
            self._api("POST", "/servers/{}/actions/{}".format(server["id"], action))
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """Hetzner Cloud instances accept inbound traffic unless a firewall/security group
        you set up says otherwise — nothing for lab-in-a-box to open."""
        if ports:
            log("- open_ports: Hetzner Cloud lets inbound traffic in by default — nothing to "
                "open (add rules yourself if this project filters traffic)")

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        server = self._find_server(vm_name)
        if not server:
            return  # already gone — idempotent, matches every other backend's delete_vm()
        try:
            self._api("DELETE", "/servers/{}".format(server["id"]))
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        """No-op beyond validation — ISO_IMAGE is a Hetzner image NAME/ID here, not a file to
        copy anywhere (see this class's own docstring)."""
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'hetzner' backend — set it to a real "
                "Hetzner system-image name (e.g. \"ubuntu-24.04\") or your own snapshot ID".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Stashes this VM's already-generated cloud-init user-data for create_vm() to send at
        server-creation time — Hetzner has no separate "push after boot" step; user_data is a
        field on the create-server request itself, read by cloud-init on first boot same as any
        other cloud-init cloud (matches HarvesterBackend's own call-order assumption: this always
        runs before create_vm(), see setup_vm.py's provision_vm())."""
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._user_data_by_vm[vm_name] = userdata_path.read_text()

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_server_type() and uses
        # the given Hetzner server_type name verbatim, e.g. a type that is not in the built-in table
        # or a HETZNER_SERVER_TYPES override.
        server_type = cloud_instance_type or self._pick_server_type(vm_cpu, vm_mem, vm_dsk_gb, vm_name)

        body = {
            "name": vm_name,
            "server_type": server_type,
            "image": iso_image,
            "user_data": self._user_data_by_vm.get(vm_name, ""),
        }
        if self.location:
            body["location"] = self.location

        log("Creating VM '{}' on Hetzner Cloud (server_type={})".format(vm_name, server_type))
        try:
            result = self._api("POST", "/servers", body)
        except RuntimeError as e:
            die(str(e))
        server = (result or {}).get("server", {})
        disk_gb = server.get("server_type", {}).get("disk")
        if disk_gb and int(vm_dsk_gb) > int(disk_gb):
            log("  {}WARNING{}: requested {}G disk but server_type '{}' only includes {}G — "
                "HetznerBackend does not attach extra Volumes to make up the difference".format(
                    _YELLOW, _RESET, vm_dsk_gb, server_type, disk_gb))

        # The create-server response carries the new server's public IPv4 inline. get_ip() is a
        # short poll that runs only when that field is missing, for example while the IPv4 is still
        # being provisioned.
        ip = ((server.get("public_net") or {}).get("ipv4") or {}).get("ip") or None
        if not ip:
            log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
            ip = _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)
        return ip

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking logic. Hetzner has
        no "cluster capacity" concept the way libvirt/Harvester do — a Hetzner project's real
        constraint is its account-level server LIMIT, not CPU/RAM headroom (Hetzner's own capacity
        is effectively unlimited from a single lab's perspective). Returns a large constant instead
        of 0 so a "hetzner" node is never wrongly treated as out of capacity by multi-host
        selection logic that expects a real number here.
        """
        return 9999, 999999, 999999


class AWSBackend(VMBackend):
    """
    Talks to Amazon EC2 by running the `aws` CLI (`aws ec2 ...`). This follows the convention used
    for virsh and virt-install (LibvirtBackend) and kubectl and virtctl (HarvesterBackend). AWS request
    signing (SigV4) is impractical to implement by hand, so the CLI handles it.

    Auth: either AWS_PROFILE (a named profile from ~/.aws/config), or AWS_ACCESS_KEY_ID and
    AWS_SECRET_ACCESS_KEY. The keys are passed as environment variables to the aws subprocess only and are
    never written to disk. AWS_SESSION_TOKEN is optional, but required for temporary credentials such as
    STS or SSO credentials, whose access key ID starts with ASIA. AWS_REGION is always required.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend
    only consumes them:

      - config_method: only "cloud-init", passed as EC2's --user-data at launch.
      - ISO_IMAGE: must be an AMI ID (e.g. "ami-0123456789abcdef0") the operator can access. The backend
        does not build, import or copy images.
      - Networking: EC2 needs a subnet and security group to be reachable. AWS_SUBNET_ID and
        AWS_SECURITY_GROUP_ID are optional. When omitted, EC2 uses the account's default VPC and security
        group, which suits a quick lab but not a production setup. AWS_KEY_NAME (an existing EC2 key pair in
        the region) is optional. Cloud-init user-data is the access mechanism on every backend; the key
        pair is an extra access path.
      - vm_cpu and vm_mem: EC2 sells fixed (vCPU, memory) instance types. The backend picks the smallest
        sufficient one from INSTANCE_TYPES. vm_dsk_gb maps directly: the root EBS volume is sized at launch
        with --block-device-mappings, keyed off the AMI's root device name from describe-images.
      - EC2 instances have no native name field. The backend identifies a VM by the Name tag
        (aws ec2 describe-instances --filters Name=tag:Name,Values=<vm_name>).
      - MAC addresses: EC2 does not allow a custom MAC. check_or_generate_mac() and list_used_macs() are
        stubs, as in HetznerBackend.

    The CLI takes --count for run-instances. A subnet with MapPublicIpOnLaunch=false needs
    --associate-public-ip-address, or the instance has no reachable address.

    Nested virtualization is opt-in and off by default. The per-node or common lab-JSON field
    aws_nested_virtualization: "true" adds --cpu-options "NestedVirtualization=enabled" to run-instances.
    AWS documents this for running KVM inside a regular, non-bare-metal EC2 instance
    (docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html). The kickstart and
    combustion lab pipeline, including a Harvester HCI ISO install that needs real /dev/kvm, can then run on
    an EC2 "hypervisor" node. Nothing after create_vm() changes: once setup_kvm_node.py installs libvirt, the
    instance behaves like bare metal. The supported families are listed in _NESTED_VIRT_SUPPORTED_FAMILIES.
    The backend exits with a message that names a supported alternative when the resolved instance family is
    not in that list. Nested virtualization requires an explicit cloud_instance_type, because the default
    INSTANCE_TYPES table (T3, burstable) does not support it.
    """

    # Smallest-to-largest by (cores, memory_gb) — the standard burstable general-purpose family,
    # enough to cover this project's typical lab-sized nodes; extend as needed. Used only when
    # AWS_INSTANCE_TYPES isn't set — see resolve() and README's Compute backends table for the
    # override and the real URL to AWS's own current EC2 instance-type catalog.
    INSTANCE_TYPES = [
        ("t3.medium", 2, 4), ("t3.large", 2, 8), ("t3.xlarge", 4, 16), ("t3.2xlarge", 8, 32),
    ]

    # Instance families AWS documents as supporting nested virtualization, lowercased for
    # comparison. The list is taken from the "Considerations" section of the EC2 nested
    # virtualization user guide and covers only virtual (non-.metal) types. A .metal instance
    # already has direct hardware virtualization and needs no flag.
    _NESTED_VIRT_SUPPORTED_FAMILIES = {
        "m7i", "m7i-flex", "m8i", "m8id", "m8i-flex",
        "c7i", "c7i-flex", "c8i", "c8id", "c8i-flex",
        "r7i", "r7iz", "r8i", "r8id", "r8i-flex", "x8i",
        "i7i", "i7ie",
    }

    def __init__(self, region, profile=None, access_key=None, secret_key=None, session_token=None,
                 subnet_id=None, security_group_id=None, key_name=None, instance_types=None,
                 vm_img_loc=None, lab_setup_path=None):
        self.region = region
        self.profile = profile
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.subnet_id = subnet_id
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.instance_types = instance_types  # AWS_INSTANCE_TYPES override, or None -> INSTANCE_TYPES
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}  # populated by push_provisioning_files(), read by create_vm()
        self._cached_public_ip = None  # see _own_public_ip()
        self._instance_id_by_vm = {}  # populated by create_vm(), read by _find_instance()

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        region = config.get("AWS_REGION")
        if not region:
            die("backend 'aws' requires AWS_REGION to be set in /etc/lab_creation.cfg (VM '{}')".format(vm_name))
        profile = config.get("AWS_PROFILE")
        access_key = None if profile else config.get("AWS_ACCESS_KEY_ID")
        secret_key = None if profile else config.get("AWS_SECRET_ACCESS_KEY")
        session_token = None if profile else config.get("AWS_SESSION_TOKEN")
        # AWS_PROFILE takes precedence when set. resolve_cloud_account() merges by key, so
        # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN from /etc/lab_creation.cfg
        # would survive the merge. The aws CLI checks those variables before AWS_PROFILE, so they
        # are cleared here. A profile is an explicit auth choice and must not be overridden by raw
        # keys from another config layer.
        if not profile and not (access_key and secret_key):
            die("backend 'aws' requires either AWS_PROFILE, or both AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY, in /etc/lab_creation.cfg (VM '{}')".format(vm_name))
        if access_key and access_key.startswith("ASIA") and not session_token:
            die("backend 'aws': AWS_ACCESS_KEY_ID '{}' is a temporary/STS credential (starts with "
                "'ASIA') but no AWS_SESSION_TOKEN is set in /etc/lab_creation.cfg — it will be "
                "rejected without its paired session token (VM '{}')".format(access_key, vm_name))
        # AWS_INSTANCE_TYPES: optional full override of the built-in INSTANCE_TYPES table, in the
        # form "name:cores:mem_gb,...", e.g. "t3.medium:2:4,t3.large:2:8". See _parse_sku_table()
        # for the exact format and the README for AWS's current EC2 instance-type catalog.
        instance_types = _parse_sku_table(config.get("AWS_INSTANCE_TYPES"), "AWS_INSTANCE_TYPES")
        return cls(region, profile=profile, access_key=access_key, secret_key=secret_key,
                   session_token=session_token, instance_types=instance_types,
                   subnet_id=config.get("AWS_SUBNET_ID"), security_group_id=config.get("AWS_SECURITY_GROUP_ID"),
                   key_name=config.get("AWS_KEY_NAME"), vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _aws(self, *args, **kwargs):
        """One `aws` CLI invocation, region/auth applied uniformly. Returns the parsed JSON stdout
        (or None for a command with no output), raises RuntimeError with the real CLI stderr on a
        non-zero exit — mirrors HetznerBackend._api()'s own contract so callers' die() messages
        stay meaningful either way."""
        env = dict(os.environ)
        if self.profile:
            env["AWS_PROFILE"] = self.profile
        if self.access_key:
            env["AWS_ACCESS_KEY_ID"] = self.access_key
        if self.secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.secret_key
        if self.session_token:
            env["AWS_SESSION_TOKEN"] = self.session_token
        cmd = ["aws", "--region", self.region, "--output", "json"] + list(args)
        result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
        if result.returncode != 0:
            raise RuntimeError("aws CLI {} failed: {}".format(" ".join(args), result.stderr.strip()))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("AWSBackend only supports config_method=\"cloud-init\" (got '{}') for VM '{}'".format(
                config_method or "<empty>", vm_name))

    def _find_instance(self, vm_name):
        """
        Return the non-terminated instance tagged Name=<vm_name>, or None.

        If create_vm() has cached the InstanceId for this vm_name, that exact instance is returned. The cache is checked
        first because Name tags are not unique in EC2: a tag lookup can return a different instance with the same name, such
        as an older instance that still exists. Every other caller (vm_exists, delete_vm, a newly constructed backend with
        nothing cached) uses the tag lookup below.
        """
        cached_id = self._instance_id_by_vm.get(vm_name)
        if cached_id:
            result = self._aws(
                "ec2", "describe-instances",
                "--instance-ids", cached_id,
                "--filters", "Name=instance-state-name,Values=pending,running,stopping,stopped",
            )
            for reservation in (result or {}).get("Reservations", []):
                instances = reservation.get("Instances", [])
                if instances:
                    return instances[0]
            # Cached ID no longer matches a live instance (terminated elsewhere) —
            # fall through to the tag-based lookup rather than returning None outright.
        result = self._aws(
            "ec2", "describe-instances",
            "--filters", "Name=tag:Name,Values={}".format(vm_name),
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
        )
        for reservation in (result or {}).get("Reservations", []):
            instances = reservation.get("Instances", [])
            if instances:
                return instances[0]
        return None

    def _pick_instance_type(self, vm_cpu, vm_mem_mb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.instance_types or self.INSTANCE_TYPES
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("AWSBackend has no known instance type big enough for VM '{}' ({} vCPU / {} MiB) — "
            "extend AWSBackend.INSTANCE_TYPES or set AWS_INSTANCE_TYPES in "
            "/etc/lab_creation.cfg".format(vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_instance(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the public IP of the instance named vm_name. Falls back to the private IP when no public IP is assigned,
        for example without AWS_SUBNET_ID and with auto-assign of public IPs disabled. Returns None if the instance does not
        exist yet or has no IP (still pending).
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return None
        return instance.get("PublicIpAddress") or instance.get("PrivateIpAddress") or None

    def list_used_macs(self):
        """EC2 has no customer-assignable MAC-address concept — nothing to check against."""
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)  # EC2 assigns its own networking — see _cloud_no_mac()'s docstring

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as the other backends': True = keep, False = destroy and recreate."""
        instance = self._find_instance(vm_name)
        state = (instance or {}).get("State", {}).get("Name")
        if not instance or state != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on EC2 (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        instance = self._find_instance(vm_name)
        if not instance:
            die("EC2 instance '{}' not found — cannot reboot".format(vm_name))
        try:
            self._aws("ec2", "reboot-instances", "--instance-ids", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        instance = self._find_instance(vm_name)
        return (instance.get("State") or {}).get("Name", "unknown") if instance else "not found"

    def start_vm(self, vm_name):
        instance = self._find_instance(vm_name)
        if not instance:
            die("EC2 instance '{}' not found — cannot start".format(vm_name))
        try:
            self._aws("ec2", "start-instances", "--instance-ids", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def stop_vm(self, vm_name):
        instance = self._find_instance(vm_name)
        if not instance:
            die("EC2 instance '{}' not found — cannot stop".format(vm_name))
        try:
            self._aws("ec2", "stop-instances", "--instance-ids", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """Same security-group rules create_vm() adds for open_ports (aws_open_ports)."""
        if ports:
            self.ensure_ports_open(ports)

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        instance = self._find_instance(vm_name)
        if not instance:
            return  # already gone — idempotent, matches every other backend's delete_vm()
        try:
            self._aws("ec2", "terminate-instances", "--instance-ids", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        """No-op beyond validation — ISO_IMAGE is a real AMI ID here, not a file to copy anywhere
        (see this class's own docstring)."""
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'aws' backend — set it to a real AMI ID "
                "(e.g. \"ami-0123456789abcdef0\")".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Stashes this VM's already-generated cloud-init user-data for create_vm() to send at
        launch time — EC2 has no separate "push after boot" step; user-data is a launch parameter
        read by cloud-init on first boot, same as every other cloud-init cloud (matches
        HarvesterBackend's/HetznerBackend's own call-order assumption: this always runs before
        create_vm(), see setup_vm.py's provision_vm())."""
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._user_data_by_vm[vm_name] = userdata_path.read_text()

    def _own_public_ip(self):
        """
        Return this automation node's own public IP, as AWS sees it. The result is cached on the first call, because a
        single setup_lab.py run creates many VMs and the host's outbound address does not change during it. The address
        scopes the SSH rule in _ensure_security_group_access(), so port 22 is not opened to the whole internet.
        """
        if self._cached_public_ip is None:
            try:
                with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as resp:
                    self._cached_public_ip = resp.read().decode("utf-8").strip()
            except (urllib.error.URLError, OSError) as e:
                die("backend 'aws': could not determine this automation node's own public IP "
                    "(needed to open SSH access in the security group) — {}".format(e))
        return self._cached_public_ip

    def _ensure_security_group_access(self, open_ports):
        """
        Ensure self.security_group_id allows: (1) SSH from this automation node's public IP, always, since every AWS VM
        needs it to be reachable (see _own_public_ip()); and (2) each port in `open_ports` (the lab-JSON aws_open_ports list,
        e.g. ["443", "4505", "4506"] or ["69/udp"]; tcp by default) from anywhere, 0.0.0.0/0. The second group is for
        public-facing service ports.

        Rules are only added, never removed or revoked, so rules configured by hand outside this project are kept. The method
        does nothing if no security group is configured.
        """
        if not self.security_group_id:
            return
        wanted = [(22, 22, "tcp", "{}/32".format(self._own_public_ip()))]
        for entry in (open_ports or []):
            entry = str(entry)
            port_s, _, proto = entry.partition("/")
            port = int(port_s)
            wanted.append((port, port, (proto or "tcp").lower(), "0.0.0.0/0"))

        existing = set()
        sg_result = self._aws("ec2", "describe-security-groups", "--group-ids", self.security_group_id)
        for perm in ((sg_result or {}).get("SecurityGroups") or [{}])[0].get("IpPermissions", []):
            for r in perm.get("IpRanges", []):
                existing.add((perm.get("FromPort"), perm.get("ToPort"), perm.get("IpProtocol"), r.get("CidrIp")))

        for from_port, to_port, proto, cidr in wanted:
            if (from_port, to_port, proto, cidr) in existing:
                continue
            log("- Opening {}/{} from {} on security group {}".format(to_port, proto, cidr, self.security_group_id))
            self._aws("ec2", "authorize-security-group-ingress", "--group-id", self.security_group_id,
                       "--protocol", proto, "--port", str(to_port), "--cidr", cidr)

    def ensure_ports_open(self, open_ports):
        """
        VMBackend.ensure_ports_open() override — delegates straight to
        _ensure_security_group_access(), which already adds whatever's
        missing from `open_ports` (plus the unconditional SSH-from-this-
        automation-node rule) to self.security_group_id. The only
        difference from calling it via create_vm() is that this can run
        against an account with NO specific VM being created at all (see
        overlay.py's OVERLAY_HUB_ACCOUNT, used alongside an already-existing
        OVERLAY_HUB_HOST).
        """
        self._ensure_security_group_access(open_ports)

    def get_private_ip(self, vm_name):
        """VMBackend.get_private_ip() override — EC2's own PrivateIpAddress field."""
        instance = self._find_instance(vm_name)
        return (instance or {}).get("PrivateIpAddress") or None

    def get_subnet_cidr(self):
        """VMBackend.get_subnet_cidr() override — describe-subnets on self.subnet_id's own
        CidrBlock. None if no subnet is configured at all."""
        if not self.subnet_id:
            return None
        result = self._aws("ec2", "describe-subnets", "--subnet-ids", self.subnet_id)
        subnets = (result or {}).get("Subnets") or []
        return subnets[0].get("CidrBlock") if subnets else None

    def disable_source_dest_check(self, vm_name):
        """
        VMBackend.disable_source_dest_check() override — EC2's own
        SourceDestCheck instance attribute. Best-effort: a vm_name that
        doesn't currently resolve to a live instance is a silent no-op
        (mirrors this project's other best-effort teardown/setup-adjacent
        cloud calls), not a die() — this is a site gateway's own
        maintenance step, not a hard provisioning dependency for the node
        actually being created.
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return
        instance_id = instance.get("InstanceId")
        if not instance_id:
            return
        self._aws("ec2", "modify-instance-attribute", "--instance-id", instance_id,
                   "--no-source-dest-check")
        log("- Disabled source/dest check on '{}' ({}) — required for it to forward traffic "
            "for other nodes in its site".format(vm_name, instance_id))

    def _ensure_internet_gateway(self):
        """
        Ensure the subnet's VPC has a route to the internet. If no Internet Gateway is attached, create and attach one and
        add the route table's 0.0.0.0/0 route. The method does nothing if self.subnet_id is not set or a working IGW route
        already exists, so a second IGW is never created.

        A VPC without an attached Internet Gateway gives an instance a public IP that cannot be reached. The address is
        assigned, but no route exists for traffic to arrive by, and the symptom is a silent timeout from outside. Security
        groups and network ACLs are not evaluated in that case. This method adds the route, and
        _ensure_security_group_access() opens the ports. Both are needed.
        """
        if not self.subnet_id:
            return
        subnet_result = self._aws("ec2", "describe-subnets", "--subnet-ids", self.subnet_id)
        subnets = (subnet_result or {}).get("Subnets") or []
        if not subnets:
            die("backend 'aws': subnet '{}' not found — check AWS_SUBNET_ID".format(self.subnet_id))
        vpc_id = subnets[0]["VpcId"]

        igw_result = self._aws("ec2", "describe-internet-gateways",
                                "--filters", "Name=attachment.vpc-id,Values={}".format(vpc_id))
        igws = (igw_result or {}).get("InternetGateways") or []
        if igws:
            igw_id = igws[0]["InternetGatewayId"]
        else:
            log("- No Internet Gateway attached to VPC {} — creating one".format(vpc_id))
            create_result = self._aws("ec2", "create-internet-gateway")
            igw_id = create_result["InternetGateway"]["InternetGatewayId"]
            self._aws("ec2", "attach-internet-gateway", "--internet-gateway-id", igw_id, "--vpc-id", vpc_id)

        rt_result = self._aws("ec2", "describe-route-tables",
                               "--filters", "Name=association.subnet-id,Values={}".format(self.subnet_id))
        route_tables = (rt_result or {}).get("RouteTables") or []
        if not route_tables:
            # No explicit per-subnet association -> falls back to the VPC's
            # own main route table, same as AWS itself does.
            rt_result = self._aws("ec2", "describe-route-tables", "--filters",
                                   "Name=vpc-id,Values={}".format(vpc_id), "Name=association.main,Values=true")
            route_tables = (rt_result or {}).get("RouteTables") or []
        if not route_tables:
            die("backend 'aws': could not find a route table for subnet '{}' (VPC {})".format(
                self.subnet_id, vpc_id))
        rt_id = route_tables[0]["RouteTableId"]

        has_default_route = any(
            r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in route_tables[0].get("Routes", []))
        if not has_default_route:
            log("- Adding a 0.0.0.0/0 route to Internet Gateway {} on route table {}".format(igw_id, rt_id))
            self._aws("ec2", "create-route", "--route-table-id", rt_id,
                       "--destination-cidr-block", "0.0.0.0/0", "--gateway-id", igw_id)

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", open_ports=None,
        nested_virtualization="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        self._ensure_internet_gateway()
        self._ensure_security_group_access(open_ports)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_instance_type() and uses
        # the given EC2 instance type name verbatim.
        instance_type = cloud_instance_type or self._pick_instance_type(vm_cpu, vm_mem, vm_name)

        # nested_virtualization: opt-in, off by default. Dies here, before run-instances, when the
        # resolved instance type is not in the nested-virtualization family list, rather than letting
        # AWS reject the launch with a less actionable error. See this class's docstring for what the
        # flag enables.
        nested_virt_enabled = str(nested_virtualization).lower() == "true"
        if nested_virt_enabled:
            family = instance_type.split(".")[0].lower()
            if family not in self._NESTED_VIRT_SUPPORTED_FAMILIES:
                die("backend 'aws': aws_nested_virtualization is 'true' but instance type '{}' "
                    "(family '{}') doesn't support it — AWS's own current supported-family list "
                    "is: {}. Set cloud_instance_type to one of those (e.g. \"m8id.8xlarge\") "
                    "explicitly; this backend's own default T3 family never supports nested "
                    "virtualization.".format(
                        instance_type, family, ", ".join(sorted(self._NESTED_VIRT_SUPPORTED_FAMILIES))))

        image_result = self._aws("ec2", "describe-images", "--image-ids", iso_image)
        images = (image_result or {}).get("Images", [])
        if not images:
            die("AMI '{}' not found for VM '{}' — check ISO_IMAGE and AWS_REGION".format(iso_image, vm_name))
        root_device = images[0].get("RootDeviceName", "/dev/xvda")

        # Raise vm_dsk_gb to the AMI's minimum root volume size when the requested size is smaller.
        # AWS rejects a smaller volume with InvalidBlockDeviceMapping. This is the one place that knows
        # the AMI's minimum, so it covers every caller, including ensure_cloud_dns_vm().
        for bdm in images[0].get("BlockDeviceMappings", []):
            if bdm.get("DeviceName") == root_device:
                ami_min_gb = (bdm.get("Ebs") or {}).get("VolumeSize")
                if ami_min_gb and int(vm_dsk_gb) < ami_min_gb:
                    log("- VM_DSK is {} GiB but AMI '{}' needs at least {} GiB — raising to {} GiB "
                        "(a smaller volume would fail at instance launch)".format(
                            vm_dsk_gb, iso_image, ami_min_gb, ami_min_gb))
                    vm_dsk_gb = ami_min_gb
                break

        args = [
            "ec2", "run-instances",
            "--image-id", iso_image,
            "--instance-type", instance_type,
            # The installed aws CLI takes --count, not the --min-count/--max-count pair; run-instances
            # help lists only --count.
            "--count", "1",
            "--user-data", self._user_data_by_vm.get(vm_name, ""),
            "--block-device-mappings",
            json.dumps([{"DeviceName": root_device, "Ebs": {"VolumeSize": int(vm_dsk_gb)}}]),
            "--tag-specifications",
            "ResourceType=instance,Tags=[{{Key=Name,Value={}}}]".format(vm_name),
        ]
        if self.subnet_id:
            # Always pass --associate-public-ip-address when a subnet is given. A subnet with
            # MapPublicIpOnLaunch=false would otherwise create a private-IP-only instance, which the
            # SSH-based access model every backend assumes cannot reach.
            args += ["--subnet-id", self.subnet_id, "--associate-public-ip-address"]
        if self.security_group_id:
            args += ["--security-group-ids", self.security_group_id]
        if self.key_name:
            args += ["--key-name", self.key_name]
        if nested_virt_enabled:
            # The exact, documented flag (docs.aws.amazon.com/AWSEC2/latest/UserGuide/
            # amazon-ec2-nested-virtualization.html's own "AWS CLI" example) — not a guess.
            args += ["--cpu-options", "NestedVirtualization=enabled"]

        log("Creating VM '{}' on AWS EC2 (instance_type={}{})".format(
            vm_name, instance_type, ", nested virtualization enabled" if nested_virt_enabled else ""))
        try:
            run_result = self._aws(*args)
        except RuntimeError as e:
            die(str(e))

        # Cache the exact InstanceId returned by run-instances. Finding the instance again by its
        # Name tag can return a different instance when an older one with the same name still exists.
        # Later lookups for this vm_name within this backend instance (get_ip(), vm_exists(), ...)
        # target this exact instance. See _find_instance()'s docstring.
        new_instances = (run_result or {}).get("Instances", [])
        if new_instances:
            self._instance_id_by_vm[vm_name] = new_instances[0].get("InstanceId")

        # RunInstances returns the instance, but its IP fields are empty while the state is still
        # pending, so get_ip() polling is required. See create_vm()'s return-value contract on
        # VMBackend.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking logic. EC2 has no
        "cluster capacity" concept the way libvirt/Harvester do — a real constraint would be this
        account's own per-region vCPU service quota, not queried here (a real follow-up, not
        implemented — see HetznerBackend.host_resources()'s identical reasoning). Returns a large
        constant so an "aws" node is never wrongly treated as out of capacity.
        """
        return 9999, 999999, 999999


class GCPBackend(VMBackend):
    """
    Talks to Google Compute Engine by running the `gcloud` CLI (`gcloud compute instances ...`). This
    follows the convention AWSBackend uses for `aws`. GCP request signing is service-account based, so the
    CLI handles it.

    Auth: GCP_SERVICE_ACCOUNT_KEY is the path to a service-account JSON key file. The backend runs
    `gcloud auth activate-service-account --key-file=...` the first time it is resolved. The call is
    idempotent, and gcloud's credential store keeps the activation across invocations. GCP_PROJECT and
    GCP_ZONE are required.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend
    only consumes them:

      - config_method: only "cloud-init", passed as `--metadata-from-file user-data=<path>`. It points at
        the cloud-init file prepare_cloud_init() generates, so no copy or upload step is needed.
      - ISO_IMAGE: must be a GCE image name the operator can access, such as "debian-12" or a custom image.
        The backend does not build or import images. GCP_IMAGE_PROJECT is optional and names the project that
        owns the image when it is not GCP_PROJECT (e.g. "debian-cloud" for Google's public Debian images).
        When omitted, GCP_PROJECT is assumed to own the image.
      - vm_cpu and vm_mem: GCP supports custom machine types (`e2-custom-<cpu>-<mem_mb>`). The backend builds
        one from vm_cpu and vm_mem instead of picking from a table. GCE requires memory in 256 MB multiples,
        so the memory is rounded up. An even vCPU count above 1 is required, so the vCPU count is rounded up to
        the next even number. See _normalize_custom_shape().
      - vm_dsk_gb maps directly to `--boot-disk-size`, as in AWSBackend.
      - Networking: GCP_NETWORK and GCP_SUBNET are optional. When omitted, gcloud uses the project's "default"
        auto-mode VPC.
      - MAC addresses: GCE does not allow a custom MAC on its VirtIO NIC. The backend has no MAC concept, as on
        every cloud backend.
    """

    def __init__(self, project, zone, service_account_key=None, image_project=None, network=None,
                 subnet=None, vm_img_loc=None, lab_setup_path=None):
        self.project = project
        self.zone = zone
        self.service_account_key = service_account_key
        self.image_project = image_project
        self.network = network
        self.subnet = subnet
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        project = config.get("GCP_PROJECT")
        zone = config.get("GCP_ZONE")
        if not project or not zone:
            die("backend 'gcp' requires both GCP_PROJECT and GCP_ZONE to be set in "
                "/etc/lab_creation.cfg (VM '{}')".format(vm_name))
        service_account_key = config.get("GCP_SERVICE_ACCOUNT_KEY")
        if service_account_key:
            result = subprocess.run(
                ["gcloud", "auth", "activate-service-account", "--key-file", service_account_key],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True,
            )
            if result.returncode != 0:
                die("gcloud auth activate-service-account failed: {}".format(result.stderr.strip()))
        return cls(project, zone, service_account_key=service_account_key,
                   image_project=config.get("GCP_IMAGE_PROJECT"), network=config.get("GCP_NETWORK"),
                   subnet=config.get("GCP_SUBNET"), vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _gcloud(self, *args, **kwargs):
        """One `gcloud` CLI invocation, project applied uniformly. Returns the parsed JSON stdout
        (or None for no output), raises RuntimeError with the real CLI stderr on a non-zero exit —
        same contract as HetznerBackend._api()/AWSBackend._aws()."""
        cmd = ["gcloud"] + list(args) + ["--project", self.project, "--format", "json", "--quiet"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            raise RuntimeError("gcloud {} failed: {}".format(" ".join(args), result.stderr.strip()))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("GCPBackend only supports config_method=\"cloud-init\" (got '{}') for VM '{}'".format(
                config_method or "<empty>", vm_name))

    def _find_instance(self, vm_name):
        result = self._gcloud("compute", "instances", "list", "--filter", "name={}".format(vm_name))
        instances = result or []
        return instances[0] if instances else None

    @staticmethod
    def _normalize_custom_shape(vm_cpu, vm_mem_mb):
        """
        Round a requested (vCPU, memory) pair to GCE's custom machine-type constraints: memory in exact 256 MB multiples,
        and an even vCPU count above 1. Both values are rounded up, so the VM is never smaller than requested. Lab JSON
        values that do not follow GCP's rules are accepted.
        """
        cpu = int(vm_cpu)
        if cpu > 1 and cpu % 2 != 0:
            cpu += 1
        mem_mb = int(vm_mem_mb)
        if mem_mb % 256 != 0:
            mem_mb += 256 - (mem_mb % 256)
        return cpu, mem_mb

    def vm_exists(self, vm_name):
        return self._find_instance(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the external (public) IP of the instance, from networkInterfaces[].accessConfigs[].natIP. Falls back to the
        internal networkIP when the NIC has no external IP. Returns None if the instance does not exist or has no IP yet.
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return None
        nics = instance.get("networkInterfaces") or []
        if not nics:
            return None
        access_configs = nics[0].get("accessConfigs") or []
        if access_configs and access_configs[0].get("natIP"):
            return access_configs[0]["natIP"]
        return nics[0].get("networkIP") or None

    def list_used_macs(self):
        """GCE has no customer-assignable MAC-address concept on its standard VirtIO NIC —
        nothing to check a new one against."""
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)  # GCE assigns its own networking — see _cloud_no_mac()'s docstring

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as the other backends': True = keep, False = destroy and recreate."""
        instance = self._find_instance(vm_name)
        status = (instance or {}).get("status")
        if not instance or status != "RUNNING":
            log("  {}KEEP CHECK{} \"{}{}{}\": not RUNNING on GCE (status: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        try:
            self._gcloud("compute", "instances", "reset", vm_name, "--zone", self.zone)
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        instance = self._find_instance(vm_name)
        return (instance or {}).get("status", "unknown") if instance else "not found"

    def start_vm(self, vm_name):
        try:
            self._gcloud("compute", "instances", "start", vm_name, "--zone", self.zone)
        except RuntimeError as e:
            die(str(e))

    def stop_vm(self, vm_name):
        try:
            self._gcloud("compute", "instances", "stop", vm_name, "--zone", self.zone)
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """
        Create one firewall rule per VM (lab-<vm>), allowing `ports` from anywhere to instances carrying the network tag of
        the same name. The tag is added to the VM.
        """
        if not ports:
            return
        name = _gce_name(vm_name.split(".")[0])
        allow = ",".join("{}:{}".format(proto, port) for port, proto in parse_open_ports(ports))
        try:
            existing = self._gcloud("compute", "firewall-rules", "list", "--filter", "name={}".format(name))
            if existing:
                self._gcloud("compute", "firewall-rules", "update", name, "--allow", allow)
            else:
                self._gcloud("compute", "firewall-rules", "create", name,
                             "--network", self.network or "default", "--direction", "INGRESS",
                             "--allow", allow, "--source-ranges", "0.0.0.0/0", "--target-tags", name)
            self._gcloud("compute", "instances", "add-tags", vm_name, "--zone", self.zone, "--tags", name)
        except RuntimeError as e:
            die(str(e))

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        if not self.vm_exists(vm_name):
            return  # already gone — idempotent, matches every other backend's delete_vm()
        try:
            self._gcloud("compute", "instances", "delete", vm_name, "--zone", self.zone)
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        """No-op beyond validation — ISO_IMAGE is a real GCE image name here, not a file to copy
        anywhere (see this class's own docstring)."""
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'gcp' backend — set it to a real GCE "
                "image name (e.g. \"debian-12\")".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Unlike AWS/Hetzner, nothing to stash — create_vm() points --metadata-from-file directly
        at the same cloud-init file this validates exists (matches every other backend's call-
        order assumption: this always runs before create_vm(), see setup_vm.py's provision_vm())."""
        self._require_cloud_init(config_method, vm_name)
        userdata_path = Path(self.lab_setup_path) / "cloud-init" / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Used as the GCE machine-type string
        # verbatim (a predefined type such as n2-standard-4, or a correctly shaped custom one),
        # skipping _normalize_custom_shape().
        if cloud_instance_type:
            machine_type = cloud_instance_type
        else:
            cpu, mem_mb = self._normalize_custom_shape(vm_cpu, vm_mem)
            machine_type = "e2-custom-{}-{}".format(cpu, mem_mb)
        userdata_path = Path(self.lab_setup_path) / "cloud-init" / "{}_user-data".format(vm_name)

        args = [
            "compute", "instances", "create", vm_name,
            "--zone", self.zone,
            "--machine-type", machine_type,
            "--image", iso_image,
            "--boot-disk-size", "{}GB".format(int(vm_dsk_gb)),
            "--metadata-from-file", "user-data={}".format(userdata_path),
        ]
        if self.image_project:
            args += ["--image-project", self.image_project]
        if self.network:
            args += ["--network", self.network]
        if self.subnet:
            args += ["--subnet", self.subnet]

        log("Creating VM '{}' on GCP (machine_type={})".format(vm_name, machine_type))
        try:
            self._gcloud(*args)
        except RuntimeError as e:
            die(str(e))

        # instances create is synchronous, but get_ip() still polls rather than trusting that the
        # IP is already present, matching AWSBackend.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking logic. GCE has no
        "cluster capacity" concept the way libvirt/Harvester do — a real constraint would be this
        project's own per-region quota, not queried here (a real follow-up, not implemented — see
        HetznerBackend/AWSBackend's identical reasoning). Returns a large constant so a "gcp" node
        is never wrongly treated as out of capacity.
        """
        return 9999, 999999, 999999


class AlibabaBackend(VMBackend):
    """
    Talks to Alibaba Cloud ECS by running the `aliyun` CLI (`aliyun ecs ...`). This follows the convention
    AWSBackend and GCPBackend use. Alibaba Cloud request signing is a proprietary scheme, so the CLI handles it.

    Auth: ALIBABA_ACCESS_KEY_ID and ALIBABA_ACCESS_KEY_SECRET are required. They are passed as --access-key-id
    and --access-key-secret flags on each call. ALIBABA_REGION is required.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend
    only consumes them:

      - config_method: only "cloud-init", passed as `--UserData`. Alibaba Cloud requires that value to be
        Base64-encoded, and this backend encodes it. The cloud-init file on disk stays plain text.
      - ISO_IMAGE: must be an Alibaba Cloud ImageId the operator can access. The backend does not build or
        import images.
      - Networking is required. Alibaba Cloud VPC instances need an explicit security group and VSwitch.
        ALIBABA_SECURITY_GROUP_ID and ALIBABA_VSWITCH_ID are both mandatory.
      - vm_cpu and vm_mem: Alibaba Cloud sells fixed (cpu, memory) InstanceType SKUs. The backend picks the
        smallest sufficient one from INSTANCE_TYPES.
      - vm_dsk_gb maps directly to `--SystemDisk.Size`, as in AWS and GCP.
      - InstanceName is a native field, so a VM is found with `DescribeInstances --InstanceName <vm_name>`.
      - MAC addresses: ECS does not allow a custom MAC. The backend has no MAC concept, as on every cloud
        backend.
    """

    # Smallest-to-largest by (cores, memory_gb) — enough of Alibaba's own current general-purpose
    # lineup to cover this project's typical lab-sized nodes; extend as needed. Used only when
    # ALIBABA_INSTANCE_TYPES isn't set — see resolve() and README's Compute backends table for
    # the override and the real URL to Alibaba's own current ECS instance-family catalog.
    INSTANCE_TYPES = [
        ("ecs.g6.large", 2, 8), ("ecs.g6.xlarge", 4, 16), ("ecs.g6.2xlarge", 8, 32),
    ]

    def __init__(self, access_key_id, access_key_secret, region, security_group_id, vswitch_id,
                 instance_types=None, vm_img_loc=None, lab_setup_path=None):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = region
        self.security_group_id = security_group_id
        self.vswitch_id = vswitch_id
        self.instance_types = instance_types  # ALIBABA_INSTANCE_TYPES override, or None -> INSTANCE_TYPES
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}  # populated by push_provisioning_files(), read by create_vm()

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        access_key_id = config.get("ALIBABA_ACCESS_KEY_ID")
        access_key_secret = config.get("ALIBABA_ACCESS_KEY_SECRET")
        region = config.get("ALIBABA_REGION")
        security_group_id = config.get("ALIBABA_SECURITY_GROUP_ID")
        vswitch_id = config.get("ALIBABA_VSWITCH_ID")
        missing = [k for k, v in (
            ("ALIBABA_ACCESS_KEY_ID", access_key_id), ("ALIBABA_ACCESS_KEY_SECRET", access_key_secret),
            ("ALIBABA_REGION", region), ("ALIBABA_SECURITY_GROUP_ID", security_group_id),
            ("ALIBABA_VSWITCH_ID", vswitch_id),
        ) if not v]
        if missing:
            die("backend 'alibaba' requires {} to be set in /etc/lab_creation.cfg (VM '{}')".format(
                ", ".join(missing), vm_name))
        # ALIBABA_INSTANCE_TYPES: optional full override of the built-in INSTANCE_TYPES table, in the
        # form "name:cores:mem_gb,...", e.g. "ecs.g6.large:2:8". See _parse_sku_table() for the exact
        # format and the README for Alibaba's current ECS instance-family catalog.
        instance_types = _parse_sku_table(config.get("ALIBABA_INSTANCE_TYPES"), "ALIBABA_INSTANCE_TYPES")
        return cls(access_key_id, access_key_secret, region, security_group_id, vswitch_id,
                   instance_types=instance_types, vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _aliyun(self, *args, **kwargs):
        """One `aliyun` CLI invocation, region/auth applied uniformly. Returns the parsed JSON
        stdout (or None for no output), raises RuntimeError with the real CLI stderr on a non-zero
        exit — same contract as the other cloud backends' own request helpers."""
        cmd = ["aliyun", "ecs"] + list(args) + [
            "--region", self.region,
            "--access-key-id", self.access_key_id,
            "--access-key-secret", self.access_key_secret,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            raise RuntimeError("aliyun ecs {} failed: {}".format(" ".join(args), result.stderr.strip()))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("AlibabaBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_instance(self, vm_name):
        result = self._aliyun("DescribeInstances", "--InstanceName", vm_name)
        instances = ((result or {}).get("Instances") or {}).get("Instance", [])
        return instances[0] if instances else None

    def _pick_instance_type(self, vm_cpu, vm_mem_mb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.instance_types or self.INSTANCE_TYPES
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("AlibabaBackend has no known InstanceType big enough for VM '{}' ({} vCPU / {} MiB) — "
            "extend AlibabaBackend.INSTANCE_TYPES or set ALIBABA_INSTANCE_TYPES in "
            "/etc/lab_creation.cfg".format(vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_instance(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the public IP of the instance, from PublicIpAddress.IpAddress in DescribeInstances. Falls back to the VPC
        private IP (VpcAttributes.PrivateIpAddress.IpAddress) when no public IP is assigned. Returns None if the instance does
        not exist or has no IP yet.
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return None
        public_ips = (instance.get("PublicIpAddress") or {}).get("IpAddress") or []
        if public_ips:
            return public_ips[0]
        private_ips = ((instance.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress") or []
        return private_ips[0] if private_ips else None

    def list_used_macs(self):
        """ECS has no customer-assignable MAC-address concept — nothing to check against."""
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)  # ECS assigns its own networking — see _cloud_no_mac()'s docstring

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as the other backends': True = keep, False = destroy and recreate."""
        instance = self._find_instance(vm_name)
        status = (instance or {}).get("Status")
        if not instance or status != "Running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not Running on Alibaba Cloud (status: {}) — will "
                "recreate".format(_YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        instance = self._find_instance(vm_name)
        if not instance:
            die("Alibaba Cloud instance '{}' not found — cannot reboot".format(vm_name))
        try:
            self._aliyun("RebootInstance", "--InstanceId", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        instance = self._find_instance(vm_name)
        return (instance or {}).get("Status", "unknown") if instance else "not found"

    def start_vm(self, vm_name):
        self._instance_call(vm_name, "StartInstance")

    def stop_vm(self, vm_name):
        self._instance_call(vm_name, "StopInstance")

    def _instance_call(self, vm_name, call):
        instance = self._find_instance(vm_name)
        if not instance:
            die("Alibaba Cloud instance '{}' not found — cannot {}".format(vm_name, call))
        try:
            self._aliyun(call, "--InstanceId", instance["InstanceId"])
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """
        Add ingress rules from anywhere to ALIBABA_SECURITY_GROUP_ID, which is shared by the lab's instances. Rules that
        already exist are left alone.
        """
        for port, proto in parse_open_ports(ports):
            try:
                self._aliyun("AuthorizeSecurityGroup", "--SecurityGroupId", self.security_group_id,
                             "--IpProtocol", proto, "--PortRange", "{0}/{0}".format(port),
                             "--SourceCidrIp", "0.0.0.0/0")
            except RuntimeError as e:
                if "duplicate" not in str(e).lower():
                    die(str(e))

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        instance = self._find_instance(vm_name)
        if not instance:
            return  # already gone — idempotent, matches every other backend's delete_vm()
        try:
            self._aliyun("DeleteInstance", "--InstanceId", instance["InstanceId"], "--Force", "true")
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        """No-op beyond validation — ISO_IMAGE is a real Alibaba Cloud ImageId here, not a file to
        copy anywhere (see this class's own docstring)."""
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'alibaba' backend — set it to a real "
                "Alibaba Cloud ImageId".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """
        Store this VM's generated cloud-init user-data, Base64-encoded, for create_vm() to send at launch. Alibaba Cloud's
        API requires Base64, while AWS and GCP accept raw text. This runs before create_vm(), as in setup_vm.py's
        provision_vm().
        """
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        raw = userdata_path.read_bytes()
        self._user_data_by_vm[vm_name] = base64.b64encode(raw).decode("ascii")

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_instance_type() and uses
        # the given Alibaba Cloud InstanceType name verbatim.
        instance_type = cloud_instance_type or self._pick_instance_type(vm_cpu, vm_mem, vm_name)
        user_data = self._user_data_by_vm.get(vm_name, "")

        log("Creating VM '{}' on Alibaba Cloud ECS (InstanceType={})".format(vm_name, instance_type))
        try:
            self._aliyun(
                "RunInstances",
                "--ImageId", iso_image,
                "--InstanceType", instance_type,
                "--InstanceName", vm_name,
                "--SecurityGroupId", self.security_group_id,
                "--VSwitchId", self.vswitch_id,
                "--SystemDisk.Size", str(int(vm_dsk_gb)),
                "--UserData", user_data,
            )
        except RuntimeError as e:
            die(str(e))

        # RunInstances returns only an InstanceIdSets list, with no IP, so get_ip() polls
        # DescribeInstances.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking logic. ECS has no
        "cluster capacity" concept the way libvirt/Harvester do — a real constraint would be this
        account's own per-region instance quota, not queried here (a real follow-up, not
        implemented — see HetznerBackend/AWSBackend/GCPBackend's identical reasoning). Returns a
        large constant so an "alibaba" node is never wrongly treated as out of capacity.
        """
        return 9999, 999999, 999999


class ScalewayBackend(VMBackend):
    """
    Talks to the Scaleway Instance API (api.scaleway.com/instance/v2alpha1) directly with the stdlib
    urllib.request. This is the same Bearer-token REST shape as HetznerBackend. Scaleway's X-Auth-Token
    header needs no request signing, so no CLI is required.

    Auth: SCALEWAY_SECRET_KEY and SCALEWAY_PROJECT_ID are required. SCALEWAY_ZONE (e.g. "fr-par-1",
    "nl-ams-1", "pl-waw-1", "it-mil-1") is also required. Scaleway has no default region, unlike AWS and GCP.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend
    only consumes them:

      - config_method must be "cloud-init", which is sent in Scaleway's user_data server field.
      - ISO_IMAGE must be a Scaleway image ID or label.
      - vm_cpu and vm_mem are matched to the smallest sufficient fixed server_type in
        ScalewayBackend.SERVER_TYPES. Scaleway sells fixed-SKU instances, as Hetzner and AWS do.
      - vm_dsk_gb cannot be set independently at creation for most server_types. The local or block volume
        size is bundled with the plan, as on Hetzner. The backend does not attach a separate volume to make up
        a shortfall. It warns instead of silently under-provisioning.
      - MAC addresses are not a customer-assignable concept on Scaleway.
    """

    API_BASE = "https://api.scaleway.com/instance/v2alpha1"

    # Used only when SCALEWAY_SERVER_TYPES isn't set — see resolve() and README's Compute
    # backends table for the override and the real URL to Scaleway's own current lineup.
    SERVER_TYPES = [
        ("DEV1-S", 2, 2), ("DEV1-M", 3, 4), ("DEV1-L", 4, 8), ("GP1-S", 8, 32),
    ]

    def __init__(self, secret_key, project_id, zone, server_types=None, vm_img_loc=None, lab_setup_path=None):
        self.secret_key = secret_key
        self.project_id = project_id
        self.zone = zone
        self.server_types = server_types  # SCALEWAY_SERVER_TYPES override, or None -> SERVER_TYPES
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}  # populated by push_provisioning_files(), read by create_vm()

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        secret_key = config.get("SCALEWAY_SECRET_KEY")
        project_id = config.get("SCALEWAY_PROJECT_ID")
        zone = config.get("SCALEWAY_ZONE")
        missing = [k for k, v in (("SCALEWAY_SECRET_KEY", secret_key), ("SCALEWAY_PROJECT_ID", project_id),
                                   ("SCALEWAY_ZONE", zone)) if not v]
        if missing:
            die("backend 'scaleway' requires {} to be set in /etc/lab_creation.cfg (VM '{}')".format(
                ", ".join(missing), vm_name))
        # SCALEWAY_SERVER_TYPES: optional full override of the built-in SERVER_TYPES table, in the
        # form "name:cores:mem_gb,...", e.g. "DEV1-S:2:2,DEV1-M:3:4". See _parse_sku_table() for the
        # exact format and the README for Scaleway's current commercial-type lineup.
        server_types = _parse_sku_table(config.get("SCALEWAY_SERVER_TYPES"), "SCALEWAY_SERVER_TYPES")
        return cls(secret_key, project_id, zone, server_types=server_types,
                   vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _api(self, method, path, body=None):
        """One JSON request against the Scaleway Instance API. Same contract as
        HetznerBackend._api() (its closest sibling among these backends)."""
        url = "{}/zones/{}{}".format(self.API_BASE, self.zone, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Auth-Token": self.secret_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("Scaleway API {} {} failed ({}): {}".format(method, path, e.code, detail))

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("ScalewayBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_server(self, vm_name):
        result = self._api("GET", "/servers?name={}".format(vm_name))
        servers = (result or {}).get("servers", [])
        return servers[0] if servers else None

    def _pick_server_type(self, vm_cpu, vm_mem_mb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.server_types or self.SERVER_TYPES
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("ScalewayBackend has no known server_type big enough for VM '{}' ({} vCPU / {} MiB) — "
            "extend ScalewayBackend.SERVER_TYPES or set SCALEWAY_SERVER_TYPES in "
            "/etc/lab_creation.cfg".format(vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_server(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the public IP of the server, from public_ip.address. Returns None if the server does not exist or has no
        public IP yet, for example while booting, or on a private-network-only server.
        """
        server = self._find_server(vm_name)
        if not server:
            return None
        return (server.get("public_ip") or {}).get("address") or None

    def list_used_macs(self):
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)

    def vm_is_reusable(self, vm_name, mymac, myip):
        server = self._find_server(vm_name)
        state = (server or {}).get("state")
        if not server or state != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on Scaleway (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        server = self._find_server(vm_name)
        if not server:
            die("Scaleway server '{}' not found — cannot reboot".format(vm_name))
        try:
            self._api("POST", "/servers/{}/action".format(server["id"]), {"action": "reboot"})
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        server = self._find_server(vm_name)
        return (server or {}).get("state", "unknown") if server else "not found"

    def start_vm(self, vm_name):
        self._server_action(vm_name, "poweron")

    def stop_vm(self, vm_name):
        self._server_action(vm_name, "poweroff")

    def _server_action(self, vm_name, action):
        server = self._find_server(vm_name)
        if not server:
            die("Scaleway server '{}' not found — cannot {}".format(vm_name, action))
        try:
            self._api("POST", "/servers/{}/action".format(server["id"]), {"action": action})
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """Scaleway (default security group policy: accept) instances accept inbound traffic unless a firewall/security group
        you set up says otherwise — nothing for lab-in-a-box to open."""
        if ports:
            log("- open_ports: Scaleway (default security group policy: accept) lets inbound traffic in by default — nothing to "
                "open (add rules yourself if this project filters traffic)")

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        server = self._find_server(vm_name)
        if not server:
            return
        try:
            self._api("DELETE", "/servers/{}".format(server["id"]))
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'scaleway' backend — set it to a real "
                "Scaleway image ID/label".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._user_data_by_vm[vm_name] = userdata_path.read_text()

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_server_type() and uses the
        # given Scaleway commercial_type name verbatim.
        server_type = cloud_instance_type or self._pick_server_type(vm_cpu, vm_mem, vm_name)

        body = {
            "name": vm_name,
            "commercial_type": server_type,
            "project": self.project_id,
            "image": iso_image,
        }
        log("Creating VM '{}' on Scaleway (server_type={})".format(vm_name, server_type))
        try:
            result = self._api("POST", "/servers", body)
        except RuntimeError as e:
            die(str(e))
        server = (result or {}).get("server", {})
        server_id = server.get("id")
        user_data = self._user_data_by_vm.get(vm_name, "")
        if server_id and user_data:
            # user_data is set with a separate PATCH to /servers/{id}/user_data/cloud-init, not inline
            # in the create body as Hetzner and AWS do it.
            try:
                url = "{}/zones/{}/servers/{}/user_data/cloud-init".format(self.API_BASE, self.zone, server_id)
                req = urllib.request.Request(url, data=user_data.encode("utf-8"), method="PATCH", headers={
                    "X-Auth-Token": self.secret_key, "Content-Type": "text/plain",
                })
                urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as e:
                die("failed to set cloud-init user_data for '{}': {}".format(
                    vm_name, e.read().decode("utf-8", errors="replace")))
        try:
            self._api("POST", "/servers/{}/action".format(server_id), {"action": "poweron"})
        except RuntimeError as e:
            die(str(e))

        # A public IP is not reported until poweron completes, so get_ip() polling is required.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        return 9999, 999999, 999999


class UpCloudBackend(VMBackend):
    """
    Talks to the UpCloud API (api.upcloud.com/1.3) directly with the stdlib urllib.request, using HTTP
    Basic Auth. This is UpCloud's simplest documented method. The token-based alternative is not supported, so
    the backend has one auth path, as HetznerBackend and ScalewayBackend do.

    Auth: UPCLOUD_USERNAME and UPCLOUD_PASSWORD are required. They belong to an UpCloud subaccount with API
    access enabled in the control panel. UPCLOUD_ZONE (e.g. "fi-hel1", "uk-lon1", "us-chi1") is also required.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend
    only consumes them:

      - config_method must be "cloud-init".
      - ISO_IMAGE must be an UpCloud storage or template UUID the operator can access. It is the clone source
        for storage_devices. It is never a qcow2 or ISO filename.
      - vm_cpu and vm_mem are matched to the smallest sufficient fixed plan in UpCloudBackend.PLANS. UpCloud
        names plans "NxCPU-MGB". This is the same fixed-SKU approach as Hetzner, AWS and Scaleway.
      - vm_dsk_gb maps directly to the storage device's size field, as in AWS and GCP.
      - MAC addresses are not a customer-assignable concept on UpCloud.
      - A server is found by name with a client-side filter over the full GET /server list. The list endpoint
        has no server-side name filter. This is adequate for a lab-sized account, but slow on an account with
        many servers.
    """

    API_BASE = "https://api.upcloud.com/1.3"

    # Used only when UPCLOUD_PLANS isn't set — see resolve() and README's Compute backends table
    # for the override and the real URL to UpCloud's own current plan lineup.
    PLANS = [
        ("1xCPU-2GB", 1, 2), ("2xCPU-4GB", 2, 4), ("4xCPU-8GB", 4, 8), ("6xCPU-16GB", 6, 16),
    ]

    def __init__(self, username, password, zone, plans=None, vm_img_loc=None, lab_setup_path=None):
        self.username = username
        self.password = password
        self.zone = zone
        self.plans = plans  # UPCLOUD_PLANS override, or None -> PLANS
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        username = config.get("UPCLOUD_USERNAME")
        password = config.get("UPCLOUD_PASSWORD")
        zone = config.get("UPCLOUD_ZONE")
        missing = [k for k, v in (("UPCLOUD_USERNAME", username), ("UPCLOUD_PASSWORD", password),
                                   ("UPCLOUD_ZONE", zone)) if not v]
        if missing:
            die("backend 'upcloud' requires {} to be set in /etc/lab_creation.cfg (VM '{}')".format(
                ", ".join(missing), vm_name))
        # UPCLOUD_PLANS: optional full override of the built-in PLANS table, in the form
        # "name:cores:mem_gb,...", e.g. "1xCPU-2GB:1:2,2xCPU-4GB:2:4". See _parse_sku_table() for the
        # exact format and the README for UpCloud's current plans.
        plans = _parse_sku_table(config.get("UPCLOUD_PLANS"), "UPCLOUD_PLANS")
        return cls(username, password, zone, plans=plans, vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _api(self, method, path, body=None):
        url = "{}{}".format(self.API_BASE, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        auth = base64.b64encode("{}:{}".format(self.username, self.password).encode("utf-8")).decode("ascii")
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Basic {}".format(auth),
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("UpCloud API {} {} failed ({}): {}".format(method, path, e.code, detail))

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("UpCloudBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_server(self, vm_name):
        """Client-side filter over the full server list — see this class's own docstring for why."""
        result = self._api("GET", "/server")
        servers = ((result or {}).get("servers") or {}).get("server", [])
        return next((s for s in servers if s.get("title") == vm_name), None)

    def _pick_plan(self, vm_cpu, vm_mem_mb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.plans or self.PLANS
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("UpCloudBackend has no known plan big enough for VM '{}' ({} vCPU / {} MiB) — extend "
            "UpCloudBackend.PLANS or set UPCLOUD_PLANS in /etc/lab_creation.cfg".format(
                vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_server(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the server's IP address, preferring the public address from ip_addresses.ip_address (access="public"). Falls
        back to the first "private" address, for example on a private-network-only plan. Returns None if the server does not
        exist or reports no IP addresses yet.
        """
        server = self._find_server(vm_name)
        if not server:
            return None
        addrs = ((server.get("ip_addresses") or {}).get("ip_address")) or []
        public = next((a["address"] for a in addrs if a.get("access") == "public" and a.get("address")), None)
        if public:
            return public
        private = next((a["address"] for a in addrs if a.get("address")), None)
        return private

    def list_used_macs(self):
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)

    def vm_is_reusable(self, vm_name, mymac, myip):
        server = self._find_server(vm_name)
        state = (server or {}).get("state")
        if not server or state != "started":
            log("  {}KEEP CHECK{} \"{}{}{}\": not started on UpCloud (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        server = self._find_server(vm_name)
        if not server:
            die("UpCloud server '{}' not found — cannot reboot".format(vm_name))
        try:
            self._api("POST", "/server/{}/restart".format(server["uuid"]), {"restart_server": {}})
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        server = self._find_server(vm_name)
        return (server or {}).get("state", "unknown") if server else "not found"

    def start_vm(self, vm_name):
        server = self._require_server(vm_name, "start")
        try:
            self._api("POST", "/server/{}/start".format(server["uuid"]))
        except RuntimeError as e:
            die(str(e))

    def stop_vm(self, vm_name):
        server = self._require_server(vm_name, "stop")
        try:
            self._api("POST", "/server/{}/stop".format(server["uuid"]),
                      {"stop_server": {"stop_type": "soft", "timeout": "120"}})
        except RuntimeError as e:
            die(str(e))

    def _require_server(self, vm_name, action):
        server = self._find_server(vm_name)
        if not server:
            die("UpCloud server '{}' not found — cannot {}".format(vm_name, action))
        return server

    def open_vm_ports(self, vm_name, ports):
        """UpCloud (server firewall off by default) instances accept inbound traffic unless a firewall/security group
        you set up says otherwise — nothing for lab-in-a-box to open."""
        if ports:
            log("- open_ports: UpCloud (server firewall off by default) lets inbound traffic in by default — nothing to "
                "open (add rules yourself if this project filters traffic)")

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        server = self._find_server(vm_name)
        if not server:
            return
        try:
            self._api("DELETE", "/server/{}?storages=1".format(server["uuid"]))
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'upcloud' backend — set it to a real "
                "UpCloud storage/template UUID".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._user_data_by_vm[vm_name] = userdata_path.read_text()

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_plan() and uses the given
        # UpCloud plan name verbatim.
        plan = cloud_instance_type or self._pick_plan(vm_cpu, vm_mem, vm_name)

        body = {
            "server": {
                "zone": self.zone,
                "title": vm_name,
                "hostname": vm_name,
                "plan": plan,
                "user_data": self._user_data_by_vm.get(vm_name, ""),
                "storage_devices": {
                    "storage_device": [{
                        "action": "clone",
                        "storage": iso_image,
                        "title": "{}-disk0".format(vm_name),
                        "size": int(vm_dsk_gb),
                        "tier": "maxiops",
                    }],
                },
            },
        }
        log("Creating VM '{}' on UpCloud (plan={})".format(vm_name, plan))
        try:
            self._api("POST", "/server", body)
        except RuntimeError as e:
            die(str(e))

        # UpCloud's create-server response may carry the assigned IP addresses inline, but get_ip() polls
        # regardless, so the response shape is not parsed separately, matching AWSBackend.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        return 9999, 999999, 999999


class OVHcloudBackend(VMBackend):
    """
    Talks to the OVHcloud API (Public Cloud instances, `{endpoint}/cloud/project/...`) directly with the
    stdlib urllib.request. OVHcloud signs requests itself, instead of using a bearer token or Basic Auth as the
    other raw-REST backends do.

    Auth, all required: OVH_APPLICATION_KEY and OVH_APPLICATION_SECRET come from an OVH API application created
    at https://api.ovh.com/createApp/. OVH_CONSUMER_KEY is a consumer key validated for that application and
    scoped to the /cloud/project/* routes. OVH uses this two-step application and consumer model. OVH_SERVICE_NAME
    (the Public Cloud project ID) and OVH_REGION (e.g. "GRA7", "SBG5", "BHS5") are required. OVH_ENDPOINT is
    optional and defaults to https://eu.api.ovh.com/1.0. Set it to the ca or us root for accounts registered
    outside the EU.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend only
    consumes them:

      - config_method must be "cloud-init".
      - ISO_IMAGE must be a region-scoped OVHcloud Public Cloud imageId (a UUID, looked up through OVH's image API
        or the console). It is not a qcow2 or ISO filename, and it is not a readable name.
      - vm_dsk_gb cannot be set at creation. The disk is bundled with the flavor, as on Hetzner and Scaleway. The
        backend warns instead of silently under-provisioning.
      - MAC addresses are not a customer-assignable concept on OVHcloud.
      - Flavor IDs are per-region UUIDs, not stable names, so the backend cannot ship a static vCPU and RAM table.
        _pick_flavor() calls GET .../flavor?region=<region> at create time and picks the smallest flavor whose
        vcpus and ram fields meet the request.

    Request signing follows OVH's documented scheme: X-Ovh-Application, X-Ovh-Consumer, X-Ovh-Timestamp and
    X-Ovh-Signature headers. The signature is "$1$" + SHA1_HEX(AppSecret+"+"+ConsumerKey+"+"+METHOD+"+"+URL+"+"+BODY+"+"+TIMESTAMP).
    The timestamp comes from the unauthenticated /auth/time endpoint, as in OVH's own SDKs, so local clock drift
    does not break signatures.
    """

    def __init__(self, application_key, application_secret, consumer_key, service_name, region,
                 endpoint="https://eu.api.ovh.com/1.0", vm_img_loc=None, lab_setup_path=None):
        self.application_key = application_key
        self.application_secret = application_secret
        self.consumer_key = consumer_key
        self.service_name = service_name
        self.region = region
        self.endpoint = endpoint.rstrip("/")
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._user_data_by_vm = {}

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        application_key = config.get("OVH_APPLICATION_KEY")
        application_secret = config.get("OVH_APPLICATION_SECRET")
        consumer_key = config.get("OVH_CONSUMER_KEY")
        service_name = config.get("OVH_SERVICE_NAME")
        region = config.get("OVH_REGION")
        endpoint = config.get("OVH_ENDPOINT") or "https://eu.api.ovh.com/1.0"
        missing = [k for k, v in (
            ("OVH_APPLICATION_KEY", application_key), ("OVH_APPLICATION_SECRET", application_secret),
            ("OVH_CONSUMER_KEY", consumer_key), ("OVH_SERVICE_NAME", service_name), ("OVH_REGION", region),
        ) if not v]
        if missing:
            die("backend 'ovhcloud' requires {} to be set in /etc/lab_creation.cfg (VM '{}')".format(
                ", ".join(missing), vm_name))
        return cls(application_key, application_secret, consumer_key, service_name, region,
                   endpoint=endpoint, vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _server_timestamp(self):
        """
        Return the current time from OVH's unauthenticated /auth/time endpoint. The signature timestamp comes from OVH's
        server, as in OVH's SDKs, so a drifted local clock does not break authentication.
        """
        req = urllib.request.Request("{}/auth/time".format(self.endpoint), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return int(resp.read().decode("utf-8").strip())
        except (urllib.error.URLError, ValueError):
            return int(time.time())

    def _sign(self, method, url, body_str, timestamp):
        to_sign = "+".join([self.application_secret, self.consumer_key, method, url, body_str, str(timestamp)])
        return "$1$" + hashlib.sha1(to_sign.encode("utf-8")).hexdigest()

    def _api(self, method, path, body=None):
        url = "{}{}".format(self.endpoint, path)
        body_str = json.dumps(body) if body is not None else ""
        timestamp = self._server_timestamp()
        signature = self._sign(method, url, body_str, timestamp)
        data = body_str.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Ovh-Application": self.application_key,
            "X-Ovh-Consumer": self.consumer_key,
            "X-Ovh-Timestamp": str(timestamp),
            "X-Ovh-Signature": signature,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("OVHcloud API {} {} failed ({}): {}".format(method, path, e.code, detail))

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("OVHcloudBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_instance(self, vm_name):
        result = self._api("GET", "/cloud/project/{}/instance".format(self.service_name))
        instances = result or []
        return next((i for i in instances if i.get("name") == vm_name), None)

    def _pick_flavor(self, vm_cpu, vm_mem_mb, vm_name):
        """No stable name/vCPU/RAM table is possible here — flavorId is a per-region UUID, so this
        does a real flavor-list call and picks the smallest one satisfying both constraints."""
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        flavors = self._api("GET", "/cloud/project/{}/flavor?region={}".format(
            self.service_name, self.region)) or []
        candidates = [f for f in flavors if f.get("vcpus", 0) >= int(vm_cpu)
                      and (f.get("ram", 0) / 1024.0) >= needed_mem_gb]
        if not candidates:
            die("OVHcloudBackend found no flavor in region '{}' big enough for VM '{}' ({} vCPU / "
                "{} MiB)".format(self.region, vm_name, vm_cpu, vm_mem_mb))
        candidates.sort(key=lambda f: (f.get("vcpus", 0), f.get("ram", 0)))
        return candidates[0]["id"]

    def vm_exists(self, vm_name):
        return self._find_instance(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return an IPv4 public address from the instance's ipAddresses list (entries shaped {"ip": ..., "type": "public" or
        "private", "version": 4}). Falls back to a private address. Returns None if the instance does not exist or has no IP yet.
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return None
        addrs = instance.get("ipAddresses") or []
        public = next((a["ip"] for a in addrs if a.get("type") == "public" and a.get("version") == 4), None)
        if public:
            return public
        private = next((a["ip"] for a in addrs if a.get("ip")), None)
        return private

    def list_used_macs(self):
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)

    def vm_is_reusable(self, vm_name, mymac, myip):
        instance = self._find_instance(vm_name)
        status = (instance or {}).get("status")
        if not instance or status != "ACTIVE":
            log("  {}KEEP CHECK{} \"{}{}{}\": not ACTIVE on OVHcloud (status: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        instance = self._find_instance(vm_name)
        if not instance:
            die("OVHcloud instance '{}' not found — cannot reboot".format(vm_name))
        try:
            self._api("POST", "/cloud/project/{}/instance/{}/reboot".format(
                self.service_name, instance["id"]), {"type": "hard"})
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        instance = self._find_instance(vm_name)
        return (instance or {}).get("status", "unknown") if instance else "not found"

    def start_vm(self, vm_name):
        self._instance_action(vm_name, "start")

    def stop_vm(self, vm_name):
        self._instance_action(vm_name, "stop")

    def _instance_action(self, vm_name, action):
        instance = self._find_instance(vm_name)
        if not instance:
            die("OVHcloud instance '{}' not found — cannot {}".format(vm_name, action))
        try:
            self._api("POST", "/cloud/project/{}/instance/{}/{}".format(
                self.service_name, instance["id"], action))
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """OVHcloud Public Cloud instances accept inbound traffic unless a firewall/security group
        you set up says otherwise — nothing for lab-in-a-box to open."""
        if ports:
            log("- open_ports: OVHcloud Public Cloud lets inbound traffic in by default — nothing to "
                "open (add rules yourself if this project filters traffic)")

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        instance = self._find_instance(vm_name)
        if not instance:
            return
        try:
            self._api("DELETE", "/cloud/project/{}/instance/{}".format(self.service_name, instance["id"]))
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'ovhcloud' backend — set it to a real "
                "OVHcloud Public Cloud imageId (UUID) for region '{}'".format(vm_name, self.region))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._user_data_by_vm[vm_name] = userdata_path.read_text()

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_flavor()'s live API call and
        # uses the given OVHcloud flavorId verbatim. The flavorId is a per-region UUID; see this
        # class's docstring for why there is no stable name.
        flavor_id = cloud_instance_type or self._pick_flavor(vm_cpu, vm_mem, vm_name)

        body = {
            "name": vm_name,
            "flavorId": flavor_id,
            "imageId": iso_image,
            "region": self.region,
            "userData": self._user_data_by_vm.get(vm_name, ""),
        }
        log("Creating VM '{}' on OVHcloud (flavorId={}, region={})".format(vm_name, flavor_id, self.region))
        try:
            self._api("POST", "/cloud/project/{}/instance".format(self.service_name), body)
        except RuntimeError as e:
            die(str(e))

        # ipAddresses are not populated until well after BUILDING completes, so get_ip() polls.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        return 9999, 999999, 999999


class ExoscaleBackend(VMBackend):
    """
    Talks to Exoscale by running the `exo` CLI (`exo compute instance ...`). This follows the convention
    AWSBackend, GCPBackend and AlibabaBackend use. Exoscale's IAM-key request signing is proprietary, so the CLI
    handles it.

    Auth: EXOSCALE_API_KEY and EXOSCALE_API_SECRET are passed as environment variables to the exo subprocess only,
    and are never written to disk. This is exo's non-interactive auth method, so no config profile is needed.
    EXOSCALE_ZONE (e.g. "ch-gva-2", "de-fra-1", "at-vie-1") is required. Exoscale is always zone-scoped.

    Mismatches with the KVM model. The operator provisions the cloud-native prerequisites, and this backend only
    consumes them:

      - config_method: only "cloud-init". It is passed to `exo compute instance create --cloud-init <file>`
        pointing at the generated cloud-init file. The CLI reads and encodes the file.
      - ISO_IMAGE must be an Exoscale template ID or name the operator can access. It is not a qcow2 or ISO filename.
      - vm_cpu and vm_mem are matched to the smallest sufficient fixed instance-type in
        ExoscaleBackend.INSTANCE_TYPES, which follows Exoscale's "standard.<size>" naming.
      - vm_dsk_gb maps directly to --disk-size.
      - MAC addresses are not a customer-assignable concept on Exoscale.
    """

    # Smallest-to-largest by (cores, memory_gb), following Exoscale's standard family naming.
    # Used only when EXOSCALE_INSTANCE_TYPES is not set. resolve() and the README's Compute
    # backends table give the override and the catalog URL.
    INSTANCE_TYPES = [
        ("standard.tiny", 1, 1), ("standard.small", 1, 2), ("standard.medium", 2, 4),
        ("standard.large", 4, 8), ("standard.extra-large", 4, 16),
    ]

    def __init__(self, api_key, api_secret, zone, instance_types=None, vm_img_loc=None, lab_setup_path=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.zone = zone
        self.instance_types = instance_types  # EXOSCALE_INSTANCE_TYPES override, or None -> INSTANCE_TYPES
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        self._userdata_path_by_vm = {}  # populated by push_provisioning_files(), read by create_vm()

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        api_key = config.get("EXOSCALE_API_KEY")
        api_secret = config.get("EXOSCALE_API_SECRET")
        zone = config.get("EXOSCALE_ZONE")
        missing = [k for k, v in (("EXOSCALE_API_KEY", api_key), ("EXOSCALE_API_SECRET", api_secret),
                                   ("EXOSCALE_ZONE", zone)) if not v]
        if missing:
            die("backend 'exoscale' requires {} to be set in /etc/lab_creation.cfg (VM '{}')".format(
                ", ".join(missing), vm_name))
        # EXOSCALE_INSTANCE_TYPES: optional full override of the built-in INSTANCE_TYPES table, in the
        # form "name:cores:mem_gb,...", e.g. "standard.tiny:1:1,standard.small:1:2". See
        # _parse_sku_table() for the exact format and the README for Exoscale's current catalog.
        instance_types = _parse_sku_table(config.get("EXOSCALE_INSTANCE_TYPES"), "EXOSCALE_INSTANCE_TYPES")
        return cls(api_key, api_secret, zone, instance_types=instance_types,
                   vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    def _exo(self, *args, **kwargs):
        """One `exo` CLI invocation, zone/auth applied uniformly. Returns the parsed JSON stdout
        (or None for a command with no output), raises RuntimeError with the real CLI stderr on a
        non-zero exit — mirrors AWSBackend._aws()'s own contract."""
        env = dict(os.environ)
        env["EXOSCALE_API_KEY"] = self.api_key
        env["EXOSCALE_API_SECRET"] = self.api_secret
        cmd = ["exo"] + list(args) + ["--zone", self.zone, "-O", "json"]
        result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
        if result.returncode != 0:
            raise RuntimeError("exo {} failed: {}".format(" ".join(args), result.stderr.strip()))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("ExoscaleBackend only supports config_method=\"cloud-init\" (got '{}') for VM "
                "'{}'".format(config_method or "<empty>", vm_name))

    def _find_instance(self, vm_name):
        result = self._exo("compute", "instance", "list") or []
        return next((i for i in result if i.get("name") == vm_name), None)

    def _pick_instance_type(self, vm_cpu, vm_mem_mb, vm_name):
        needed_mem_gb = int(vm_mem_mb) / 1024.0
        table = self.instance_types or self.INSTANCE_TYPES
        for name, cores, mem_gb in table:
            if cores >= int(vm_cpu) and mem_gb >= needed_mem_gb:
                return name
        die("ExoscaleBackend has no known instance-type big enough for VM '{}' ({} vCPU / {} MiB) "
            "— extend ExoscaleBackend.INSTANCE_TYPES or set EXOSCALE_INSTANCE_TYPES in "
            "/etc/lab_creation.cfg".format(vm_name, vm_cpu, vm_mem_mb))

    def vm_exists(self, vm_name):
        return self._find_instance(vm_name) is not None

    def get_ip(self, vm_name):
        """
        Return the public IP from the instance-list entry's "public-ip" field, as printed by exo's JSON output. Returns None
        if the instance does not exist or has no public IP yet, for example a private-network-only instance or one still
        starting.
        """
        instance = self._find_instance(vm_name)
        if not instance:
            return None
        return instance.get("public-ip") or None

    def list_used_macs(self):
        return [], {}

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        return _cloud_no_mac(mymac)

    def vm_is_reusable(self, vm_name, mymac, myip):
        instance = self._find_instance(vm_name)
        state = (instance or {}).get("state")
        if not instance or state != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on Exoscale (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        if not self.vm_exists(vm_name):
            die("Exoscale instance '{}' not found — cannot reboot".format(vm_name))
        try:
            self._exo("compute", "instance", "reboot", vm_name, "-f")
        except RuntimeError as e:
            die(str(e))

    def vm_state(self, vm_name):
        instance = self._find_instance(vm_name)
        return (instance or {}).get("state", "unknown") if instance else "not found"

    def start_vm(self, vm_name):
        try:
            self._exo("compute", "instance", "start", vm_name)
        except RuntimeError as e:
            die(str(e))

    def stop_vm(self, vm_name):
        try:
            self._exo("compute", "instance", "stop", vm_name, "--force")
        except RuntimeError as e:
            die(str(e))

    def open_vm_ports(self, vm_name, ports):
        """
        Create a per-VM security group (lab-<vm>) with one ingress rule per port from anywhere, and attach it to the
        instance. Exoscale's default security group allows no inbound traffic. Existing rules or attachments are left alone.
        """
        if not ports:
            return
        name = _gce_name(vm_name.split(".")[0])
        try:
            groups = self._exo("compute", "security-group", "list") or []
            if not any(g.get("name") == name for g in groups):
                self._exo("compute", "security-group", "create", name)
        except RuntimeError as e:
            die(str(e))
        for port, proto in parse_open_ports(ports):
            try:
                self._exo("compute", "security-group", "rule", "add", name, "--flow", "ingress",
                          "--protocol", proto, "--port", str(port), "--network", "0.0.0.0/0")
            except RuntimeError as e:
                if "already" not in str(e).lower() and "duplicate" not in str(e).lower():
                    die(str(e))
        try:
            self._exo("compute", "instance", "security-group", "add", vm_name, name)
        except RuntimeError as e:
            if "already" not in str(e).lower():
                die(str(e))

    def delete_vm(self, vm_name):
        log("Deleting VM '{}'".format(vm_name))
        if not self.vm_exists(vm_name):
            return
        try:
            self._exo("compute", "instance", "delete", vm_name, "-f")
        except RuntimeError as e:
            die(str(e))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        if not iso_image:
            die("ISO_IMAGE is required for VM '{}' on the 'exoscale' backend — set it to a real "
                "Exoscale template ID/name".format(vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Just validates the file exists — `exo`'s own `--cloud-init` flag reads the file path
        directly at create time, no stash/encode step needed (unlike Alibaba's Base64 requirement
        or Scaleway's separate PATCH call)."""
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))
        self._userdata_path_by_vm[vm_name] = str(userdata_path)

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, cloud_instance_type="", **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        # cloud_instance_type: explicit lab-JSON override. Bypasses _pick_instance_type() and uses
        # the given Exoscale instance-type name verbatim.
        instance_type = cloud_instance_type or self._pick_instance_type(vm_cpu, vm_mem, vm_name)
        userdata_path = self._userdata_path_by_vm.get(vm_name)

        log("Creating VM '{}' on Exoscale (instance-type={})".format(vm_name, instance_type))
        args = [
            "compute", "instance", "create", vm_name,
            "--template", iso_image,
            "--instance-type", instance_type,
            "--disk-size", str(int(vm_dsk_gb)),
        ]
        if userdata_path:
            args += ["--cloud-init", userdata_path]
        try:
            self._exo(*args)
        except RuntimeError as e:
            die(str(e))

        # exo compute instance create is synchronous, but get_ip() still polls, matching AWSBackend.
        log("Waiting for '{}' to be assigned a real IP address".format(vm_name))
        return _poll_for_ip(lambda: self.get_ip(vm_name), vm_name)

    def host_resources(self):
        return 9999, 999999, 999999


BACKENDS = {
    "libvirt": LibvirtBackend,
    "harvester": HarvesterBackend,
    "hetzner": HetznerBackend,
    "aws": AWSBackend,
    "gcp": GCPBackend,
    "alibaba": AlibabaBackend,
    "scaleway": ScalewayBackend,
    "upcloud": UpCloudBackend,
    "ovhcloud": OVHcloudBackend,
    "exoscale": ExoscaleBackend,
}


# Every non-libvirt, non-Harvester backend: dynamic-IP, no-MAC-concept cloud providers. Used to
# decide whether a node needs the cloud-DNS-VM treatment in setup_vm.py's provision_vm().
CLOUD_BACKEND_NAMES = frozenset(BACKENDS) - {"libvirt", "harvester"}


def _cloud_dns_vm_user_data(root_ssh_key, mydomain):
    """
    Minimal cloud-init for the cloud DNS VM created by ensure_cloud_dns_vm(). It installs BIND and serves
    `mydomain` from the same on-disk path and convention DNSService (libs/services.py) uses for
    automation.mydemo.lab: NAMED_ZONE_DIR is /var/lib/named, and named is restarted with `systemctl restart named`.
    This lets add_to_dns() append records through the existing remote_dns_servers mechanism, which SSH-appends a line
    to a zone file and restarts named on each listed server, with no new propagation code.

    Cloud AMIs are usually Ubuntu or Debian, not SUSE. Debian and Ubuntu's bind9 package expects zone files under
    /etc/bind or /var/cache/bind. The shim creates /var/lib/named and points named.conf.local at it, instead of
    porting DNSService's hardcoded path.

    Ubuntu 24.04's bind9 package ships a working named.service at /usr/lib/systemd/system/named.service. Enable,
    start and restart `named` directly. Do not add a bind9.service alias in /etc/systemd/system, because that alias
    shadows the packaged unit.

    The zone is queried successfully through the DNS VM's private IP and its loopback address. Querying the same zone
    through the instance's public IP can return a root-zone NXDOMAIN, so records on this VM are not reliably
    resolvable from outside the cloud network. The cause is not yet identified. See TODO.
    """
    def yq(s):
        """YAML single-quoted scalar: wrap in '...', doubling any literal ' per YAML's own
        escaping rule (YAML single-quoted strings don't support backslash escapes at all)."""
        return "'" + s.replace("'", "''") + "'"

    zone_lines = [
        "$TTL 300",
        "@ IN SOA ns.{d}. admin.{d}. ( 1 3600 900 604800 300 )".format(d=mydomain),
        "@ IN NS ns.{d}.".format(d=mydomain),
        "ns IN A 127.0.0.1",
    ]
    zone_printf_args = " ".join("'{}'".format(line) for line in zone_lines)
    named_conf_line = (
        'zone "{d}" {{ type master; file "/var/lib/named/{d}.lan"; allow-update {{ none; }}; }};'
    ).format(d=mydomain)

    # Each entry is one shell command, run through runcmd. cloud-init runs runcmd last, after
    # packages has installed bind9. write_files runs earlier, before packages install, so a
    # bind:bind chown or a write into /var/lib/named would fail on a stock image.
    #
    # Ubuntu's bind9 package already ships a working named.service in /usr/lib/systemd/system.
    # Do not symlink it from /etc/systemd/system: that shadows the packaged unit and points at a
    # path that does not exist. Enable and restart named directly.
    commands = [
        "mkdir -p /var/lib/named",
        "printf '%s\\n' {args} > /var/lib/named/{d}.lan".format(args=zone_printf_args, d=mydomain),
        "printf '%s\\n' {conf} > /etc/bind/named.conf.local".format(conf="'{}'".format(named_conf_line)),
        "chown -R bind:bind /var/lib/named",
        "chmod 0755 /var/lib/named",
        # Ubuntu's usr.sbin.named AppArmor profile allows zone data only under /var/lib/bind/**, not
        # under /var/lib/named/** (the NAMED_ZONE_DIR convention used elsewhere). Without an override,
        # named fails to load the zone with permission denied despite correct UNIX ownership. The
        # local/ drop-in is Ubuntu's supported override, so the profile stays enforced.
        "printf '%s\\n' '/var/lib/named/** rw,' > /etc/apparmor.d/local/usr.sbin.named",
        "apparmor_parser -r /etc/apparmor.d/usr.sbin.named",
        "systemctl enable --now named",
    ]
    runcmd_block = "\n".join("  - {}".format(yq(cmd)) for cmd in commands)

    # A top-level ssh_authorized_keys grants the key only to the distro default user (ubuntu), and
    # the image's login wrapper keeps root SSH blocked. Every SSH call this project makes assumes
    # root, so root is declared explicitly under users:, the same convention as template_user-data.
    return (
        "#cloud-config\n"
        "package_update: true\n"
        "packages:\n"
        "  - bind9\n"
        "users:\n"
        "  - default\n"
        "  - name: root\n"
        "    lock_passwd: false\n"
        "    ssh_authorized_keys:\n"
        "      - {key}\n"
        "runcmd:\n"
        "{runcmd_block}\n"
    ).format(key=root_ssh_key.strip(), runcmd_block=runcmd_block)


def ensure_cloud_dns_vm(backend, backend_name, root_ssh_key, mydomain, iso_image, lab_setup_path):
    """
    Idempotently ensures a small, cheap DNS-serving VM exists for this cloud backend, and returns its real IP.
    There is one DNS VM per backend, e.g. "lab-dns-aws", reused by every lab that uses that backend. It is not
    created per lab.

    Cloud nodes generally cannot reach automation.mydemo.lab's own BIND server, which sits behind the home lab's NAT
    and is not reachable from the internet. A cloud cluster therefore needs its own DNS server inside the same
    cloud network, and its nodes resolve each other through it. See provision_vm() in setup_vm.py for how this plugs
    into add_to_dns()'s remote_dns_servers mechanism.

    Known limitation: a freshly created cloud node does not point its own DNS resolution (/etc/resolv.conf or the
    provider's VPC DHCP option set) at this DNS VM. A second cloud node in the same lab therefore cannot resolve a
    first one by hostname until that wiring is added.

    With multiple cloud accounts (backend.account set, see resolve_cloud_account()), each account is an isolated
    cloud network, so the DNS VM is per account: "lab-dns-<backend>-<account>". The unnamed or "default" account
    keeps the plain "lab-dns-<backend>" name.

    Reuse and creation: the fixed name "lab-dns-<backend_name>[-<account>]" is looked up with vm_exists() and get_ip().
    If the VM does not exist, it is created with the same backend's create_vm(), at the smallest instance or plan size
    (1 vCPU and 512 MiB, enough for BIND) and with the ISO_IMAGE the calling lab already configured. No new required
    config key is added. copy_vm_image() needs iso_loc and vm_img_loc only for libvirt. Each cloud backend's
    copy_vm_image() validation is a no-op.

    Known limitation: querying this DNS VM's zone through its public IP from an external client, including
    automation.mydemo.lab, can return a root-zone NXDOMAIN, even though recursive queries through the same public IP
    work. Use the private IP or loopback address. The cause is not yet identified. See TODO.
    """
    acct = getattr(backend, "account", "") or ""
    if acct in ("", "default"):
        dns_vm_name = "lab-dns-{}".format(backend_name)
    else:
        dns_vm_name = "lab-dns-{}-{}".format(backend_name, acct)

    if backend.vm_exists(dns_vm_name):
        ip = backend.get_ip(dns_vm_name)
        if ip:
            log("- Reusing existing cloud DNS VM \"{}{}{}\" ({})".format(_RED, dns_vm_name, _RESET, ip))
            return ip
        die("cloud DNS VM '{}' exists on backend '{}' but reported no IP — check it manually "
            "before retrying".format(dns_vm_name, backend_name))

    log("- No cloud DNS VM found for backend \"{}{}{}\" — creating \"{}{}{}\"".format(
        _RED, backend_name, _RESET, _RED, dns_vm_name, _RESET))

    base = Path(lab_setup_path) / "cloud-init"
    base.mkdir(parents=True, exist_ok=True)
    (base / "{}_user-data".format(dns_vm_name)).write_text(
        _cloud_dns_vm_user_data(root_ssh_key, mydomain))

    backend.copy_vm_image(iso_image, dns_vm_name, 8, config_method="cloud-init")
    backend.push_provisioning_files(dns_vm_name, config_method="cloud-init")
    ip = backend.create_vm(
        dns_vm_name, 1, 512, 8, None,
        config_method="cloud-init", iso_image=iso_image, mymac=None,
    )
    if not ip:
        die("cloud DNS VM '{}' was created on backend '{}' but create_vm() reported no IP — "
            "cannot continue".format(dns_vm_name, backend_name))

    # create_vm() returns once the provider has assigned an IP, but cloud-init still needs time to
    # install and start bind9. Wait here until named answers, so add_to_dns()'s later SSH append
    # does not race it. Because the DNS VM is a secondary server, check=False would otherwise hide
    # that race completely.
    log("- Waiting for \"{}{}{}\" to finish installing and starting BIND".format(_RED, dns_vm_name, _RESET))
    waited = 0
    while waited < 240:
        check = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(ip), "systemctl is-active named"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if check.returncode == 0:
            break
        time.sleep(5)
        waited += 5
    else:
        die("cloud DNS VM '{}' ({}) never reported 'named' active within 240s — check it "
            "manually (SSH in and inspect cloud-init's own log, /var/log/cloud-init-output.log)"
            .format(dns_vm_name, ip))

    log("- Cloud DNS VM \"{}{}{}\" ready at {}".format(_RED, dns_vm_name, _RESET, ip))
    return ip


def _resolve_account_name(definition, config, vm_name):
    """
    Return the cloud_account name that applies to `vm_name`. An explicit "cloud_account" field (node, then common) is used
    if set. Otherwise auto-discovery checks the credentials files for a matching account, so an encrypted account is used
    even when the lab JSON does not name it. Auto-discovery runs only when the node's own "backend" field names a cloud
    backend. A libvirt or harvester node never scans the credentials directory.

    Returns "" when no account applies, in which case lab_creation.cfg is read directly. Never returns None.

    Dies if auto-discovery finds more than one credentials file for the provider. The ambiguity must be resolved with an
    explicit "cloud_account".
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    common_cfg = definition.get("common", {}) or {}
    account = node_cfg.get("cloud_account") or common_cfg.get("cloud_account") or ""
    if account:
        return account
    backend_name = node_cfg.get("backend") or common_cfg.get("backend") or config.get("BACKEND") or "libvirt"
    if backend_name not in CLOUD_BACKEND_NAMES:
        return ""
    found, matches = primary.find_cloud_account_for_cloudtype(backend_name, config=config)
    if found:
        return found
    if len(matches) > 1:
        die("VM '{}': backend '{}' has no explicit \"cloud_account\" set, and {} matching "
            "credentials files were found ({}) — set \"cloud_account\" explicitly to pick "
            "one".format(vm_name, backend_name, len(matches), ", ".join(matches)))
    return ""


def resolve_cloud_account(definition, config, vm_name):
    """
    Multiple cloud accounts, the same way KVM_HOSTS gives multiple hypervisors.

    An account is a file /etc/lab_creation/credentials/<name>.{yaml,json,cfg}
    (path configurable via lab_creation.cfg's CREDENTIALS_PATH), encrypted by
    default (see libs/crypto_store.py + scripts/setup_credentials.py), carrying
    a `cloudtype` (aws/gcp/…) plus that provider's usual connection keys
    (AWS_REGION, etc.) — see primary.load_cloud_account(). A node (or common)
    picks one with a "cloud_account": "<name>" field, exactly like
    "kvm_host": "<host>" — or one is auto-discovered (see
    _resolve_account_name()) if none is named explicitly.

    Returns (account_name, effective_config, cloudtype):
      - no account applies -> ("", config, None): today's behaviour,
        keys read straight from lab_creation.cfg.
      - resolved -> (name, config-with-the-account-file's-keys-layered-on-top, cloudtype).
    Dies (via primary) if the named/discovered account file is missing or has no cloudtype.
    """
    account = _resolve_account_name(definition, config, vm_name)
    if not account:
        return "", config, None
    acct = primary.load_cloud_account(account, config=config)
    cloudtype = acct.get("CLOUDTYPE", "")
    merged = dict(config)
    merged.update(acct)
    return account, merged, cloudtype


def effective_backend_name(definition, config, vm_name):
    """The backend name that actually applies to `vm_name`, honouring a
    cloud_account's cloudtype (which wins, making the `backend` field optional)
    before falling back to nodes[x].backend / common.backend / config["BACKEND"]
    / "libvirt" — the account may be named explicitly or auto-discovered (see
    _resolve_account_name()). Used by setup_vm.py / destroy_vm.py for their
    CLOUD_BACKEND_NAMES gate. Dies if a named/discovered cloud_account file is
    missing/invalid."""
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    common_cfg = definition.get("common", {}) or {}
    account = _resolve_account_name(definition, config, vm_name)
    if account:
        return primary.load_cloud_account(account, config=config).get("CLOUDTYPE", "")
    return node_cfg.get("backend") or common_cfg.get("backend") or config.get("BACKEND") or "libvirt"


def get_backend(definition, config, vm_name, for_existing=False, vm_img_loc=None,
                 iso_loc=None, lab_setup_path=None):
    """
    Resolve which backend a VM should use and return a ready instance,
    hiding host/cluster selection and connection-detail construction from
    the caller. Backend selection: a cloud_account's cloudtype (see
    resolve_cloud_account()) wins; else nodes[vm_name].backend, else
    common.backend, else config["BACKEND"], else "libvirt". Unknown name
    dies listing the known backends. The actual target-resolution work
    (which KVM host, which Harvester cluster) is delegated to the chosen
    backend's own resolve() classmethod — see VMBackend.resolve()'s
    docstring for why that split exists.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    common_cfg = definition.get("common", {}) or {}

    account, eff_config, cloudtype = resolve_cloud_account(definition, config, vm_name)
    explicit_backend = node_cfg.get("backend") or common_cfg.get("backend")
    if account and explicit_backend and explicit_backend != cloudtype:
        die("VM '{}': cloud_account '{}' is cloudtype '{}', but backend is set to '{}' — remove "
            "the backend field or make the two agree".format(
                vm_name, account, cloudtype, explicit_backend))

    backend_name = cloudtype or explicit_backend or eff_config.get("BACKEND") or "libvirt"

    backend_cls = BACKENDS.get(backend_name)
    if backend_cls is None:
        die("Unknown backend '{}' for VM '{}' — supported backends: {}".format(
            backend_name, vm_name, ", ".join(sorted(BACKENDS))))

    inst = backend_cls.resolve(definition, vm_name, eff_config, for_existing,
                                vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)
    inst.account = account
    return inst


def get_backend_for_account(account_name, config, vm_img_loc=None, iso_loc=None, lab_setup_path=None):
    """
    Resolve a backend from a named cloud account, independent of any lab node. overlay.ensure_overlay_hub() uses it,
    because the hub lives in its own account (OVERLAY_HUB_ACCOUNT), which may differ from any lab node's backend or
    cloud_account.

    Mirrors get_backend() without the per-node backend and cloud_account resolution. A synthetic single-node definition is
    enough, because each backend's resolve() classmethod reads only the account's merged config and uses vm_name for error
    messages.
    """
    acct = primary.load_cloud_account(account_name, config=config)
    cloudtype = acct.get("CLOUDTYPE", "")
    if not cloudtype:
        die("cloud account '{}' has no CLOUDTYPE set".format(account_name))
    backend_cls = BACKENDS.get(cloudtype)
    if backend_cls is None:
        die("cloud account '{}' has unknown CLOUDTYPE '{}' — supported backends: {}".format(
            account_name, cloudtype, ", ".join(sorted(BACKENDS))))

    merged = dict(config)
    merged.update(acct)
    vm_name = "lab-overlay-hub"
    inst = backend_cls.resolve({"nodes": {vm_name: {}}}, vm_name, merged, False,
                                vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)
    inst.account = account_name
    return inst, cloudtype
