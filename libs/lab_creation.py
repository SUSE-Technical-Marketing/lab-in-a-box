"""
lab_creation.py — VM management, DNS, Helm, SSH, templates, and variable loading.

Python equivalent of lab_creation.bash.

Typical usage:
    from lab_creation import (
        ssh_run, ssh_output, check_ssh_conn,
        load_vm_vars, section_vars,
        add_to_dns, del_from_dns,
        setup_helm, helm_repo_add,
        process_template,
    )
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import base64
import json
import math
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


# ── Output helpers ────────────────────────────────────────────────────────────

_RED    = "\033[1;91m"
_YELLOW = "\033[1;33m"
_ORANGE = "\033[1;38;5;208m"  # 256-colour orange; falls back to bold text on 16-colour terminals
_GREEN  = "\033[1;92m"
_WHITE  = "\033[1;97m"
_RESET  = "\033[0m"

_level = 0  # current indentation depth (mirrors $_lvl in bash)

# setup_lab.py's parallel VM-creation mode (2026-09-21) runs multiple nodes'
# whole provisioning flow on separate threads, all calling into log() via
# deeply-nested code (provision_vm, destroy_vm, DNS registration, ...) that
# was never written to pass an explicit level= through — it all relies on
# this ambient module-level _level. Under real concurrency, "_level += 1" /
# "_level -= 1" (scattered across ~20 call sites in this file and the
# scripts that drive it) is a non-atomic shared-state race: not just
# cosmetic interleaving, the indentation depth itself can end up
# permanently wrong for the rest of the run. Rather than rewrite every
# nested call site to thread an explicit level through (invasive, easy to
# miss one), a parallel worker sets a per-thread prefix instead — when set,
# it REPLACES the shared-_level-based indentation entirely for log() calls
# on that thread, sidestepping the race without touching any of those ~20
# sites or any nested caller.
_thread_local = threading.local()


def set_log_prefix(prefix):
    """Sets (or clears, with None) a per-thread log prefix — see the module-level
    comment above _thread_local. Call at the start/end of a parallel worker's own
    per-node body; every log() call made from that thread (including deep inside
    provision_vm/destroy_vm/DNS registration) picks it up automatically."""
    _thread_local.prefix = prefix


def log(msg, level=None):
    """Print an indented message. Uses the module-level _level if level is None
    AND no per-thread prefix is set (see set_log_prefix())."""
    prefix = getattr(_thread_local, "prefix", None)
    if prefix is not None:
        print("{}{}".format(prefix, msg))
        return
    indent = "  " * (_level if level is None else level)
    print(indent + msg)


def warn(msg):
    """Print an orange warning to stderr."""
    print("{}WARNING:{} {}".format(_ORANGE, _RESET, msg), file=sys.stderr)


def error(msg):
    """Print a red error to stderr WITHOUT exiting — for a failure that is real
    (not a warning) but that the caller has deliberately chosen to continue past,
    e.g. one node failing in a multi-node setup_lab run. Use die() when the whole
    process must stop."""
    print("{}ERROR:{} {}".format(_RED, _RESET, msg), file=sys.stderr)


def die(msg):
    """Print a red error and exit (mirrors fail_with_error)."""
    print("{}ERROR:{} {}".format(_RED, _RESET, msg), file=sys.stderr)
    raise SystemExit(1)


_DEBUG = False

_NOISY_RE = re.compile(
    r"(?i)\b(warn|warning|error|errno|fail|failed|denied|refused|"
    r"not found|no such|cannot|unable|traceback|fatal)\b")


def set_debug(on):
    """Turn debug output on/off (default off). When OFF, ssh_run() runs remote
    commands quietly and only prints their output if the command fails or the
    output looks like it contains a warning/error. When ON, every remote command
    streams its output live."""
    global _DEBUG
    _DEBUG = bool(on)


def debug_enabled():
    return _DEBUG


def debug(msg):
    """Print a debug line, only when debug is enabled."""
    if _DEBUG:
        print("{}DEBUG:{} {}".format(_WHITE, _RESET, msg))


def _looks_noisy(text):
    """True if `text` looks like it carries a warning or error worth showing
    even in non-debug mode."""
    return bool(text) and bool(_NOISY_RE.search(text))


# ── Lab definition preflight validation ───────────────────────────────────────

def _jq_or(value, default=""):
    """Mirrors jq's `//` operator: only None/False count as absent (empty string is truthy)."""
    return default if value is None or value is False else value


def _empty(value):
    """Mirrors bash `[[ -z "$val" ]]` applied to a jq `// ""`-defaulted value."""
    return value is None or value is False or value == ""


def yaml_scalar(value):
    """Render a Python value as a YAML scalar for hand-built YAML config
    blocks — bool/int/float unquoted, everything else double-quoted.
    Deliberately simple (flat scalars only, no nested structures) — same
    "operator pre-configures it, we don't own the semantics" stance as
    HARVESTER_NETWORK/Multus in libs/backends.py.

    A string value's own backslash/quote/newline characters are escaped
    per YAML's double-quoted-scalar rules — confirmed live 2026-09-05
    (first in scripts/setup_harvester_cluster.py, moved here 2026-09-05
    after finding the identical bug in prepare_install_iso()'s Ubuntu
    autoinstall cloud-config below) that without this, a value containing
    so much as an embedded quote silently corrupts the rendered YAML, and
    one with an embedded colon+newline can inject entirely new, unrelated
    top-level keys into the document.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = (str(value).replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r"))
    return '"{}"'.format(escaped)


def resolve_install_type(install_type, iso_image):
    """
    Auto-detect the installer flavour from the ISO filename when install_type is
    blank. Mirrors _resolve_install_type (bash).
    """
    itype = install_type or ""
    if not itype:
        iso_lower = (iso_image or "").lower()
        if re.search(r"sles|suse|opensuse|sl-micro|sle-micro", iso_lower):
            itype = "autoyast"
        elif re.search(r"rhel|centos|rocky|almalinux|fedora", iso_lower):
            itype = "kickstart"
        elif re.search(r"ubuntu", iso_lower):
            itype = "autoinstall"
        elif re.search(r"debian", iso_lower):
            itype = "preseed"
        else:
            itype = "autoyast"
    return itype


# Vendor-documented (or live-confirmed) provisioning-method support per image
# family, matched against ISO_IMAGE by filename — used only to warn, never to
# block: an image matching nothing below is left alone, since this is a
# best-effort heuristic, not a guarantee (same convention as
# resolve_install_type() above).
#
# Sources:
#   - SLES Minimal VM variants — https://documentation.suse.com/smart/virtualization-cloud/html/minimal-vm/index.html:
#     the plain "kvm-and-xen" variant (this project's own ISO_LOC images,
#     e.g. "SLES15-SP6-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2") uses JeOS
#     Firstboot — not cloud-init and not Ignition. Only the separate "for
#     OpenStack" variant ships cloud-init, which is why this pattern
#     requires "kvm-and-xen" specifically rather than matching any
#     "Minimal-VM" filename. Confirmed live 2026-09-02 via virt-ls/virt-cat
#     against two real such images on nuc6: neither has a cloud-init binary
#     or systemd unit. openSUSE Leap ships an identically-named
#     "openSUSE-Leap-15.x-Minimal-VM.x86_64-kvm-and-xen.qcow2" image from the
#     same build pipeline, so the same pattern (not distro-specific) covers
#     it too rather than needing a separate Leap entry.
#   - SLE Micro (5.x/6.x) Ignition+Combustion — https://documentation.suse.com/sle-micro/5.2/html/SLE-Micro-all/cha-images-combustion.html
#   - RHEL/CentOS/CentOS Stream/Rocky Linux/AlmaLinux 8, 9, 10 "GenericCloud"
#     images ship cloud-init pre-installed and enabled —
#     https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/configuring_and_managing_cloud-init_for_rhel_9/introduction-to-cloud-init_cloud-content ,
#     https://wiki.almalinux.org/cloud/Generic-cloud.html ,
#     https://docs.rockylinux.org/10/guides/virtualization/cloud-init/01_fundamentals/
#     (a plain/minimal ISO install of any of these does NOT have cloud-init
#     by default — this heuristic assumes the pre-built qcow2/kvm image this
#     project actually deploys via config_method=cloud-init, matching how
#     ISO_IMAGE is used everywhere else in this project).
#   - CentOS/RHEL 7: no positive vendor confirmation either way for
#     cloud-init; this project's own CLAUDE.md already recommends
#     virt_customize for it, so it's scoped that narrowly here too.
#   - CentOS/RHEL 6: EOL November 2020; upstream cloud-init packaging for
#     el6 was dropped around 2019 — https://forum.proxmox.com/threads/cloud-init-build-on-centos-6.55832/
#   - Debian 10+ official cloud images — https://cloud.debian.org/images/cloud/:
#     the "generic" variant ships cloud-init; the "nocloud" variant
#     deliberately does NOT run cloud-init at all (boots straight to a root
#     prompt), so it's matched separately, before the general Debian rule.
#     That pattern requires "debian" alongside "nocloud" (not just "nocloud"
#     alone) — confirmed live 2026-09-03 that a bare "nocloud" substring
#     match false-positived on an unrelated image, Alibaba's own
#     "aliyun_2_1903_x64_20G_nocloud_alibase_*.qcow2" naming (its "nocloud"
#     means something else in Alibaba's own build pipeline — the image
#     genuinely has cloud-init, confirmed via virt-cat on the real file).
#   - Fedora Cloud Base images (28+) ship cloud-init —
#     https://fedoramagazine.org/setting-up-a-vm-on-fedora-server-using-cloud-images-and-virt-install-version-3/
#   - Ubuntu server/cloud images have shipped cloud-init since 18.04 LTS (and
#     informally earlier) — https://help.ubuntu.com/community/CloudInit
#   - Alibaba Cloud Linux (Aliyun Linux) 2/3 "alibase" images ship cloud-init
#     — confirmed live 2026-09-03 via virt-cat against a real
#     aliyun_2_1903_x64_20G_nocloud_alibase_20230103.qcow2.
#
# "virt_customize" edits the qcow2 filesystem directly (no in-guest agent
# required), so it works against essentially any image — included in every
# entry below and never itself a reason to warn.
_IMAGE_CONFIG_METHOD_SUPPORT = (
    (r"el6|rhel-?6|centos-?6", "RHEL/CentOS 6 (EOL — cloud-init not packaged upstream)", {"virt_customize"}),
    (r"el7|rhel-?7|centos-?7", "RHEL/CentOS 7", {"virt_customize"}),
    (r"sle?-?micro", "SLE Micro 5.x/6.x", {"", "virt_customize"}),
    (r"minimal-vm.*kvm-and-xen", "SLES 15/16 or openSUSE Leap 15.x Minimal VM (KVM/Xen)", {"virt_customize"}),
    (r"debian.*nocloud|nocloud.*debian", "Debian 10+ (nocloud variant)", {"virt_customize"}),
    (r"debian", "Debian 10+ (generic cloud image)", {"cloud-init", "virt_customize"}),
    (r"fedora", "Fedora 28+ (Cloud Base image)", {"cloud-init", "virt_customize"}),
    (r"rocky", "Rocky Linux 8/9/10 (GenericCloud image)", {"cloud-init", "virt_customize"}),
    (r"alma", "AlmaLinux 8/9/10 (GenericCloud image)", {"cloud-init", "virt_customize"}),
    (r"rhel|centos", "RHEL/CentOS(-Stream) 8/9/10", {"cloud-init", "virt_customize"}),
    (r"ubuntu", "Ubuntu 18.04+ (cloud/server image)", {"cloud-init", "install_iso", "virt_customize"}),
    (r"aliyun|alinux|alibaba", "Alibaba Cloud Linux 2/3 (alibase image)", {"cloud-init", "virt_customize"}),
)


def _supported_config_methods(iso_image):
    """
    Best-effort (ISO_IMAGE filename) -> (label, supported config_methods)
    lookup — see _IMAGE_CONFIG_METHOD_SUPPORT's sources above. Patterns are
    checked in order (most specific first: the versioned RHEL/CentOS 6/7
    patterns and the SLE Micro/Debian-nocloud patterns before their broader
    families' rules), first match wins. Returns None when nothing matches —
    an unrecognized image is left alone, never warned about.
    """
    iso_lower = (iso_image or "").lower()
    for pattern, label, supported in _IMAGE_CONFIG_METHOD_SUPPORT:
        if re.search(pattern, iso_lower):
            return label, supported
    return None


def validate_lab_definition(definition, config, iso_loc, lab_setup_path, target_node=None,
                            vm_img_loc=None, issues_out=None):
    """
    Preflight-validate an already-loaded lab definition. Mirrors
    validate_lab_definition (bash), generalized to resolve a KVM host per
    node (new in the python port — bash only ever had one hypervisor).

    Call before setup_lab.py / setup_vm.py begins work. Prints a full issue
    report (errors + warnings) and returns True iff there were no errors —
    mirrors the bash function's 0/1 return code (warnings never block).

    Args:
        definition     : the lab definition, already loaded by the caller
                          (primary.load_definition()) — a LabDefinition (see
                          primary.py), so it already knows its own source
                          path (definition.source_path), used below for the
                          preflight banner and for delegating to each
                          addon's own `install_<addon> --validate <path>`
                          subprocess (a separate process, which necessarily
                          re-reads the file itself — that's unrelated to,
                          and not fixed by, this function not re-reading it
                          in-process). No separate path parameter needed:
                          this function validates definition's CONTENT and
                          never re-reads or re-parses the file itself (the
                          bash version re-read it here since it had no
                          in-memory representation to reuse; the python
                          port doesn't need to repeat that I/O, or even
                          accept the path twice over).
        config         : lab_creation.cfg dict (REMOTE_HOST, VIRT_SRV, KVM_HOSTS, …) —
                          used to resolve_kvm_host() each node, same resolution
                          that will place it when actually created.
        iso_loc         : source image directory on the hypervisor (ISO_LOC) —
                          identical path on every KVM host, by design.
        lab_setup_path  : ignition/combustion/cloud-init/install_iso template tree
                          on the automation VM (LAB_SETUP_PATH).
        target_node     : optional single VM hostname to restrict per-node/per-
                          kcluster checks to (mirrors the optional 2nd bash arg).
        vm_img_loc      : passed through to resolve_kvm_host()'s select_kvm_host()
                          resource probing when KVM_HOSTS has more than one host.

    NOTE: the bash version also computes a `_known_addons` list (via `command -v
    install_rancher` + a directory listing, falling back to `compgen -c`) that
    is assigned but never read anywhere in the function — verified with grep,
    dropped here as dead code with no behavioural effect.

    NOTE: the bash version iterates nodes/kclusters via `while read <<< "$var"`
    (a here-string), which runs the loop body once with an empty value even
    when $var is empty — bash:117 lacked the `[[ -z ... ]] && continue` guard
    the other two here-string loops in this function already had, so a lab
    with an empty "nodes" object would get a spurious "nodes.: 'myip' is
    required" error. Fixed in both bash (added the guard) and here (a plain
    Python loop over an empty list simply doesn't iterate).
    """
    issues = []
    counts = {"errors": 0, "warnings": 0}

    def err(msg):
        issues.append("  {}[ERROR]{} {}".format(_RED, _RESET, msg))
        counts["errors"] += 1

    def warn(msg):
        issues.append("  {}[WARN]{}  {}".format(_ORANGE, _RESET, msg))
        counts["warnings"] += 1

    if target_node:
        print("{}── Preflight: validating '{}' for node '{}' ──{}".format(
            _WHITE, definition.source_path, target_node, _RESET))
    else:
        print("{}── Preflight: validating '{}' ──{}".format(_WHITE, definition.source_path, _RESET))

    # ── 1. Required top-level sections ────────────────────────────────────────
    if "nodes" not in definition:
        err("Missing required section: 'nodes'")
    if "common" not in definition:
        err("Missing required section: 'common'")

    common = definition.get("common") or {}
    nodes = definition.get("nodes") or {}
    kclusters = definition.get("kclusters") or {}

    # ── 2. common: required fields ────────────────────────────────────────────
    # common.ISO_IMAGE is only required when some node doesn't supply its own
    # override (nodes.<name>.ISO_IMAGE) — a lab where every node pins its own
    # image is valid and never needs a common default at all.
    iso = _jq_or(common.get("ISO_IMAGE"))
    if _empty(iso):
        nodes_missing_iso = [
            n for n, cfg in (definition.get("nodes") or {}).items()
            if _empty(_jq_or((cfg or {}).get("ISO_IMAGE")))
        ]
        if nodes_missing_iso:
            err("common.ISO_IMAGE is required (or set ISO_IMAGE per-node) — missing for: {}".format(
                ", ".join(sorted(nodes_missing_iso))))

    for req in ("VM_MEM", "VM_DSK", "VM_CPU"):
        if _empty(_jq_or(common.get(req))):
            err("common.{} is required".format(req))

    dsk_bus = _jq_or(common.get("VM_DSK_BUS"))
    if not _empty(dsk_bus) and dsk_bus not in ("virtio", "scsi", "sata", "usb", "ide"):
        err("common.VM_DSK_BUS '{}' is invalid — must be one of: virtio, scsi, sata, usb, ide".format(dsk_bus))

    net_model = _jq_or(common.get("VM_NET_MODEL"))
    if not _empty(net_model) and net_model not in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci"):
        err("common.VM_NET_MODEL '{}' is invalid — must be one of: virtio, e1000, e1000e, rtl8139, vmxnet3, ne2k_pci".format(net_model))

    from backends import BACKENDS, CLOUD_BACKEND_NAMES
    from primary import try_load_cloud_account
    common_backend = _jq_or(common.get("backend"))
    if not _empty(common_backend) and common_backend not in BACKENDS:
        err("common.backend '{}' is invalid — must be one of: {}".format(
            common_backend, ", ".join(sorted(BACKENDS))))

    # cloud_account (common or per-node): multiple cloud accounts, like KVM_HOSTS
    # for hypervisors. Validated per node below; the effective cloudtype is cached
    # here so section 6's libvirt-vs-cloud split can reuse it.
    node_account_cloudtype = {}

    def _account_cloudtype_for(node):
        """The cloudtype a node's cloud_account resolves to, or None. Errors are
        reported once, in the per-node loop below — this only returns the type."""
        if node in node_account_cloudtype:
            return node_account_cloudtype[node]
        acct_name = _jq_or((nodes.get(node) or {}).get("cloud_account")) or _jq_or(common.get("cloud_account"))
        ct = None
        if not _empty(acct_name):
            data, _e = try_load_cloud_account(acct_name, config=config)
            if data:
                ct = data.get("CLOUDTYPE") or None
        node_account_cloudtype[node] = ct
        return ct

    # ── 3. Per-node checks ─────────────────────────────────────────────────────
    seen_ips = set()
    seen_macs = set()
    kclusters_defined = set(kclusters.keys())

    nodes_to_check = [target_node] if target_node else list(nodes.keys())

    # Resolved lazily per node and cached — section 7 (image checks) reuses
    # the same resolution. A die() from resolve_kvm_host()/select_kvm_host()
    # (no host has enough resources, or bad config) is caught here rather
    # than aborting validation outright, so it can be reported as one more
    # preflight error alongside everything else.
    node_hosts = {}

    def resolved_host_for(node):
        if node not in node_hosts:
            try:
                node_hosts[node] = resolve_kvm_host(definition, node, config, vm_img_loc)
            except SystemExit:
                node_hosts[node] = (None, None)
        return node_hosts[node]

    for node in nodes_to_check:
        node_cfg = nodes.get(node) or {}
        myip = _jq_or(node_cfg.get("myip"))
        mymac = _jq_or(node_cfg.get("mymac"))
        kcluster = _jq_or(node_cfg.get("kcluster"))

        # A cloud-backend node's real IP is only known once the provider assigns it at create
        # time — myip is deliberately left empty for one in the JSON (see README), not a missing-
        # field mistake. Added 2026-09-09 alongside create_vm()'s own real-IP return contract —
        # see TODO. Every other backend (libvirt/Harvester) keeps requiring it exactly as before.
        # cloud_account: validate the referenced account file, and let its
        # cloudtype stand in for the `backend` field (which becomes optional).
        cloud_account = _jq_or(node_cfg.get("cloud_account")) or _jq_or(common.get("cloud_account"))
        account_cloudtype = None
        if not _empty(cloud_account):
            acct_data, acct_err = try_load_cloud_account(cloud_account, config=config)
            if acct_err:
                err("nodes.{}: {}".format(node, acct_err))
            else:
                account_cloudtype = acct_data.get("CLOUDTYPE") or ""
                if account_cloudtype not in CLOUD_BACKEND_NAMES:
                    err("nodes.{}: cloud_account '{}' has cloudtype '{}' — must be one of: {}".format(
                        node, cloud_account, account_cloudtype, ", ".join(sorted(CLOUD_BACKEND_NAMES))))
                explicit_backend = _jq_or(node_cfg.get("backend")) or common_backend
                if not _empty(explicit_backend) and explicit_backend != account_cloudtype:
                    err("nodes.{}: cloud_account '{}' is cloudtype '{}' but backend is set to '{}' "
                        "— remove the backend field or make them agree".format(
                            node, cloud_account, account_cloudtype, explicit_backend))
        node_account_cloudtype[node] = account_cloudtype or None

        node_backend_eff = (account_cloudtype or _jq_or(node_cfg.get("backend")) or common_backend
                            or _jq_or(config.get("BACKEND")) or "libvirt")
        is_cloud_node = node_backend_eff in CLOUD_BACKEND_NAMES

        if _empty(myip) and not is_cloud_node:
            err("nodes.{}: 'myip' is required".format(node))

        if not _empty(myip):
            if myip in seen_ips:
                err("nodes.{}: IP {} is already assigned to another node".format(node, myip))
            else:
                seen_ips.add(myip)

        if not _empty(mymac):
            mymac_l = str(mymac).lower()
            if mymac_l in seen_macs:
                err("nodes.{}: MAC {} is duplicated within this JSON".format(node, mymac))
            else:
                seen_macs.add(mymac_l)

        if not _empty(kcluster) and kcluster not in kclusters_defined:
            err("nodes.{}: references kcluster '{}' which is not defined in 'kclusters'".format(node, kcluster))

        node_backend = _jq_or(node_cfg.get("backend"))
        if not _empty(node_backend) and node_backend not in BACKENDS:
            err("nodes.{}: backend '{}' is invalid — must be one of: {}".format(
                node, node_backend, ", ".join(sorted(BACKENDS))))

        # A per-node "config_method": "" is a meaningful, explicit override
        # (bash/CLAUDE.md's own convention: empty string IS the value that
        # selects Ignition+Combustion) — it must win over a non-empty
        # common.config_method, not be treated as "unset" and fall through
        # to it. _empty() can't make that distinction (both "absent" and
        # "explicitly empty" look identical after it), so this checks key
        # presence directly instead — matching load_vm_vars()'s own plain
        # per-node-always-overwrites merge (the actual runtime behavior).
        # Confirmed live 2026-09-02: without this, a node explicitly opting
        # back into Ignition+Combustion under a cloud-init-default `common`
        # silently kept inheriting "cloud-init" for every check below.
        if "config_method" in node_cfg:
            eff_config_method = _jq_or(node_cfg.get("config_method"))
        else:
            eff_config_method = _jq_or(common.get("config_method"))

        # Mirrors scripts/lab_schema's config_method enum. An unrecognized
        # value (e.g. a "virt_customize" typo'd as "virt-customize") isn't
        # rejected by create_vm() either — none of its config_method
        # branches match, so it silently skips virt-install entirely and
        # never defines the VM at all, with no error anywhere. Confirmed
        # live 2026-09-02 against a real lab.json with exactly this typo.
        if not _empty(eff_config_method) and eff_config_method not in (
                "cloud-init", "virt_customize", "install_iso"):
            err("nodes.{}: config_method '{}' is invalid — must be one of: "
                "\"\" (Ignition+Combustion), cloud-init, virt_customize, install_iso".format(
                    node, eff_config_method))

        node_iso = _jq_or(node_cfg.get("ISO_IMAGE"))
        eff_iso = node_iso if not _empty(node_iso) else iso

        # An ISO_IMAGE ending in ".iso" is a genuine installer medium, not a
        # pre-built bootable disk — every config_method except "install_iso"
        # treats ISO_IMAGE as an existing disk to `cp` + `qemu-img resize`
        # (copy_vm_image()), which fails hard on real ISO9660 content
        # regardless of distro: reported live 2026-09-02 as "qemu-img: ...
        # Image is not in qcow2 format" for an Ubuntu live-server .iso used
        # with the inherited config_method="cloud-init" default. This is a
        # deterministic crash, not a heuristic, so it's an error rather than
        # a warning — and it takes priority over (suppresses) the softer
        # image/method compatibility warning below, which would otherwise
        # also fire and just add noise on top of the real problem.
        iso_is_installer_medium = not _empty(eff_iso) and str(eff_iso).lower().endswith(".iso")
        if iso_is_installer_medium and (eff_config_method or "") != "install_iso":
            err("nodes.{}: ISO_IMAGE '{}' is an installer ISO but config_method is '{}' — "
                "only config_method=\"install_iso\" boots an ISO directly (--cdrom + a fresh "
                "disk); every other config_method copies/resizes it as if it were an existing "
                "disk image and will fail.".format(
                    node, eff_iso, eff_config_method or "\"\" (Ignition+Combustion)"))

        # Only run the image/method compatibility check when config_method
        # itself is one of the known-valid values — an already-invalid value
        # (caught above) would otherwise also get a redundant "unsupported"
        # warning on top of the "invalid" error. Same reasoning for the
        # installer-ISO mismatch just above.
        cm_valid = (_empty(eff_config_method) or eff_config_method in (
            "cloud-init", "virt_customize", "install_iso")) and not (
                iso_is_installer_medium and (eff_config_method or "") != "install_iso")
        match = _supported_config_methods(eff_iso) if (cm_valid and not _empty(eff_iso)) else None
        if match:
            label, supported = match
            cm_norm = eff_config_method if not _empty(eff_config_method) else ""
            if cm_norm not in supported:
                warn("nodes.{}: config_method '{}' is likely unsupported on ISO_IMAGE '{}' "
                     "(detected as {}) — the vendor-documented/confirmed method(s) for this "
                     "image family are: {}. This is a best-effort filename heuristic, not a "
                     "guarantee — some images may genuinely differ from their family's norm.".format(
                         node, cm_norm or "\"\" (Ignition+Combustion)", eff_iso, label,
                         ", ".join(sorted(m or "\"\" (Ignition+Combustion)" for m in supported))))

        # Existing (pre-provisioned) nodes are never created/destroyed by
        # this tool, so hypervisor-existence and image checks don't apply —
        # the only thing that matters is that the host is actually reachable.
        # See targets.py's module docstring for why "existing" covers both
        # baremetal and an already-running VM this tool didn't create.
        from targets import is_existing_node, check_ssh_only_reachability
        if is_existing_node(node_cfg):
            if not _empty(myip) and not check_ssh_only_reachability(node):
                err("nodes.{}: marked \"existing\" but not reachable via SSH".format(node))
            continue

        # Hypervisor: VM name must not already exist on the host it would be
        # (re)created on
        node_host, node_virt_srv = resolved_host_for(node)
        if node_virt_srv:
            dominfo = run_libvirt_tool(
                "virsh", node_host, node_virt_srv, ["dominfo", node],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if dominfo.returncode == 0:
                warn("nodes.{}: a VM with this name already exists on KVM host '{}' "
                     "(will be destroyed and recreated)".format(node, node_host))

        # IP reachability (informational)
        if not _empty(myip):
            ping = subprocess.run(["ping", "-c1", "-W1", str(myip)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ping.returncode == 0:
                warn("nodes.{}: IP {} is currently responding to ping — "
                     "something may already be using it".format(node, myip))

    # ── 4. kcluster checks ─────────────────────────────────────────────────────
    if target_node:
        node_kcluster = _jq_or((nodes.get(target_node) or {}).get("kcluster"))
        clusters_to_check = [] if _empty(node_kcluster) else [node_kcluster]
    else:
        clusters_to_check = list(kclusters.keys())

    for clu in clusters_to_check:
        clu_cfg = kclusters.get(clu) or {}
        ctype = _jq_or(clu_cfg.get("clu_type"))
        crel = _jq_or(clu_cfg.get("clu_rel"))
        cdomain = _jq_or(clu_cfg.get("mydomain"))

        if _empty(ctype):
            err("kclusters.{}: 'clu_type' is required (rke2 or k3s)".format(clu))
        if _empty(crel):
            err("kclusters.{}: 'clu_rel' is required (e.g. stable)".format(clu))
        if _empty(cdomain):
            err("kclusters.{}: 'mydomain' is required".format(clu))

        from apps import addon_entry_name
        for addon_entry in clu_cfg.get("addons") or []:
            addon = addon_entry_name(addon_entry)
            if shutil.which("install_{}".format(addon)) is None:
                err("kclusters.{}: addon '{}' — script 'install_{}' not found in PATH".format(clu, addon, addon))
                continue
            from apps import load_plugin, requirement_issue
            from targets import TARGET_CONTAINER
            issue = requirement_issue(load_plugin(addon), TARGET_CONTAINER, clu_type=ctype)
            if issue:
                err("kclusters.{}: addon '{}' {}".format(clu, addon, issue))

    # Per-VM addon scripts must also be present
    for node in nodes_to_check:
        node_cfg = nodes.get(node) or {}
        from apps import addon_entry_name
        for addon_entry in node_cfg.get("addons") or []:
            addon = addon_entry_name(addon_entry)
            if shutil.which("install_{}".format(addon)) is None:
                err("nodes.{}: addon '{}' — script 'install_{}' not found in PATH".format(node, addon, addon))
                continue
            from apps import load_plugin, requirement_issue
            from targets import node_kind
            issue = requirement_issue(load_plugin(addon), node_kind(definition, node))
            if issue:
                err("nodes.{}: addon '{}' {}".format(node, addon, issue))

    # ── 5. Per-addon field validation — delegate to each install script ───────
    # Each install_* script supports --validate <json> and exits non-zero
    # with [ERROR] lines if its own fields are invalid or missing.
    from apps import addon_entry_name
    if target_node:
        node_kcluster = _jq_or((nodes.get(target_node) or {}).get("kcluster"))
        addon_list = list((nodes.get(target_node) or {}).get("addons") or [])
        if not _empty(node_kcluster):
            addon_list += list((kclusters.get(node_kcluster) or {}).get("addons") or [])
    else:
        addon_list = []
        for clu_cfg in kclusters.values():
            addon_list += list((clu_cfg or {}).get("addons") or [])
        for node_cfg in nodes.values():
            addon_list += list((node_cfg or {}).get("addons") or [])
    all_addons = sorted(set(addon_entry_name(e) for e in addon_list))

    for addon in all_addons:
        exe = shutil.which("install_{}".format(addon))
        if not exe:
            continue
        result = subprocess.run([exe, "--validate", str(definition.source_path)], capture_output=True, text=True)
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            for line in combined.splitlines():
                err("addon '{}': {}".format(addon, re.sub(r"^\[ERROR\]\s*", "", line)))

    # ── 6. Hypervisor: source image exists ────────────────────────────────────
    # Per-node/per-host: with multiple KVM hosts a node's image needs to
    # exist on the specific host it resolves to (ISO_LOC is the same path on
    # every host by design, but the file itself only needs to be there via
    # setup_kvm_node's storage-sharing step — checked per host, not assumed).
    if _empty(iso_loc):
        err("ISO_LOC is not set — check /etc/lab_creation.defaults")

    hv_ssh_ok = {}  # host -> bool, cached so each distinct host is only SSH-tested once

    def hv_reachable(host):
        if host not in hv_ssh_ok:
            ssh_test = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=5", "-q", "root@{}".format(host), "echo ok"],
                capture_output=True, text=True,
            )
            ok = (ssh_test.stdout or "").strip() == "ok"
            if not ok:
                err("Cannot reach KVM host '{}' via SSH — image checks skipped ({})".format(
                    host, ((ssh_test.stdout or "") + (ssh_test.stderr or "")).strip()))
            hv_ssh_ok[host] = ok
        return hv_ssh_ok[host]

    def check_image_on_hv(img, label, host):
        if _empty(img) or _empty(iso_loc) or not host or not hv_reachable(host):
            return
        test = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", "-q", "root@{}".format(host),
             "test -f '{}/{}'".format(iso_loc, img)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if test.returncode != 0:
            avail_proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-q",
                 "root@{}".format(host),
                 "ls '{}/'*.qcow2 2>/dev/null | xargs -n1 basename".format(iso_loc)],
                capture_output=True, text=True,
            )
            avail = "  ".join((avail_proc.stdout or "").splitlines()[:10])
            suffix = " — available: {}".format(avail) if avail else ""
            err("{}: image '{}' not found at {}:{}/{}{}".format(label, img, host, iso_loc, img, suffix))

    from targets import is_existing_node as _is_existing_node

    def _uses_libvirt(node):
        # ISO_IMAGE existence only means anything against a libvirt
        # hypervisor's own ISO_LOC — non-libvirt backends (e.g. Harvester,
        # any cloud) resolve images by name in their own environment instead
        # and never touch this path at all. Confirmed live (2026-08-29):
        # without this, a Harvester-backed node failed preflight over an
        # ISO_IMAGE that was never supposed to exist on any KVM hypervisor.
        if _account_cloudtype_for(node):
            return False  # a cloud_account always means a cloud backend
        node_backend = _jq_or((nodes.get(node) or {}).get("backend")) or _jq_or(common.get("backend"))
        return _empty(node_backend) or node_backend == "libvirt"

    created_nodes_to_check = [n for n in nodes_to_check if not _is_existing_node(nodes.get(n) or {})]
    libvirt_nodes_to_check = [n for n in created_nodes_to_check if _uses_libvirt(n)]

    if not _empty(iso_loc):
        hosts_used = {resolved_host_for(node)[0] for node in libvirt_nodes_to_check} - {None}
        if hosts_used:
            for host in hosts_used:
                check_image_on_hv(iso, "common.ISO_IMAGE", host)
        for node in libvirt_nodes_to_check:
            node_host, _ = resolved_host_for(node)
            node_iso = _jq_or((nodes.get(node) or {}).get("ISO_IMAGE"))
            if not _empty(node_iso):
                check_image_on_hv(node_iso, "nodes.{}.ISO_IMAGE".format(node), node_host)

    # ── 6b. Hypervisor: VM_DSK must not be smaller than the source image ──────
    # `qemu-img resize` (run at copy time by every non-install_iso config_method)
    # fails outright if asked to shrink an image below its own virtual size —
    # reported live as "qemu-img: Use the --shrink option to perform a shrink
    # operation." / a hard provisioning error for a node with a too-small VM_DSK.
    # Rather than let that surface mid-run, detect it here: bump the node's
    # VM_DSK up to the image's real virtual size (in memory only — the on-disk
    # lab file is untouched) and record a warning so the summary shows it.
    def check_min_disk_for_node(node):
        node_cfg = nodes.get(node) or {}
        img = _jq_or(node_cfg.get("ISO_IMAGE")) or iso
        host, _ = resolved_host_for(node)
        if _empty(img) or _empty(iso_loc) or not host or not hv_reachable(host):
            return
        try:
            req_gib = int(str(node_cfg.get("VM_DSK", common.get("VM_DSK", "")) or "").strip())
        except (TypeError, ValueError):
            return  # missing/invalid VM_DSK is already an [ERROR] from section 2
        info = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", "-q", "root@{}".format(host),
             "qemu-img info --output=json '{}/{}'".format(iso_loc, img)],
            capture_output=True, text=True,
        )
        if info.returncode != 0:
            return  # can't introspect it (no qemu-img, odd format) — best-effort only
        try:
            vsize = int(json.loads(info.stdout).get("virtual-size", 0))
        except (ValueError, TypeError, AttributeError):
            return
        if vsize <= 0:
            return
        img_gib = int(math.ceil(vsize / (1024 ** 3)))
        if req_gib < img_gib:
            warn("nodes.{}.VM_DSK is {} GiB but its source image '{}' is {} GiB — "
                 "raising VM_DSK to {} GiB (a smaller disk would fail at qemu-img resize)".format(
                     node, req_gib, img, img_gib, img_gib))
            if isinstance(nodes.get(node), dict):
                nodes[node]["VM_DSK"] = str(img_gib)

    if not _empty(iso_loc):
        for node in libvirt_nodes_to_check:
            check_min_disk_for_node(node)

    # ── 7. Local: provisioning templates ──────────────────────────────────────
    # Only check the config methods actually used in this definition
    needs_ign_cmb = False
    needs_cloud_init = False
    needs_install_iso = False
    for node_cfg in nodes.values():
        cm = _jq_or((node_cfg or {}).get("config_method"))
        if cm == "cloud-init":
            needs_cloud_init = True
        elif cm == "install_iso":
            needs_install_iso = True
        elif cm in ("virt_customize", "iso-cloud-init"):
            pass
        else:
            needs_ign_cmb = True

    if needs_ign_cmb:
        if not Path("{}/ignition/template".format(lab_setup_path)).is_file():
            err("Ignition template not found: {}/ignition/template".format(lab_setup_path))
        if not Path("{}/combustion/template".format(lab_setup_path)).is_file():
            err("Combustion template not found: {}/combustion/template".format(lab_setup_path))

    if needs_cloud_init:
        for tpl in ("user-data", "network-config", "meta-data"):
            if not Path("{}/cloud-init/template_{}".format(lab_setup_path, tpl)).is_file():
                err("cloud-init template not found: {}/cloud-init/template_{}".format(lab_setup_path, tpl))

    if needs_install_iso:
        itype = _jq_or(common.get("install_type"))
        if not _empty(itype) and itype not in ("autoyast", "kickstart", "preseed", "autoinstall"):
            err("common.install_type '{}' is invalid — must be: autoyast, kickstart, preseed, or autoinstall".format(itype))
        eff_itype = resolve_install_type("" if _empty(itype) else itype, iso)
        if not Path("{}/install_iso/template_{}".format(lab_setup_path, eff_itype)).is_file():
            err("install_iso template not found: {}/install_iso/template_{}".format(lab_setup_path, eff_itype))

    # ── Report ─────────────────────────────────────────────────────────────────
    if issues:
        print("\n".join(issues))

    # Hand the raw issue lines back to a caller that wants to re-surface them
    # later (setup_lab.py folds these into its end-of-run summary).
    if issues_out is not None:
        issues_out.extend(issues)

    if counts["errors"] > 0:
        print("{}✗ Preflight FAILED{} — {} error(s), {} warning(s). Fix the above before proceeding.".format(
            _RED, _RESET, counts["errors"], counts["warnings"]))
        return False
    else:
        print("{}✓ Preflight passed{} — 0 errors, {} warning(s).".format(_GREEN, _RESET, counts["warnings"]))
        return True


# ── MAC address handling ────────────────────────────────────────────────────

def _list_domain_macs(virt_srv, remote_host=None):
    """
    Returns (all_domain_lines, mac_by_domain) for the hypervisor at virt_srv.
    Thin wrapper — body moved to backends.LibvirtBackend.list_used_macs().

    remote_host is optional and defaults to None for backward compatibility
    with every existing caller — it's only actually needed by
    run_libvirt_tool()'s SSH fallback, when no local virsh binary exists
    (see its own docstring); every environment with a local virsh (the bare
    automation VM host, unchanged) never touches it.
    """
    from backends import LibvirtBackend
    return LibvirtBackend(virt_srv, remote_host=remote_host).list_used_macs()


def _generate_unused_mac(used_macs):
    """
    Generate a random locally-administered unicast MAC not present in used_macs
    (an iterable of lower-case MAC strings). Mirrors _generate_unused_mac (bash).

    NOTE: uses Python's random module rather than bash's $RANDOM — different
    PRNG, but the same algorithm (byte0 = random & 0xFC | 0x02, bytes 1-5
    uniform random) and the same guarantee (result not in used_macs). Bit-exact
    output parity between bash and python runs was never possible here since
    they use unrelated PRNGs; functional parity (a valid, unused, locally-
    administered MAC) is what's preserved.
    """
    used = set(used_macs)
    while True:
        b0 = "{:02x}".format((random.randint(0, 255) & 0xFC) | 0x02)
        candidate = "{}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(
            b0, random.randint(0, 255), random.randint(0, 255),
            random.randint(0, 255), random.randint(0, 255), random.randint(0, 255),
        )
        if candidate not in used:
            return candidate


# ── SSH helpers ───────────────────────────────────────────────────────────────

_SSH_BASE = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-q"]


def ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    """
    Run a shell command on a remote host via SSH (root by default).

    Args:
        hostname   : Target host (IP or FQDN).
        cmd        : Shell command string to execute remotely.
        check      : Raise RuntimeError on non-zero exit code.
        input_text : Text to send to the remote command's stdin.
        capture    : If True, capture stdout+stderr instead of streaming.
        user       : Remote SSH user — override when the target doesn't
                     allow root login (e.g. Harvester's default "rancher"
                     user; root SSH is disabled by Harvester's own default
                     hardening, confirmed live 2026-08-30).

    Returns:
        subprocess.CompletedProcess
    """
    args = _SSH_BASE + ["{}@{}".format(user, hostname), cmd]

    # Quiet-by-default: when the caller isn't explicitly capturing and debug is
    # off, capture the output internally and only print it if the command fails
    # or looks like it emitted a warning/error. debug on -> stream live, as before.
    # A caller that passes capture=True is unaffected (it reads .stdout itself).
    quiet = (not capture) and (not _DEBUG)
    result = subprocess.run(
        args,
        universal_newlines=True,
        input=input_text,
        stdout=subprocess.PIPE if (capture or quiet) else None,
        stderr=subprocess.STDOUT if quiet else (subprocess.PIPE if capture else None),
    )
    if quiet:
        out = (result.stdout or "").rstrip()
        if out and (result.returncode != 0 or _looks_noisy(out)):
            print(out)
    if check and result.returncode != 0:
        raise RuntimeError(
            "SSH command failed (rc={}) on {}:\n  {}".format(
                result.returncode, hostname, cmd[:120]
            )
        )
    return result


def ssh_output(hostname, cmd):
    """Run a command on a remote host and return stripped stdout."""
    return ssh_run(hostname, cmd, capture=True).stdout.strip()


def purge_known_host(*names):
    """
    Remove any stale SSH host-key entries for the given hostname(s)/IP(s)
    from this user's known_hosts, before the first real connection to a
    freshly (re)created VM. This project's lab IPs get reused across many
    disposable test VMs over time — without this, ssh_run()'s
    StrictHostKeyChecking=accept-new still refuses a brand-new VM outright
    ("REMOTE HOST IDENTIFICATION HAS CHANGED") whenever its address was
    previously held by any other VM, even though the new one is genuinely
    up and answering correctly.

    Extracted 2026-09-05 after fixing the same class of bug in 3 separate
    places (setup_lab.py/destroy_lab.py already had this inline;
    setup_harvester_cluster.py's _create_netboot_vm() and
    build_lab_usb.py's lab-host VM bootstrap both needed it added) — past
    the point where duplicating it a 4th time made sense.

    Best-effort: a name with no existing entry is a silent no-op (matches
    ssh-keygen's own exit-code-1-on-nothing-to-remove behavior), never
    raises.
    """
    known_hosts = str(Path.home() / ".ssh" / "known_hosts")
    for name in names:
        if not name:
            continue
        subprocess.run(["ssh-keygen", "-f", known_hosts, "-R", name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _has_local_binary(binary):
    """Thin shutil.which() wrapper so tests can force run_libvirt_tool()'s
    local-vs-SSH-fallback branch deterministically (patch this one name)
    instead of depending on whether the test container happens to have
    virsh/virt-install installed."""
    return shutil.which(binary) is not None


def run_libvirt_tool(binary, remote_host, virt_srv, args, **kwargs):
    """
    Run `<binary> --connect <virt_srv> <args...>` — binary is "virsh" or
    "virt-install". Prefers the local binary, exactly today's behavior
    everywhere it's already installed (the bare automation VM host always
    has it — zero change there). Falls back to plain SSH to `remote_host`,
    running the same command against the hypervisor's own local libvirt
    socket (qemu:///system), when no local binary exists.

    Why this exists: confirmed live (2026-08-29) that a thin ssh+rsync-only
    runtime (the MCP endpoint's container) can't just `zypper install
    libvirt-client virt-install` its way to parity — virt-install pulls a
    genuinely heavy GTK/libvirt-python dependency tree that filled the
    automation VM's own disk mid-build, and SUSE's minimal BCI repos don't
    carry these packages at all. This project's own philosophy is already
    "reach remote hosts over SSH" (ssh_run() everywhere else) — a thin
    client shouldn't need a full local libvirt stack just to reach a
    hypervisor it already talks to over SSH for every other operation, so
    this gives virsh/virt-install the same SSH-first treatment instead of
    forcing every runtime environment to carry one.

    Mirrors subprocess.run's kwarg surface (capture_output/text/stdout=
    PIPE-or-DEVNULL/check) and returns a subprocess.CompletedProcess (or
    ssh_run's equivalent), so it drops in unchanged at any existing
    subprocess.run([binary, "--connect", virt_srv, ...], **kwargs) call
    site.
    """
    if _has_local_binary(binary):
        return subprocess.run([binary, "--connect", virt_srv] + list(args), **kwargs)
    if not remote_host:
        raise RuntimeError("no local {} and no remote_host to reach it via SSH".format(binary))
    capture = kwargs.pop("capture_output", False) or kwargs.pop("stdout", None) == subprocess.PIPE
    kwargs.pop("stderr", None)
    kwargs.pop("text", None)
    check = kwargs.pop("check", False)
    cmd = "{} --connect qemu:///system {}".format(binary, " ".join(shlex.quote(str(a)) for a in args))
    return ssh_run(remote_host, cmd, check=check, capture=capture)


_INSTALL_ISO_HTTP_PORT = 8890


def ensure_iso_install_tree(remote_host, iso_loc, iso_image):
    """
    Idempotently loop-mounts `iso_image` (already present at
    `{iso_loc}/{iso_image}` on `remote_host`, the KVM hypervisor) read-only
    and serves it over HTTP directly from `remote_host` itself, so
    virt-install's `--location` can reach it as a real HTTP install tree.
    Returns the http:// URL to pass as `--location`.

    Confirmed live 2026-09-17: `--location <bare filesystem path>` fails
    with "Cannot access install tree on remote connection" whenever
    virt-install itself runs on a DIFFERENT host than the hypervisor (this
    project's own default architecture: automation VM -> qemu+ssh ->
    hypervisor) — virt-install inspects the path on ITS OWN local
    filesystem to extract the installer's kernel/initrd, never over the
    remote libvirt connection. This is a real, general gap in every
    `--location`-based install (kickstart/autoyast/preseed via
    config_method "install_iso"), not specific to any one distro — Ubuntu's
    own autoinstall branch sidesteps it entirely by using `--cdrom`
    instead (no filesystem inspection needed), and its own comment right
    above that code already flagged this exact `--location` limitation
    before this function existed to actually fix it.

    Mount and HTTP server are both idempotent (skip if already present) and
    SHARED across every ISO ever installed this way — one small
    `python3 -m http.server` systemd unit on the hypervisor, serving
    `{iso_loc}/.mounted-isos/` (every mounted ISO gets its own subdirectory
    under there, named after the ISO's own filename), not one server per
    ISO. The loop mount is also persisted in /etc/fstab with `nofail` (a
    single, exact-line, dedup-checked append — never blocks a hypervisor
    reboot if the ISO/mount ever goes missing) so it survives a hypervisor
    reboot without needing this function to run again first.
    """
    mount_dir = "{}/.mounted-isos/{}".format(iso_loc, iso_image)
    iso_path = "{}/{}".format(iso_loc, iso_image)

    mounted = ssh_run(remote_host, "mountpoint -q {}".format(shlex.quote(mount_dir)), check=False)
    if mounted.returncode != 0:
        ssh_run(remote_host, "mkdir -p {}".format(shlex.quote(mount_dir)))
        r = ssh_run(remote_host, "mount -o loop,ro {} {}".format(
            shlex.quote(iso_path), shlex.quote(mount_dir)), check=False)
        if r.returncode != 0:
            die("could not loop-mount ISO '{}' on '{}': {}".format(iso_image, remote_host,
                                                                     (r.stderr or r.stdout or "").strip()))

    fstab_line = "{} {} iso9660 loop,ro,nofail 0 0".format(iso_path, mount_dir)
    ssh_run(remote_host, "grep -qxF {} /etc/fstab || echo {} >> /etc/fstab".format(
        shlex.quote(fstab_line), shlex.quote(fstab_line)))

    serve_root = "{}/.mounted-isos".format(iso_loc)
    active = ssh_run(remote_host, "systemctl is-active install-iso-server.service", check=False)
    if active.returncode != 0:
        unit = (
            "[Unit]\n"
            "Description=Install-tree HTTP server for lab-in-a-box install_iso "
            "(kickstart/autoyast/preseed) — shared across every mounted ISO\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "WorkingDirectory={}\n"
            "ExecStart=/usr/bin/python3 -m http.server {}\n"
            "Restart=always\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        ).format(serve_root, _INSTALL_ISO_HTTP_PORT)
        ssh_run(remote_host, "cat > /etc/systemd/system/install-iso-server.service <<'EOF'\n{}EOF".format(unit))
        ssh_run(remote_host, "systemctl daemon-reload && systemctl enable --now install-iso-server.service")

    return "http://{}:{}/{}/".format(remote_host, _INSTALL_ISO_HTTP_PORT, iso_image)


def check_ssh_conn(vm_name, tcp_port=22, retry_interval=2, retry_limit=100):
    """
    Poll a host's TCP port until it accepts a connection. Mirrors check_ssh_conn (bash).

    Defaults match bash exactly: 2s between retries, 100 retries (~200s total),
    port 22 — i.e. _tcp_port/_retry_interval/_retry_limit's bash defaults.
    Calls die() (mirrors fail_with_error, which exits the whole process) if the
    retry limit is exceeded — this never returns a failure value, same as bash.

    NOTE: bash used `nc -z -w 2 host port` for the probe (2s connect timeout);
    here a raw socket connect with the same 2s timeout is used instead — same
    observable result without depending on netcat being installed. bash's
    'ERROR - Netcat(nc) not installed' branch (exit code 127) has no equivalent
    since this doesn't shell out to nc.
    """
    global _level
    _level += 1
    log("Waiting for {}{}{} to come online".format(_RED, vm_name, _RESET))
    count = 0
    _level += 1
    try:
        while True:
            count += 1
            time.sleep(retry_interval)
            try:
                s = socket.create_connection((vm_name, tcp_port), timeout=2)
                s.close()
                log("{}{}{} is online".format(_RED, vm_name, _RESET))
                return
            except OSError:
                pass
            if count > retry_limit:
                die("retry limit ( {} ) exceeded waiting for {}{}{} to boot.".format(
                    retry_limit, _RED, vm_name, _RESET))
    finally:
        _level -= 2


# ── Variable loading ──────────────────────────────────────────────────────────

def section_vars(definition, section):
    """
    Return all key-value pairs from a named top-level section of the definition.

    Replaces the family of load_*_vars bash functions:
        load_rancher_vars() → section_vars(definition, "rancher")
        load_lh_vars()      → section_vars(definition, "longhorn")
        load_nv_vars()      → section_vars(definition, "neuvector")
        … and so on for every addon section.

    Only scalar values are returned; nested objects and arrays are skipped.
    Returns a dict.
    """
    raw = definition.get(section, {})
    if not raw:
        warn("No variables defined for section '{}'".format(section))
    return {k: v for k, v in raw.items() if isinstance(v, (str, int, float, bool))}


def load_common_vars(definition):
    """Return all key-value pairs from the 'common' section. Returns a dict."""
    return dict(definition.get("common", {}))


def load_vm_vars(definition, vm_name, config=None):
    """
    Load variables for a specific VM (mirrors load_vm_vars in bash).

    Merges 'common' keys with per-node keys, resolves kcluster → clu_name,
    and auto-detects mydns, mygw, mydomain, mynet_reverse when absent.

    Args:
        definition : Loaded lab definition dict.
        vm_name    : The node key as it appears in definition["nodes"].
        config     : Optional lab_creation.cfg dict for additional context.

    Returns a dict of all variables for this VM.
    """
    vars = {}

    vars.update(definition.get("common", {}))

    node = definition.get("nodes", {}).get(vm_name, {})
    for key, val in node.items():
        if key == "kcluster":
            vars["clu_name"] = val
        else:
            vars[key] = val

    if not vars.get("mydns"):
        vars["mydns"] = _detect_dns()
    if not vars.get("mygw"):
        vars["mygw"] = _detect_gateway()
    if not vars.get("mydomain"):
        vars["mydomain"] = _detect_domain()
    if not vars.get("mynet_reverse") and vars.get("myip"):
        vars["mynet_reverse"] = _reverse_ip(vars["myip"])
    if not vars.get("mymask"):
        vars["mymask"] = _detect_netmask()

    return vars


def total_lab_resources(definition):
    """
    Sums VM_CPU / VM_MEM (MiB) / VM_DSK (GiB) across every node in a lab
    definition — a per-node value overrides common's, exactly the
    common-then-per-node precedence every other field in this project
    follows, without load_vm_vars()'s auto-detection side effects (real
    gateway/DNS/domain lookups), which this has no use for. Pure function,
    no I/O.

    Two independent uses: sizing the USB-delivery lab-host VM (has to be
    big enough to nest every one of these VMs inside it), and an
    informational "this lab needs N vCPU / M MiB RAM / G GiB disk" message
    on an ordinary setup_lab.py run — unrelated to USB delivery.

    Returns (total_cpu, total_mem_mib, total_disk_gib) as ints. A node with
    neither its own value nor a common default contributes 0 for that field.
    """
    common = definition.get("common", {}) or {}
    total_cpu = 0
    total_mem = 0
    total_disk = 0
    for node in (definition.get("nodes", {}) or {}).values():
        node = node or {}
        total_cpu += int(node.get("VM_CPU", common.get("VM_CPU", 0)) or 0)
        total_mem += int(node.get("VM_MEM", common.get("VM_MEM", 0)) or 0)
        total_disk += int(node.get("VM_DSK", common.get("VM_DSK", 0)) or 0)
    return total_cpu, total_mem, total_disk


# ── Multi-host selection ───────────────────────────────────────────────────────
#
# New in the python port — bash only ever supported one hypervisor
# (REMOTE_HOST/VIRT_SRV as flat globals). KVM_HOSTS is an optional cfg key
# (space-separated, like REMOTE_DNS_SERVERS); when absent or single-valued,
# resolve_kvm_host() never calls select_kvm_host()'s SSH probing at all, so
# existing single-host setups are unaffected.

def _host_resources(host, vm_img_loc):
    """
    Query free vCPUs, free memory (MiB), and free disk (MiB) on vm_img_loc for
    one candidate KVM host, over SSH. Raises RuntimeError/ValueError on any
    query failure — the caller treats that host as disqualified rather than
    letting the whole selection blow up.

    Thin wrapper — body moved to backends.LibvirtBackend.host_resources().
    Used by select_kvm_host() to probe CANDIDATE hosts before one is chosen,
    so (unlike the other wrappers here) it builds its own throwaway backend
    from a host name rather than an already-resolved virt_srv.
    """
    from backends import LibvirtBackend
    virt_srv = "qemu+ssh://root@{}/system?keyfile=.ssh/id_rsa".format(host)
    return LibvirtBackend(virt_srv, remote_host=host, vm_img_loc=vm_img_loc).host_resources()


def select_kvm_host(hosts, vm_cpu, vm_mem, vm_dsk, vm_img_loc):
    """
    Pick a KVM host from `hosts` with enough free CPU/memory/disk for a VM
    requesting vm_cpu vCPUs, vm_mem MiB RAM, and vm_dsk GiB disk on
    vm_img_loc (identical path on every host by design — see
    resolve_kvm_host). Among hosts with enough of all three, picks the one
    with the most free memory — simplest single tiebreaker, since memory is
    the usual bottleneck for these labs.

    Dies (via die()) listing each host's shortfall if none qualify.
    """
    candidates = []
    shortfalls = []
    for host in hosts:
        try:
            free_cpu, free_mem, free_disk = _host_resources(host, vm_img_loc)
        except (RuntimeError, ValueError) as e:
            shortfalls.append("{}: could not query resources ({})".format(host, e))
            continue

        missing = []
        if free_cpu < int(vm_cpu):
            missing.append("{} free vCPUs < {} requested".format(free_cpu, vm_cpu))
        if free_mem < int(vm_mem):
            missing.append("{} MiB free RAM < {} requested".format(free_mem, vm_mem))
        if free_disk < int(vm_dsk) * 1024:
            missing.append("{} MiB free disk < {} GiB requested".format(free_disk, vm_dsk))

        if missing:
            shortfalls.append("{}: {}".format(host, "; ".join(missing)))
        else:
            candidates.append((free_mem, host))

    if not candidates:
        die("No KVM host in [{}] has enough free resources for this VM "
            "({} vCPU, {} MiB RAM, {} GiB disk):\n  {}".format(
                ", ".join(hosts), vm_cpu, vm_mem, vm_dsk, "\n  ".join(shortfalls)))

    candidates.sort(reverse=True)  # most free memory first
    return candidates[0][1]


def _configured_hosts(config):
    """Returns (hosts, default_host): KVM_HOSTS split, or [REMOTE_HOST] when unset."""
    default_host = config.get("REMOTE_HOST", "")
    hosts_raw = config.get("KVM_HOSTS") or default_host
    hosts = hosts_raw.split() if hosts_raw else []
    return hosts, default_host


def _virt_srv_for_host(host, default_host, config):
    """
    libvirt connection URI for `host`. Reuses config["VIRT_SRV"] verbatim
    when `host` is the configured default REMOTE_HOST (preserving any
    customization — different keyfile, extra URI params — an existing
    single-host cfg might have); for any other host it's derived
    programmatically, since there's no per-host VIRT_SRV to reuse.
    """
    if host == default_host and config.get("VIRT_SRV"):
        return config["VIRT_SRV"]
    return "qemu+ssh://root@{}/system?keyfile=.ssh/id_rsa".format(host)


def resolve_kvm_host(definition, vm_name, config, vm_img_loc=None):
    """
    Resolve which KVM host a NEW VM should be created on, and its libvirt
    URI. For operations on an EXISTING VM (destroy, reboot), use
    locate_kvm_host() instead — see its docstring for why the two must not
    share one implementation.

    Precedence: an explicit nodes[vm_name].kvm_host override wins; otherwise
    select_kvm_host() picks one from KVM_HOSTS. With zero or one configured
    host (today's single-hypervisor setups, or KVM_HOSTS unset), no
    selection logic runs at all — the sole host is used directly, same as
    before this feature existed.

    Returns (remote_host, virt_srv).
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    explicit_host = node_cfg.get("kvm_host")

    hosts, default_host = _configured_hosts(config)

    if explicit_host:
        host = explicit_host
    elif len(hosts) <= 1:
        host = hosts[0] if hosts else default_host
    else:
        common_cfg = definition.get("common", {}) or {}
        vm_cpu = node_cfg.get("VM_CPU", common_cfg.get("VM_CPU", 0))
        vm_mem = node_cfg.get("VM_MEM", common_cfg.get("VM_MEM", 0))
        vm_dsk = node_cfg.get("VM_DSK", common_cfg.get("VM_DSK", 0))
        host = select_kvm_host(hosts, vm_cpu, vm_mem, vm_dsk, vm_img_loc or "/var/lib/libvirt/images/")

    return host, _virt_srv_for_host(host, default_host, config)


def locate_kvm_host(definition, vm_name, config):
    """
    Find which KVM host an EXISTING VM currently lives on, for destroy/
    reboot/reusability-check operations. Deliberately NOT the same code
    path as resolve_kvm_host(): a VM's host is never recorded anywhere
    after creation (kvm_host is optional and intentionally not written
    back to the JSON), so re-running resource-based selection here could
    return a different host than the one the VM actually runs on — the
    only reliable way to find it again is to ask each host directly.

    Precedence: an explicit nodes[vm_name].kvm_host override wins; with
    zero or one configured host, no probing happens (same as
    resolve_kvm_host() in that case). Otherwise queries each host with
    `virsh dominfo <vm_name>` and returns the first one that has it. Dies
    if no configured host has a domain by this name.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    explicit_host = node_cfg.get("kvm_host")

    hosts, default_host = _configured_hosts(config)

    if explicit_host:
        host = explicit_host
        return host, _virt_srv_for_host(host, default_host, config)
    if len(hosts) <= 1:
        host = hosts[0] if hosts else default_host
        return host, _virt_srv_for_host(host, default_host, config)

    for host in hosts:
        virt_srv = _virt_srv_for_host(host, default_host, config)
        result = run_libvirt_tool(
            "virsh", host, virt_srv, ["dominfo", vm_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return host, virt_srv

    die("VM '{}' not found on any configured KVM host: {}".format(vm_name, ", ".join(hosts)))


# ── VM management ─────────────────────────────────────────────────────────────

def copy_vm_image(remote_host, iso_loc, iso_image, vm_img_loc, vm_name, vm_dsk_gb, config_method=""):
    """
    Copy a QCOW2 source image and resize it on the hypervisor. Thin wrapper —
    body moved to backends.LibvirtBackend.copy_vm_image().
    """
    from backends import LibvirtBackend
    backend = LibvirtBackend(None, remote_host=remote_host, iso_loc=iso_loc, vm_img_loc=vm_img_loc)
    backend.copy_vm_image(iso_image, vm_name, vm_dsk_gb, config_method=config_method)


def create_vm(
    virt_srv, vm_name, vm_cpu, vm_mem, vm_dsk_gb, vm_img_loc, network, remote_host,
    os_variant="slem5.4", boot="uefi", config_method="",  # boot: "uefi", "firmware=bios", "hd", …
    lab_setup_path="/srv/www/htdocs/lab_creation",
    extra_disks=None, extra_filesystems=None, vm_dsk_bus="virtio",
    ign_file=None, com_file=None, salt_states="",
    install_type="", iso_image="", iso_loc="", mydns="",
    vcluster="",
):
    """
    Create a VM on a KVM hypervisor via virt-install, covering all 6
    config_method branches (see backends.LibvirtBackend.create_vm for the
    full branch-by-branch description). Thin wrapper — body moved there.
    """
    from backends import LibvirtBackend
    backend = LibvirtBackend(virt_srv, remote_host=remote_host, iso_loc=iso_loc,
                              vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)
    backend.create_vm(
        vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        os_variant=os_variant, boot=boot, config_method=config_method,
        extra_disks=extra_disks, extra_filesystems=extra_filesystems, vm_dsk_bus=vm_dsk_bus,
        ign_file=ign_file, com_file=com_file, salt_states=salt_states,
        install_type=install_type, iso_image=iso_image, iso_loc=iso_loc, mydns=mydns,
        vcluster=vcluster,
    )


def delete_vm(virt_srv, vm_name):
    """
    Remove a VM and all its storage from the hypervisor. Thin wrapper — body
    moved to backends.LibvirtBackend.delete_vm().
    """
    from backends import LibvirtBackend
    LibvirtBackend(virt_srv).delete_vm(vm_name)


def clean_ssh_keys(vm_name, myip):
    """Remove stale SSH known-hosts entries for a VM (mirrors clean_ssh_keys)."""
    known = str(Path.home() / ".ssh" / "known_hosts")
    for host in (vm_name, myip):
        subprocess.run(["ssh-keygen", "-f", known, "-R", host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def ensure_lab_ssh_key(lab_host_ip, key_path="/root/.ssh/id_lab_ed25519", key_comment="lab-in-a-box"):
    """
    Idempotently generates ONE ed25519 SSH keypair ON lab_host_ip itself —
    never locally, never per-VM. A USB-delivered lab is meant to be reached
    entirely through its own lab-host VM, so one keypair, generated and
    kept there, is what makes "SSH into any VM in this lab" easy for
    whoever receives it: not the operator's own key, and not a separate key
    per node.

    Returns the public key's content (a single line: "ssh-ed25519 <b64> <comment>").
    """
    exists = ssh_run(lab_host_ip, "test -f {}.pub".format(shlex.quote(key_path)), check=False).returncode == 0
    if exists:
        log("- Lab-wide SSH keypair already exists on {} — reusing it".format(lab_host_ip))
    else:
        ssh_run(lab_host_ip, "ssh-keygen -t ed25519 -N '' -C {} -f {}".format(
            shlex.quote(key_comment), shlex.quote(key_path)))
        log("- Generated a new lab-wide SSH keypair on {}".format(lab_host_ip))
    return ssh_output(lab_host_ip, "cat {}.pub".format(shlex.quote(key_path)))


def distribute_lab_ssh_key(lab_host_ip, pubkey, target_ips):
    """
    Installs `pubkey` (from ensure_lab_ssh_key()) into every target VM's
    authorized_keys — idempotent (skips a target that already has the exact
    line).

    Run FROM lab_host_ip, not from wherever this function itself executes:
    the lab-host VM already has SSH access to each nested VM (it just
    created them), while the orchestrator has no direct network
    reachability into whatever internal NAT range those VMs live on. Each
    call is a two-hop SSH (lab_host_ip -> target_ip).

    The key content is passed through stdin at every hop rather than
    embedded in a quoted shell string — a pubkey is a single line with no
    shell metacharacters, but nesting it through two remote shells via
    string interpolation is exactly the kind of thing that's easy to get
    subtly wrong; piping it through avoids the question entirely. POSIX
    `sh`-compatible throughout (no bash-only `<<<`), matching this
    project's general portability stance.
    """
    for target_ip in target_ips:
        remote_cmd = (
            "key=$(cat); "
            "printf '%s' \"$key\" | ssh -o StrictHostKeyChecking=accept-new root@{ip} "
            "'key=$(cat); grep -qxF \"$key\" ~/.ssh/authorized_keys 2>/dev/null "
            "|| echo \"$key\" >> ~/.ssh/authorized_keys'"
        ).format(ip=shlex.quote(target_ip))
        ssh_run(lab_host_ip, remote_cmd, input_text=pubkey)
        log("- Lab SSH key installed on {}".format(target_ip))


def prepare_local_as_kubeclient():
    """Ensure ~/.kube exists for kubeconfig storage (mirrors prepare_local_as_kubeclient)."""
    (Path.home() / ".kube").mkdir(parents=True, exist_ok=True)


def copy_to_hypervisor(remote_host, lab_setup_path, vm_name, config_method="", vm_img_loc=None):
    """
    Copy the provisioning materials needed for the install to the hypervisor.
    Thin wrapper — body moved to backends.LibvirtBackend.push_provisioning_files().
    """
    from backends import LibvirtBackend
    backend = LibvirtBackend(None, remote_host=remote_host, lab_setup_path=lab_setup_path, vm_img_loc=vm_img_loc)
    backend.push_provisioning_files(vm_name, config_method=config_method, vm_img_loc=vm_img_loc)


# ── DNS management ────────────────────────────────────────────────────────────
#
# Bodies moved to services.DNSService — these are thin back-compat wrappers,
# a fresh DNSService() per call (it holds no state). Kept because 9 addon
# scripts + setup_vm.py/destroy_vm.py/k8s.py import these names directly.

def add_to_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
    """Add forward (A) and reverse (PTR) DNS records for a VM."""
    from services import DNSService
    DNSService().add_to_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)


def del_from_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
    """Remove forward and reverse DNS records for a VM."""
    from services import DNSService
    DNSService().del_from_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=remote_dns_servers)


def add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain, remote_dns_servers=None):
    """Add round-robin A records for a cluster service DNS entry."""
    from services import DNSService
    DNSService().add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain,
                                  remote_dns_servers=remote_dns_servers)


def restart_named(remote_servers=None):
    """Restart the local BIND named service and optionally on remote servers."""
    from services import DNSService
    DNSService().restart_named(remote_servers=remote_servers)


def add_dns_to_named_rr(definition, dns_entry, node_name, mydomain, remote_dns_servers=None):
    """Add a single round-robin A record (dns_entry -> node_name's own myip)."""
    from services import DNSService
    DNSService().add_dns_to_named_rr(definition, dns_entry, node_name, mydomain,
                                      remote_dns_servers=remote_dns_servers)


# ── Provisioning files ────────────────────────────────────────────────────────

def prepare_ignition_combustion(
    vm_name, lab_setup_path, root_pwd_hash, root_ssh_key,
    mysource, sourcepath, mydns, myip, mymask, mygw,
    suse_email="", suse_regcode="", suse_url="",
):
    """
    Create Ignition + Combustion provisioning files for a VM. Mirrors
    prepare_ign_and_cmb (bash).

    NOTE: bash substitutes ROOT_SSH_KEY differently in the two output files —
    ignition.ign gets the LOCAL machine's own pubkey (`cat /root/.ssh/id_rsa.pub`,
    read fresh on every call), while combustion gets the `$ROOT_SSH_KEY` config
    variable (the `root_ssh_key` argument here). These can genuinely be
    different values, so both are preserved rather than collapsed into one.
    """
    base = Path(lab_setup_path)
    log("- Create ignition and combustion files for \"{}{}{}\"".format(_RED, vm_name, _RESET))

    root_pub_key = Path("/root/.ssh/id_rsa.pub").read_text().strip()

    ign_out = base / "ignition" / "{}.ign".format(vm_name)
    text = (base / "ignition" / "template").read_text()
    text = (text
            .replace("TEMPLATE_HN", vm_name)
            .replace("ROOT_PWD_HASH", root_pwd_hash)
            .replace("ROOT_SSH_KEY", root_pub_key))
    ign_out.write_text(text)

    cmb_out = base / "combustion" / vm_name
    text = (base / "combustion" / "template").read_text()
    inject = (
        "mysource={}\nsourcepath={}\nmydns={}\nmyip={}\n"
        "mymask={}\nmygw={}\nSUSE_email={}\nSUSE_regcode={}\nSUSE_url={}\n"
    ).format(mysource, sourcepath, mydns, myip, mymask, mygw,
             suse_email, suse_regcode, suse_url)
    text = text.replace("#local vars", "#local vars\n" + inject, 1)
    text = text.replace("ROOT_SSH_KEY", root_ssh_key)
    cmb_out.write_text(text)


def prepare_cloud_init(vm_name, lab_setup_path, variables):
    """
    Create cloud-init user-data, network-config, and meta-data files. Mirrors
    prepare_cloud-init (bash).

    variables : dict of values available to the templates via process_template
                (mirrors the shell variables visible during bash's eval/
                heredoc expansion). ROOT_SSH_KEY is always overridden with the
                local machine's own pubkey here — mirrors bash's local
                `ROOT_SSH_KEY=$(cat /root/.ssh/id_rsa.pub)` reassignment inside
                this function, which shadows whatever ROOT_SSH_KEY held before.
                network_renderer defaults to "NetworkManager" (SLE Micro/SLES/
                Leap's own default network stack) when the lab JSON doesn't set
                it — confirmed live 2026-08-30 that hardcoding NetworkManager
                here silently produced a guest with ZERO configured interfaces
                (not even DHCP fallback) on an Ubuntu Server guest, which
                defaults to systemd-networkd instead; template_network-config
                now takes the renderer from this variable instead of a literal.

                An empty/omitted `myip` selects template_network-config-dhcp
                instead of the static-addressing template — every existing
                lab node always sets myip, so this is purely additive; it
                exists for the USB-delivery lab-host VM, whose own IP is
                unknown at build time (unlike every other node this project
                creates, provisioned with a real, known address baked in).

                `_vm_name` is always injected here (overriding anything the
                caller passed under that key): bash's version ran in the same
                shell as its caller, so template_user-data/template_meta-data
                referencing `${_vm_name}` just saw whatever the enclosing
                loop's global `_vm_name` already held — no explicit passing
                needed. This function's caller (setup_vm.py) never puts
                `_vm_name` in the `env` dict it builds (it only ever reads
                `vm_name` as a separate local), so every cloud-init node's
                instance-id/local-hostname/fqdn/hostname silently rendered
                empty until this was added — confirmed live 2026-09-02 by
                reading a real generated *_meta-data/*_user-data pair off
                the automation VM.
    """
    base = Path(lab_setup_path) / "cloud-init"
    log("- Create cloud-init files for \"{}{}{}\"".format(_RED, vm_name, _RESET))
    render_vars = dict(variables)
    render_vars["_vm_name"] = vm_name
    render_vars["ROOT_SSH_KEY"] = Path("/root/.ssh/id_rsa.pub").read_text().strip()
    render_vars["network_renderer"] = render_vars.get("network_renderer") or "NetworkManager"
    dhcp = not (render_vars.get("myip") or "").strip()
    # A cloud backend leaves BOTH myip and mymac empty (see _cloud_no_mac()'s own docstring in
    # backends.py) — template_network-config-dhcp matches its NIC by `macaddress: "${mymac}"`,
    # which would render an empty match and likely configure no interface at all. Found and fixed
    # 2026-09-09, live-testing AWSBackend, before it ever reached a real instance: a name-glob-
    # based sibling template is used instead whenever mymac is ALSO empty — the USB-delivery lab-
    # host VM (myip empty, mymac known) still gets the original mac-matched template unchanged.
    no_mac = not (render_vars.get("mymac") or "").strip()
    for kind in ("user-data", "network-config", "meta-data"):
        if kind == "network-config" and dhcp:
            tmpl_name = "network-config-dhcp-nomac" if no_mac else "network-config-dhcp"
        else:
            tmpl_name = kind
        tmpl = base / "template_{}".format(tmpl_name)
        out  = base / "{}_{}".format(vm_name, kind)
        out.write_text(process_template(str(tmpl), render_vars))


# ── virt-customize ───────────────────────────────────────────────────────────
#
# NOTE: an earlier, local-execution design (_virt_ls/_virt_cat/_vc_detect_net_type/
# _vc_detect_iface/_vc_net_config, using virt-ls/virt-cat directly from the
# automation VM) lived here and was removed — verified via grep that none of
# them were called anywhere (not even from prepare_virt_customize below, and
# not from webui/). It was superseded by the current design, which generates a
# fully self-contained script (embedding its own vls/vcat/detect_net/etc.) and
# runs it entirely on the hypervisor over SSH, since virt-customize/virt-ls
# need to run where the qcow2 image actually lives.

def prepare_virt_customize_for_vm(
    remote_host, vm_img_loc, vm_name, myip, mymask, mygw, mydns, mydomain, mymac,
    vm_root_pass=None, root_pwd_hash=None, root_ssh_key_path=None,
):
    """
    Resolve the root password and SSH pubkey, then delegate to
    prepare_virt_customize(). Mirrors the bash-level prepare_virt_customize
    function (bash:605-669), which only computed these derived values and
    bridged into this same python function via `python3 -c` + env vars +
    base64 — that bridge is unnecessary here since this already runs
    in-process.

    Password fallback chain (identical to bash):
      VM_ROOT_PASS set   → plain password
      else ROOT_PWD_HASH → crypted hash
      else               → plain password "linux" (with a warning)

    Pubkey fallback chain (identical to bash):
      ROOT_SSH_KEY set (a private-key PATH) and "<path>.pub" exists → that pubkey
      else ~/.ssh/id_rsa.pub if it exists
      else no key injected

    NOTE: bash's own `|| fail_with_error` around this call is redundant here —
    prepare_virt_customize() already calls die() itself on failure (same
    message, mentioning vm_name), so there's nothing left for this wrapper
    to catch.
    """
    img = "{}/{}.qcow2".format(vm_img_loc, vm_name)
    prefix = mymask or "24"

    if vm_root_pass:
        pass_type, pass_val = "plain", vm_root_pass
    elif root_pwd_hash:
        pass_type, pass_val = "crypted", root_pwd_hash
    else:
        pass_type, pass_val = "plain", "linux"
        log("WARNING: neither VM_ROOT_PASS nor ROOT_PWD_HASH is set — using default password 'linux'")

    pubkey = None
    if root_ssh_key_path and Path(root_ssh_key_path + ".pub").is_file():
        pubkey = Path(root_ssh_key_path + ".pub").read_text().strip()
    elif Path.home().joinpath(".ssh", "id_rsa.pub").is_file():
        pubkey = Path.home().joinpath(".ssh", "id_rsa.pub").read_text().strip()

    log("- Customising '{}' via virt-customize ({}/{})".format(vm_name, myip, prefix))

    prepare_virt_customize(
        remote_host=remote_host, img_path=img, vm_name=vm_name,
        ip=myip, prefix=prefix, gw=mygw or "", dns=mydns or "",
        domain=mydomain or "", mac=mymac or "",
        pass_type=pass_type, pass_val=pass_val, pubkey=pubkey,
    )


def prepare_virt_customize(
    remote_host, img_path, vm_name,
    ip, prefix, gw, dns, domain, mac,
    pass_type, pass_val,
    pubkey=None,
):
    """
    Configure a QCOW2 image on the hypervisor via virt-customize.

    All guest inspection (virt-ls / virt-cat) and the virt-customize invocation
    run over SSH on remote_host.  Sensitive values are base64-encoded so they
    survive the SSH argument list without word-splitting.

    pass_type : "plain" or "crypted"
    pass_val  : plaintext password or crypt hash (e.g. $6$salt$hash)
    pubkey    : SSH public key string (the full "ssh-rsa AAAA… user@host" line), or None
    """
    log("Customising '{}' via virt-customize ({}/{})".format(vm_name, ip, prefix))

    pass_b64   = base64.b64encode(pass_val.encode()).decode()
    pubkey_b64 = base64.b64encode(pubkey.encode()).decode() if pubkey else ""

    # Build the Python snippet that runs on the hypervisor.
    # Values are embedded using repr() — produces valid Python literals regardless
    # of content (dollar signs, spaces, quotes, newlines).  No env vars, no SSH
    # argument quoting, no word-splitting risk.
    script = """\
import base64, os, re, subprocess, sys, tempfile

img       = {img!r}
vmname    = {vmname!r}
ip        = {ip!r}
prefix    = {prefix!r}
gw        = {gw!r}
dns       = {dns!r}
domain    = {domain!r}
mac       = {mac!r}
pass_type = {pass_type!r}
pass_b64  = {pass_b64!r}
pk_b64    = {pubkey_b64!r}
""".format(
        img=img_path, vmname=vm_name, ip=ip, prefix=str(prefix),
        gw=gw or "", dns=dns or "", domain=domain or "", mac=mac or "",
        pass_type=pass_type, pass_b64=pass_b64, pubkey_b64=pubkey_b64,
    ) + '''

def vls(path):
    r = subprocess.run(["virt-ls", "-a", img, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8","replace").splitlines() if r.returncode == 0 else []

def vcat(path):
    r = subprocess.run(["virt-cat", "-a", img, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8","replace") if r.returncode == 0 else ""

def _virt_exists(path):
    return subprocess.run(["virt-cat","-a",img,path],stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode==0

def detect_net():
    # netplan: only if the binary is installed — some cloud images drop .yaml templates without netplan
    if any(f.endswith((".yaml",".yml")) for f in vls("/etc/netplan")):
        if _virt_exists("/usr/sbin/netplan") or _virt_exists("/usr/bin/netplan"):
            return "netplan"
    # network-scripts before nm-keyfile: CentOS 7 has NM installed but uses ifcfg as primary
    if any(f.startswith("ifcfg-") and f!="ifcfg-lo" for f in vls("/etc/sysconfig/network-scripts")):
        return "network-scripts"
    # wicked: binary exists AND its service is enabled (in wants dirs).
    # SLES 16 may have the wicked binary as a compat package but NM is primary —
    # in that case wicked.service is not in the wants symlinks.
    if _virt_exists("/usr/sbin/wicked"):
        wants = (vls("/etc/systemd/system/network.target.wants") +
                 vls("/etc/systemd/system/multi-user.target.wants"))
        if any("wicked" in f.lower() for f in wants):
            return "wicked"
    # cloud-init+NM: SLES 16 uses cloud-init to configure NetworkManager — don't disable it
    if subprocess.run(["virt-ls","-a",img,"/etc/NetworkManager"],stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode==0:
        if _virt_exists("/usr/bin/cloud-init") or _virt_exists("/usr/sbin/cloud-init"):
            return "cloud-init"
        return "nm-keyfile"
    # wicked binary exists but NM absent and service not in wants — treat as wicked anyway
    if _virt_exists("/usr/sbin/wicked"):
        return "wicked"
    if "interfaces" in vls("/etc/network"): return "ifupdown"
    if any(f.endswith(".network") for f in vls("/etc/systemd/network")): return "systemd-networkd"
    return "unknown"

def _iface_from_udev():
    """Search udev rules in the image for a NAME= assignment matching our MAC."""
    ml = mac.lower()
    if not ml:
        return None
    for rdir in ["/etc/udev/rules.d", "/lib/udev/rules.d", "/usr/lib/udev/rules.d"]:
        for rf in vls(rdir):
            if not rf.endswith(".rules"):
                continue
            for ln in vcat("{}/{}".format(rdir, rf)).splitlines():
                if ml in ln.lower() and "NAME=" in ln:
                    for part in ln.split(","):
                        part = part.strip()
                        if part.startswith("NAME="):
                            return part.split("=",1)[1].strip().strip('"').strip("'")
    return None

def detect_iface(nt):
    ml, mu = mac.lower(), mac.upper()
    if nt == "wicked":
        # Prefer a file that already matches our MAC (LLADDR)
        fs = [f for f in vls("/etc/sysconfig/network") if f.startswith("ifcfg-") and f!="ifcfg-lo"]
        if ml:
            for f in fs:
                c = vcat("/etc/sysconfig/network/"+f)
                if "LLADDR" in c.upper() and ml in c.lower(): return f[len("ifcfg-"):]
        if fs: return fs[0][len("ifcfg-"):]
        # No existing files: try udev rules, else "eth0" (a .link file pins the name at boot)
        return _iface_from_udev() or "eth0"
    if nt == "network-scripts":
        fs = [f for f in vls("/etc/sysconfig/network-scripts") if f.startswith("ifcfg-") and f!="ifcfg-lo"]
        if mu:
            for f in fs:
                c = vcat("/etc/sysconfig/network-scripts/"+f)
                if "HWADDR" in c.upper() and mu in c.upper(): return f[len("ifcfg-"):]
        if fs: return fs[0][len("ifcfg-"):]
        return _iface_from_udev() or "eth0"
    if nt in ("nm-keyfile", "cloud-init"):
        cs = [f for f in vls("/etc/NetworkManager/system-connections") if not f.startswith(".")]
        if not cs: return "eth0"
        c = cs[0]; return c[:-len(".nmconnection")] if c.endswith(".nmconnection") else c
    if nt == "ifupdown":
        for ln in vcat("/etc/network/interfaces").splitlines():
            p = ln.split()
            if p[:1]==["iface"] and len(p)>=2 and p[1]!="lo": return p[1]
        return "eth0"
    if nt == "systemd-networkd":
        ns = [f for f in vls("/etc/systemd/network") if f.endswith(".network")]
        if ns:
            for ln in vcat("/etc/systemd/network/"+ns[0]).splitlines():
                if ln.startswith("Name="): return ln.split("=",1)[1].strip()
        return "eth0"
    if nt == "netplan":
        ys = [f for f in vls("/etc/netplan") if f.endswith((".yaml",".yml"))]
        if ys:
            in_e = False
            for ln in vcat("/etc/netplan/"+ys[0]).splitlines():
                if "ethernets:" in ln: in_e=True; continue
                if in_e and ln.startswith("    ") and not ln.startswith("     "):
                    return ln.strip().rstrip(":")
        return "eth0"
    return "eth0"

def net_config(nt, iface):
    extra = []
    chmod = None
    if nt == "wicked":
        ls = ["STARTMODE='auto'","BOOTPROTO='static'","IPADDR='{}/{}'".format(ip,prefix)]
        if mac:    ls.append("LLADDR='{}'".format(mac))
        if domain: ls.append("DOMAIN='{}'".format(domain))
        if gw: extra.append(("/etc/sysconfig/network/routes","default {} - -\\n".format(gw)))
        return "/etc/sysconfig/network/ifcfg-{}".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "network-scripts":
        ls = ["DEVICE={}".format(iface),"TYPE=Ethernet","BOOTPROTO=none","ONBOOT=yes"]
        if mac:    ls.append("HWADDR={}".format(mac))
        ls += ["IPADDR={}".format(ip),"PREFIX={}".format(prefix)]
        if gw:     ls.append("GATEWAY={}".format(gw))
        if dns:    ls.append("DNS1={}".format(dns))
        if domain: ls.append("DOMAIN={}".format(domain))
        return "/etc/sysconfig/network-scripts/ifcfg-{}".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "nm-keyfile":
        # Use mac-address match only — no interface-name — so NM applies the profile
        # regardless of the predictable interface name (enp1s0, eth0, etc.)
        ls = ["[connection]","id=lab-static","type=ethernet","autoconnect=true","","[ethernet]"]
        if mac: ls.append("mac-address={}".format(mac))
        ls += ["","[ipv4]","method=manual","addresses={}/{}".format(ip,prefix)]
        if gw:     ls.append("gateway={}".format(gw))
        if dns:    ls.append("dns={};".format(dns))
        if domain: ls.append("dns-search={};".format(domain))
        ls += ["","[ipv6]","method=disabled",""]
        return "/etc/NetworkManager/system-connections/lab-static.nmconnection", "\\n".join(ls), "0600", extra
    # cloud-init: network config is written via a NoCloud seed in the vc block — no direct file here
    if nt == "ifupdown":
        ls = ["auto lo","iface lo inet loopback","",
              "auto {}".format(iface),"iface {} inet static".format(iface),
              "    address {}/{}".format(ip,prefix)]
        if gw:     ls.append("    gateway {}".format(gw))
        if dns:    ls.append("    dns-nameservers {}".format(dns))
        if domain: ls.append("    dns-search {}".format(domain))
        return "/etc/network/interfaces", "\\n".join(ls)+"\\n", None, extra
    if nt == "systemd-networkd":
        ls = ["[Match]","MACAddress={}".format(mac) if mac else "Name={}".format(iface),
              "","[Network]","Address={}/{}".format(ip,prefix)]
        if gw:     ls.append("Gateway={}".format(gw))
        if dns:    ls.append("DNS={}".format(dns))
        if domain: ls.append("Domains={}".format(domain))
        return "/etc/systemd/network/10-{}.network".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "netplan":
        ys = [f for f in vls("/etc/netplan") if f.endswith((".yaml",".yml"))]
        npf = ys[0] if ys else "50-lab.yaml"
        ls = ["network:","  version: 2","  ethernets:","    {}:".format(iface),
              "      dhcp4: no","      addresses:","        - {}/{}".format(ip,prefix)]
        if gw: ls.append("      gateway4: {}".format(gw))
        ls.append("      nameservers:")
        if dns:    ls.append("        addresses: [{}]".format(dns))
        if domain: ls.append("        search: [{}]".format(domain))
        return "/etc/netplan/{}".format(npf), "\\n".join(ls)+"\\n", None, extra
    return None, None, None, extra

with tempfile.TemporaryDirectory(prefix="vc_") as tmp:
    nt    = detect_net()
    iface = detect_iface(nt)
    print("    net config: {}, interface: {}".format(nt, iface), flush=True)

    pass_val = base64.b64decode(pass_b64).decode()
    cred_f = os.path.join(tmp, "root_cred")
    open(cred_f,"w").write(pass_val)

    vc = ["virt-customize","-a",img,"--hostname",vmname,
          "--upload","{}:/tmp/.vc_rc".format(cred_f)]

    # Real bug found live 2026-09-23 (solar-system-lab.json, venus.mydemo.lab,
    # SLES 16): libguestfs's own --hostname action writes the LEGACY SUSE
    # /etc/HOSTNAME (uppercase) on this image, not the modern systemd
    # /etc/hostname (lowercase) SLES 16 actually reads — confirmed live:
    # /etc/HOSTNAME had the correct value, /etc/hostname was a 0-byte file
    # dated to the base image's own build time, completely untouched.
    # systemd then reported "Static hostname: (unset)" and fell back to
    # whatever transient hostname it could derive another way (reverse DNS,
    # in this case landing on a stale record from a previous VM that once
    # held the same IP — see services.py's own _dns_remove_ptr_for_octet for
    # that half of the same incident). Writing /etc/hostname directly here,
    # unconditionally alongside --hostname (not instead of it — some distros
    # DO need the legacy file too), fixes this regardless of which per-distro
    # assumption libguestfs's own --hostname implementation gets wrong.
    vc += ["--run-command", "echo {} > /etc/hostname".format(vmname)]

    if pass_type == "plain":
        vc += ["--run-command",
               r'printf "root:%s\\n" "$(cat /tmp/.vc_rc)" | chpasswd; rm -f /tmp/.vc_rc']
    else:
        vc += ["--run-command",
               r"""awk 'BEGIN{FS=OFS=":"; getline h < "/tmp/.vc_rc"; close("/tmp/.vc_rc")}"""
               r""" /^root:/{$2=h}1' /etc/shadow > /tmp/.vc_sh"""
               r""" && cat /tmp/.vc_sh > /etc/shadow && rm -f /tmp/.vc_sh /tmp/.vc_rc"""
               r""" && awk 'BEGIN{FS=OFS=":"} /^root:/ && $2!="x" && $2!=""{$2="x"}1'"""
               r""" /etc/passwd > /tmp/.vc_pw"""
               r""" && cat /tmp/.vc_pw > /etc/passwd && rm -f /tmp/.vc_pw"""]

    if pk_b64:
        pubkey = base64.b64decode(pk_b64).decode()
        ak_f = os.path.join(tmp, "authorized_keys")
        open(ak_f,"w").write(pubkey)
        vc += ["--ssh-inject","root:file:{}".format(ak_f)]

    if nt in ("wicked", "network-scripts") and mac:
        # Write a systemd .link file to pin the interface name by MAC.
        # NAME= in udev rules is not supported for net renaming in modern systemd;
        # .link files are the correct mechanism (processed by systemd-udevd before wicked starts).
        link_content = "[Match]\\nMACAddress={}\\n\\n[Link]\\nName={}\\n".format(mac.lower(), iface)
        link_f = os.path.join(tmp, "10-lab.link")
        open(link_f, "w").write(link_content)
        vc += ["--run-command", "mkdir -p /etc/systemd/network"]
        vc += ["--upload", "{}:/etc/systemd/network/10-lab.link".format(link_f)]

    if nt == "wicked":
        # Find DHCP ifcfg files in Python (avoids shell quoting issues with regex)
        # and remove them one by one with a simple rm command per file
        dhcp_pat = re.compile(r"BOOTPROTO\s*=\s*\S*(dhcp|auto)", re.IGNORECASE)
        for _f in vls("/etc/sysconfig/network"):
            if not _f.startswith("ifcfg-") or _f == "ifcfg-lo":
                continue
            _content = vcat("/etc/sysconfig/network/" + _f)
            if dhcp_pat.search(_content):
                vc += ["--run-command", "rm -f /etc/sysconfig/network/{}".format(_f)]

    if nt in ("nm-keyfile", "cloud-init"):
        # Prevent NM from auto-generating "Wired connection 1" DHCP for unmatched interfaces
        nm_nodauto_f = os.path.join(tmp, "nm_no_auto")
        open(nm_nodauto_f, "w").write("[main]\\nno-auto-default=*\\n")
        vc += ["--run-command", "mkdir -p /etc/NetworkManager/conf.d /etc/NetworkManager/system-connections"]
        vc += ["--upload", "{}:/etc/NetworkManager/conf.d/99-no-auto-default.conf".format(nm_nodauto_f)]
        for _f in vls("/etc/NetworkManager/system-connections"):
            if _f.startswith("."): continue
            vc += ["--run-command", "rm -f '/etc/NetworkManager/system-connections/{}'".format(_f)]

    if nt == "cloud-init":
        # SLES 16 uses cloud-init + NetworkManager. Use cloud-init properly: write a NoCloud
        # seed with network-config v2 and force the NM renderer so netplan is never called.

        # Remove existing netplan YAMLs — they would trigger the netplan renderer whose
        # binary does not exist on SLES 16 (NM reads them directly without the CLI).
        for _yf in vls("/etc/netplan"):
            if _yf.endswith((".yaml", ".yml")):
                vc += ["--run-command", "rm -f '/etc/netplan/{}'".format(_yf)]
        # Remove legacy sysconfig routes — cloud-init fails parsing "default" as an IP address.
        vc += ["--run-command",
               "rm -f /etc/sysconfig/network/routes /etc/sysconfig/network/ifroute-* 2>/dev/null; true"]

        # Build network-config v2: use 0.0.0.0/0 (not "default") for the default route.
        _nc = ["version: 2", "ethernets:", "  id0:"]
        if mac:
            _nc += ["    match:", "      macaddress: \\"{}\\"".format(mac.lower())]
        _nc += ["    dhcp4: false", "    addresses:", "      - {}/{}".format(ip, prefix)]
        if gw:
            _nc += ["    routes:", "      - to: 0.0.0.0/0", "        via: {}".format(gw)]
        if dns or domain:
            _nc.append("    nameservers:")
            if dns:    _nc.append("      addresses: [{}]".format(dns))
            if domain: _nc.append("      search: [{}]".format(domain))

        _seed = "/var/lib/cloud/seed/nocloud"
        _nc_f  = os.path.join(tmp, "ci-network-config")
        open(_nc_f, "w").write("\\n".join(_nc) + "\\n")
        _meta_f = os.path.join(tmp, "ci-meta-data")
        open(_meta_f, "w").write("instance-id: lab-{}\\nlocal-hostname: {}\\n".format(vmname, vmname))
        _ud_f = os.path.join(tmp, "ci-user-data")
        open(_ud_f, "w").write("#cloud-config\\n")
        vc += ["--run-command", "mkdir -p {}".format(_seed)]
        vc += ["--upload", "{}:{}/network-config".format(_nc_f, _seed)]
        vc += ["--upload", "{}:{}/meta-data".format(_meta_f, _seed)]
        vc += ["--upload", "{}:{}/user-data".format(_ud_f, _seed)]

        # Force NoCloud datasource and NM renderer — prevents cloud-init from using netplan.
        _ci_f = os.path.join(tmp, "ci-lab-cfg")
        open(_ci_f, "w").write(
            "datasource_list: ['NoCloud', 'None']\\n"
            "system_info:\\n  network:\\n    renderers: ['network-manager']\\n"
        )
        vc += ["--run-command", "mkdir -p /etc/cloud/cloud.cfg.d"]
        vc += ["--upload", "{}:/etc/cloud/cloud.cfg.d/99-lab.cfg".format(_ci_f)]

    dest, content, chmod, extras = net_config(nt, iface)
    if dest and content:
        net_f = os.path.join(tmp, "net")
        open(net_f,"w").write(content)
        vc += ["--run-command","mkdir -p {}".format(os.path.dirname(dest))]
        vc += ["--upload","{}:{}".format(net_f,dest)]
        if chmod: vc += ["--chmod","{}:{}".format(chmod,dest)]

    for edest, econtent in extras:
        ef = os.path.join(tmp, os.path.basename(edest))
        open(ef,"w").write(econtent)
        vc += ["--upload","{}:{}".format(ef,edest)]

    if nt == "wicked" and (dns or domain):
        dl = []
        if dns:    dl.append("NETCONFIG_DNS_STATIC_SERVERS='{}'".format(dns))
        if domain: dl.append("NETCONFIG_DNS_STATIC_SEARCHLIST='{}'".format(domain))
        dns_f = os.path.join(tmp,"netconfig_dns")
        open(dns_f,"w").write("\\n".join(dl)+"\\n")
        vc += ["--upload","{}:/tmp/.vc_ncdns".format(dns_f)]
        vc += ["--run-command",
               r"""awk 'BEGIN{FS=OFS="="}"""
               r""" /^NETCONFIG_DNS_STATIC_SERVERS=/ || /^NETCONFIG_DNS_STATIC_SEARCHLIST=/ {next}"""
               r""" 1' /etc/sysconfig/network/config > /tmp/.vc_cfg"""
               r""" && cat /tmp/.vc_ncdns >> /tmp/.vc_cfg"""
               r""" && cat /tmp/.vc_cfg > /etc/sysconfig/network/config"""
               r""" && rm -f /tmp/.vc_cfg /tmp/.vc_ncdns"""]
        vc += ["--run-command","netconfig update -f 2>/dev/null || true"]

    fqdn = "{}.{}".format(vmname, domain) if domain else vmname
    vc += ["--run-command",
           "grep -qF ' {}' /etc/hosts || echo '{} {} {}' >> /etc/hosts".format(
               vmname, ip, vmname, fqdn)]
    vc += ["--run-command",
           "systemctl enable sshd 2>/dev/null || systemctl enable ssh 2>/dev/null || true"]
    vc += ["--run-command",
           r"""mkdir -p /etc/ssh/sshd_config.d"""
           r""" && printf 'PermitRootLogin yes\nPasswordAuthentication yes\n'"""
           r""" > /etc/ssh/sshd_config.d/99-lab.conf"""
           r""" ; sed -i -E"""
           r""" -e 's/^#?\s*PermitRootLogin\s+.*/PermitRootLogin yes/'"""
           r""" -e 's/^#?\s*PasswordAuthentication\s+.*/PasswordAuthentication yes/'"""
           r""" /etc/ssh/sshd_config 2>/dev/null; true"""]
    if nt != "cloud-init":
        # For non-cloud-init systems: disable cloud-init entirely so it doesn't interfere.
        ci_dis_f = os.path.join(tmp, "ci_disabled")
        open(ci_dis_f, "w").write("")
        vc += ["--run-command", "mkdir -p /etc/cloud"]
        vc += ["--upload", "{}:/etc/cloud/cloud-init.disabled".format(ci_dis_f)]

    # SUSE JeOS appliance images (confirmed live 2026-08-29 on
    # SLES-16.0-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2) ship an interactive
    # jeos-firstboot wizard (keyboard layout, locale, etc.) that runs on the
    # console on first boot and BLOCKS there indefinitely in any headless,
    # unattended deployment — there's no one at the console to answer it.
    # Confirmed via a live screenshot: the VM never reached multi-user.target
    # (SSH kept rejecting with "System is booting up") because it was stuck
    # showing a "Select keyboard layout" dialog. Disabled unconditionally,
    # like cloud-init above — harmless (`|| true`) on any image that doesn't
    # ship these units at all.
    vc += ["--run-command",
           "systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true"]

    if "config" in vls("/etc/selinux"):
        vc += ["--selinux-relabel"]

    print("    running virt-customize...", flush=True)
    r = subprocess.run(vc)
    sys.exit(r.returncode)
'''

    # Pipe the script to the hypervisor — all values are already baked in as
    # repr() literals so the remote python3 needs no env vars or extra args.
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "root@{}".format(remote_host), "python3 -"],
        input=script.encode(),
    )
    if result.returncode != 0:
        die("virt-customize failed for '{}'".format(vm_name))


def prepare_install_iso(
    vm_name, lab_setup_path, install_type, iso_image,
    mymac, myip, mymask, mygw, mydns, mydomain,
    root_pwd_hash, root_ssh_key=None,
):
    """
    Render the answer file (AutoYaST/Kickstart/Preseed/autoinstall) for a VM.
    Mirrors prepare_install_iso (bash).

    The rendered file is written to lab_setup_path/install_iso/, already
    served over HTTP by the automation VM's web server — nothing needs to be
    copied to the hypervisor.

    NOTE on ROOT_SSH_KEY vs ROOT_SSH_PUBKEY: previously the kickstart/autoyast
    templates echoed the raw `ROOT_SSH_KEY` config value straight into
    authorized_keys, on the assumption that admins keep it in sync with their
    real ~/.ssh/id_rsa.pub — confirmed live 2026-09-17 that this assumption
    doesn't hold in practice (this environment's own lab_creation.cfg
    ROOT_SSH_KEY had drifted from the automation VM's actual id_rsa.pub,
    silently baking an unusable key into every kickstart/autoyast install —
    the VM would provision successfully but be permanently SSH-unreachable,
    "Permission denied (publickey)"). All four install types now use
    ROOT_SSH_PUBKEY instead, which always falls through to the automation
    VM's real, current ~/.ssh/id_rsa.pub (see below) unless root_ssh_key
    points at an actual, existing key-PATH override — matching how ignition/
    combustion/cloud-init already behave. ROOT_SSH_KEY itself is kept as a
    render var only for backward compatibility with any custom templates
    that might still reference it.

    NOTE on ROOT_PWD_HASH: bash used to escape '$' in the hash before this
    point — verified empirically (see lab_creation.bash's prepare_install_iso)
    that this corrupted the hash. Fixed in bash; the raw hash is used
    directly here, with no escaping needed at all.
    """
    itype = resolve_install_type(install_type, iso_image)

    root_ssh_pubkey = ""
    if root_ssh_key and Path(root_ssh_key + ".pub").is_file():
        root_ssh_pubkey = Path(root_ssh_key + ".pub").read_text().strip()
    elif Path.home().joinpath(".ssh", "id_rsa.pub").is_file():
        root_ssh_pubkey = Path.home().joinpath(".ssh", "id_rsa.pub").read_text().strip()

    log("- Rendering {} answer file for \"{}{}{}\"".format(itype, _RED, vm_name, _RESET))

    if itype == "autoinstall":
        # Ubuntu 22+ subiquity autoinstall — written directly (not via
        # process_template/eval) to keep this well away from any shell
        # re-interpretation of the password hash.
        out_dir = Path(lab_setup_path) / "install_iso" / vm_name
        out_dir.mkdir(parents=True, exist_ok=True)
        user_data = (
            "#cloud-config\n"
            "autoinstall:\n"
            "  version: 1\n"
            "  locale: en_US.UTF-8\n"
            "  keyboard:\n"
            "    layout: us\n"
            "  network:\n"
            "    network:\n"
            "      version: 2\n"
            "      ethernets:\n"
            "        id0:\n"
            "          match:\n"
            "            macaddress: {mymac}\n"
            "          set-name: eth0\n"
            "          dhcp4: no\n"
            "          addresses:\n"
            "            - {mycidr}\n"
            "          gateway4: {mygw}\n"
            "          nameservers:\n"
            "            addresses: [{mydns}]\n"
            "            search: [{mydomain}]\n"
            "  storage:\n"
            "    layout:\n"
            "      name: lvm\n"
            "  user-data:\n"
            "    disable_root: false\n"
            "    ssh_pwauth: true\n"
            "    users:\n"
            "      - name: root\n"
            "        lock_passwd: false\n"
            "        hashed_passwd: {root_pwd_hash}\n"
            "        ssh_authorized_keys:\n"
            "          - {root_ssh_pubkey}\n"
            "  ssh:\n"
            "    install-server: true\n"
            "    allow-pw: true\n"
            "  late-commands:\n"
            "    - mkdir -p /target/etc/ssh/sshd_config.d\n"
            "    - printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /target/etc/ssh/sshd_config.d/99-lab.conf\n"
            # No `identity:` section above (it would force a separate default
            # user this project doesn't want — root-only access is the
            # point) — but `identity` is also autoinstall's only mechanism
            # for setting /etc/hostname at install time, so without it
            # curtin leaves the installed system's hostname at whatever the
            # live installer environment defaulted to ("localhost", not even
            # "ubuntu") — confirmed live 2026-09-03 (`hostname` inside the
            # freshly-installed, fully-reachable VM read back "localhost").
            # meta-data's local-hostname doesn't help either: it's a
            # cloud-init concept, and cloud-init's own NoCloud datasource
            # (the seed cdrom) is detached again right after this install
            # finishes, so nothing ever re-reads it on a later real boot.
            # Set directly instead, the same way the sshd config above is.
            # vm_name is quoted here (found in code review 2026-09-05) —
            # this late-command runs as a real shell command inside the
            # target, and vm_name (a lab.json node hostname) is never
            # validated against shell metacharacters anywhere in this
            # codebase.
            "    - echo \"{vm_name}\" > /target/etc/hostname\n"
        ).format(
            # mymac/myip/mymask/mygw/mydns/mydomain/root_pwd_hash/
            # root_ssh_pubkey are all bare (myip/mymask/mygw/mydns/mydomain
            # not even hand-quoted) YAML scalars in this hand-built
            # #cloud-config document — found in code review 2026-09-05,
            # confirmed live by direct execution: a mydomain value with an
            # embedded colon+newline injected two new, unrelated top-level
            # keys straight into the rendered YAML, the exact same bug
            # already found and fixed in setup_harvester_cluster.py's own
            # hand-built YAML. yaml_scalar() escapes each value correctly
            # regardless of which one a lab.json author gets creative with.
            mymac=yaml_scalar(mymac), mycidr=yaml_scalar("{}/{}".format(myip, mymask)),
            mygw=yaml_scalar(mygw), mydns=yaml_scalar(mydns), mydomain=yaml_scalar(mydomain),
            root_pwd_hash=yaml_scalar(root_pwd_hash), root_ssh_pubkey=yaml_scalar(root_ssh_pubkey),
            vm_name=vm_name,
        )
        (out_dir / "user-data").write_text(user_data)
        (out_dir / "meta-data").write_text(
            "instance-id: {}\nlocal-hostname: {}\n".format(vm_name, vm_name))
    else:
        ext = {"autoyast": "xml", "kickstart": "ks", "preseed": "preseed"}.get(itype)
        tpl = Path(lab_setup_path) / "install_iso" / "template_{}".format(itype)
        out = Path(lab_setup_path) / "install_iso" / "{}.{}".format(vm_name, ext)
        if not tpl.is_file():
            die("install_iso template not found: {}".format(tpl))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(process_template(str(tpl), {
            "ROOT_PWD_HASH": root_pwd_hash,
            "ROOT_SSH_KEY": root_ssh_key or "",
            "ROOT_SSH_PUBKEY": root_ssh_pubkey,
            "_vm_name": vm_name,
            "myip": myip, "mymask": mymask, "mygw": mygw,
            "mydns": mydns, "mydomain": mydomain, "mymac": mymac,
        }))


# ── Helm ──────────────────────────────────────────────────────────────────────

def setup_helm(hostname, clu_name, online=False, automation_host="automation"):
    """
    Install Helm on a remote K8s node (mirrors setup_helm).

    online=True  → downloads directly from GitHub.
    online=False → downloads from a local automation VM.

    NOTE: default is False (offline/automation-VM path) to match bash's real
    default — bash checks `[[ "$online" == "1" ]]`, and the `online` JSON
    field is absent from every real lab config found in this repo, so an
    unset bash variable (empty string) takes the offline branch. An earlier
    version of this function defaulted to True, inverting that behaviour for
    any caller that didn't explicitly pass online=.
    """
    log("Setting up Helm on cluster '{}'".format(clu_name))
    if online:
        ssh_run(hostname,
                "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash")
    else:
        ssh_run(hostname, "curl http://{}/helm/install_helm.sh | bash -".format(automation_host))


def helm_repo_add(hostname, repo_name, repo_url):
    """Add and update a Helm repository on a remote node (mirrors helm_repo_add)."""
    log("Adding Helm repo '{}'".format(repo_name))
    ssh_run(hostname, "helm repo add {} {}".format(repo_name, repo_url))
    ssh_run(hostname, "helm repo update")


# ── Template processing ───────────────────────────────────────────────────────

def process_template(template_file, variables):
    """
    Render a template file exactly as bash's process_templates does:
    `eval "cat <<EOF\n$(cat template_file)\nEOF\n"` — i.e. full unquoted-heredoc
    expansion: $VAR / ${VAR} substitution AND $(...) / `...` command
    substitution, using the given variables as environment.

    Shells out to bash to run that exact construct rather than hand-porting
    heredoc/eval semantics in Python. This matters: some templates (e.g.
    cloud-init.template_user-data, install_iso.template_kickstart) use $(...)
    command substitution, which Python's string.Template cannot express at
    all — a previous version of this function used string.Template and would
    have silently left those command substitutions un-expanded. Shelling out
    is the only way to guarantee identical output without a way to test this
    live.

    Args:
        template_file : path to the template file.
        variables     : dict of variable name -> value, exported into the
                         subshell's environment for the expansion.

    Returns the expanded text.
    """
    script = 'eval "cat <<EOF\n$(cat {})\nEOF\n"'.format(shlex.quote(str(template_file)))
    env = os.environ.copy()
    env.update({k: "" if v is None else str(v) for k, v in variables.items()})
    # stdout=PIPE/stderr=PIPE/universal_newlines=True, not capture_output=/
    # text= (both Python 3.7+ only): confirmed live 2026-08-30 that this
    # function had never actually been exercised by any test until
    # 30_setup_harvester_cluster_test.py's template-rendering checks —
    # tests/run_tests.sh's own container python3 is 3.6.15 (see
    # 09_spacecmd_common_test.py's mock.call notes for the same interpreter),
    # so capture_output/text raised TypeError there even though real
    # production (python3.11 everywhere) was never affected.
    result = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, env=env)
    if result.returncode != 0:
        die("process_template failed on '{}': {}".format(template_file, result.stderr.strip()))
    return result.stdout


# ── Salt ──────────────────────────────────────────────────────────────────────

def setup_salt(vm_name, salt_states, lab_setup_path):
    """
    Create a salt-ssh roster and state files for a VM (mirrors setup_salt).

    salt_states : Space-separated list of state names.
    """
    salt_dir = Path.home() / "salt-ssh"
    (salt_dir / "states").mkdir(parents=True, exist_ok=True)

    (salt_dir / "roster").write_text(
        "managed:\n"
        "  host: {}\n"
        "  user: root\n"
        "  sudo: False\n"
        "  priv: {}/.ssh/id_rsa\n".format(vm_name, Path.home())
    )

    for state in salt_states.split():
        tmpl = Path(lab_setup_path) / "salt-ssh" / state
        out  = salt_dir / "states" / state
        out.write_text(process_template(str(tmpl), {"_vm_name": vm_name}))


# ── OS detection (local) ──────────────────────────────────────────────────────

def find_os():
    """
    Return (os_id, version_id, arch) for the local machine (mirrors find_OS).
    Reads /etc/os-release and uname -m.
    """
    os_id = version_id = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            m = re.match(r'^ID="?([^"]+)"?', line)
            if m:
                os_id = m.group(1)
            m = re.match(r'^VERSION_ID="?([^"]+)"?', line)
            if m:
                version_id = m.group(1)
    except IOError:
        pass
    arch = subprocess.run(
        ["uname", "-m"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True
    ).stdout.strip()
    return os_id, version_id, arch


def boot_packages_for(os_id):
    """
    Return the space-separated BOOT_PACKAGES list for a detected OS id, or
    raise via die() for unsupported OSes. Mirrors install_packages (bash).

    NOTE: bash's install_packages only ever assigns the BOOT_PACKAGES variable
    — nothing in the bash codebase reads it afterwards (verified with grep);
    it's effectively dead code today. Kept here as a pure function (returning
    the value bash would have assigned) rather than a no-op, since a future
    caller may still want it and there's no behavioural risk either way.
    """
    if os_id == "sles":
        return "vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
    elif os_id == "sle-micro":
        return "vim-small iptables NetworkManager-cloud-setup wget git"
    elif os_id == "opensuse-leap":
        return "vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
    else:
        die("ERROR - OS not supported yet")


# ── Misc ──────────────────────────────────────────────────────────────────────

def check_exists(needle, haystack):
    """Return True if needle is a whole word in the space-separated haystack (mirrors check_exists)."""
    return " {} ".format(needle) in " {} ".format(haystack)


def reboot_vm(virt_srv, vm_name, remote_host=None):
    """
    Reboot a VM, forcing a power cycle if it doesn't respond within 120s.
    Thin wrapper — body moved to backends.LibvirtBackend.reboot_vm().

    remote_host: see _list_domain_macs()'s docstring — optional, only needed
    by the SSH fallback (and only reached at all when the guest isn't
    reachable over SSH directly, in which case this same host is exactly
    where a virsh fallback would need to reach anyway).
    """
    from backends import LibvirtBackend
    LibvirtBackend(virt_srv, remote_host=remote_host).reboot_vm(vm_name)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(cmd, error_msg):
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(error_msg)


def _remote(hostname, cmd, check=True):
    return ssh_run(hostname, cmd, check=check)


def _remote_dns_add(server, zone_file, record):
    _remote(server,
            "grep -qF '{}' {} 2>/dev/null || echo '{}' >> {}".format(
                record, zone_file, record, zone_file))


def _dns_add_line(zone_file, record):
    """
    Append `record` to zone_file unless a line exactly equal to it already
    exists. Mirrors the intent of bash's `grep -qi <pattern> || echo <record>
    >> zone_file` dedup checks in add_to_dns/add_dns_to_named_rr.

    NOTE: an earlier version of this took a separate `dedup_key` substring
    (e.g. just the short hostname or the last IP octet) and checked whether
    that fragment appeared ANYWHERE in the file — which is a real false-
    positive risk given this project's hostnames (node1/node10/node101,
    n1/n10/n11, …): adding "node1" would wrongly be skipped as a duplicate
    once "node10" was already present, since "node1" is a substring of
    "node10". This project's actual bash also had a related but different
    bug here (see lab_creation.bash's add_to_dns — a stray quote character
    meant the PTR dedup check never matched anything, causing duplicates
    instead of false-skips). Exact-line matching on the full record avoids
    both failure modes.
    """
    zone_file = Path(zone_file)
    zone_file.touch()
    lines = zone_file.read_text().splitlines()
    if record not in lines:
        zone_file.write_text("\n".join(lines + [record]) + "\n")


def _dns_remove_line(zone_file, record):
    """
    Remove any line exactly equal to `record` from zone_file. Mirrors bash's
    `sed "/<full record text>/d"` calls in del_from_dns/add_service_dns, which
    always passed the complete record text as the delete pattern (not a bare
    hostname/octet fragment) — exact-line matching here preserves that same
    precision, avoiding substring false-positives (see _dns_add_line's note).
    """
    zone_file = Path(zone_file)
    if zone_file.exists():
        lines = [l for l in zone_file.read_text().splitlines() if l != record]
        zone_file.write_text("\n".join(lines) + "\n")


def _dns_append_line(zone_file, record):
    zone_file = Path(zone_file)
    zone_file.touch()
    with zone_file.open("a") as f:
        f.write(record + "\n")


def _detect_dns():
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            m = re.match(r'^nameserver\s+([\d.]+)', line)
            if m:
                return m.group(1)
    except IOError:
        pass
    return ""


def _detect_gateway():
    result = subprocess.run(
        ["ip", "route", "list", "to", "default"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    )
    m = re.search(r'via\s+([\d.]+)', result.stdout)
    return m.group(1) if m else ""


def _detect_domain():
    import socket as _socket
    fqdn  = _socket.getfqdn()
    short = _socket.gethostname().split(".")[0]
    return fqdn[len(short) + 1:] if fqdn.startswith(short + ".") else ""


def _reverse_ip(ip):
    parts = ip.split(".")
    return ".".join(reversed(parts[:-1])) if len(parts) == 4 else ""


def _detect_netmask():
    """
    Return the CIDR prefix length of the default-route network device.
    Mirrors load_vm_vars' mymask auto-detect (bash used `ip -o -f inet addr
    show ${_default_dev} | egrep ... | ipcalc -p ... | cut -d= -f2` — same
    end result via a shorter path: the device is the same one carrying the
    default route, already needed for _detect_gateway, and its prefix length
    is directly in `ip -o addr show` output, no ipcalc round-trip needed).
    """
    route = subprocess.run(
        ["ip", "route", "list", "to", "default"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    ).stdout
    dev_match = re.search(r"\bdev\s+(\S+)", route)
    if not dev_match:
        return ""
    addr = subprocess.run(
        ["ip", "-o", "-f", "inet", "addr", "show", dev_match.group(1)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    ).stdout
    m = re.search(r"inet\s+[\d.]+/(\d{1,2})", addr)
    return m.group(1) if m else ""
