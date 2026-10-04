"""
Shared spacecmd and mgr-sync helpers for SUSE Multi-Linux Manager (SMLM) and Uyuni servers.

The same server tooling is reached through an exec wrapper that depends on how the server is
deployed: install_smlm.py (Kubernetes) and install_uyuni.py (podman, via mgradm) pass their own
wrapper as `exec_prefix`, for example "kubectl exec -n {ns} deploy/uyuni -c uyuni --" or
"mgrctl exec --".

Commands use the spacecmd native subcommands where they exist. Namespaces without a spacecmd
module (ansible, access, contentmanagement, saltkey, audit, and the extra system.scap calls) go
through the generic `spacecmd api` passthrough, which calls the XML-RPC method directly.

Idempotency: the ensure_* functions look up the current state first and only create what is
missing. Where the server has no list or lookup call, the check matches on the command's output
text, and the docstring of each function states the match used. ensure_appstreams detects an
already-enabled module from the server's duplicate-stream fault text.

One-shot actions are never run by the automatic install flow, because each call starts real work
with no duplicate check. These are: scheduling an Ansible playbook, CLM build and promote, SCAP
scans, and recurring schedules. Each has its own function here and a flag in the install scripts
(--run-ansible-playbooks, --run-clm-actions, --run-scap-scans, --run-recurring-schedules).

Feature areas:
  - Activation keys: the key itself, its packages, system-group links and AppStreams.
  - Software and configuration channels, including channel sharing between organizations.
  - Organizations: session scope follows the authenticated user, so once a session is for an
    organization's admin, the existing ensure_* functions apply to that organization.
  - Custom access groups (RBAC) and user accounts.
  - Ansible control nodes: the Ansible Control Node entitlement is enabled on a registered system,
    and playbooks and inventories stay on that system's filesystem, managed outside this module.
  - Content Lifecycle Management: projects, filters, environments and sources, with software
    channels only.
  - SCAP audits (the legacy XCCDF scan path and the SMLM 5.2 beta policy calls) and CVE/OVAL
    patch-status queries.
  - Environment topology: system groups, activation-key-to-group links, custom-info tags, and
    recurring highstate or custom schedules, which the install flow runs only when asked to.
  - Client registration: registers an existing host as a Salt minion. It runs the bootstrap script
    with an activation key, then accepts the pending minion key through the saltkey API. The minion
    ID is the client's FQDN.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import base64
import hashlib
import json
import re
import shlex
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from lab_creation import ssh_run, scp_to, die, warn, error


def run_provisioning_step(label, func, *args, retries=1, retry_delay=15, **kwargs):
    """
    Run one config-provisioning step from install_smlm.py's or install_uyuni.py's setup orchestration, and report a
    failure without stopping the remaining steps.

    A die() raised by one step (an uncaught SystemExit) would otherwise unwind out of the whole orchestration function and
    skip every step after it. This function catches it and reports through lab_creation.error(), so unrelated steps still
    run. Steps keep their source order, so a step that depends on an earlier failed step reports its own error.

    retries and retry_delay: some steps depend on state that another process is still producing, for example a client that
    background workers have not registered yet. With retries > 1 the step is retried up to `retries` times, sleeping
    `retry_delay` seconds between attempts, before the failure is reported. The default is no retry, so a real configuration
    error is reported at once.

    The wait is bounded. A dependency that takes longer than retries * retry_delay seconds is picked up by a later
    configuration run.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            func(*args, **kwargs)
            return
        except SystemExit as e:
            last_err = e
            if attempt < retries:
                warn("config step '{}' failed (attempt {}/{}) — its dependency may still be "
                     "catching up elsewhere in the automation; retrying in {}s".format(
                         label, attempt, retries, retry_delay))
                time.sleep(retry_delay)
    error("config step '{}' failed after {} attempt{} — see the ERROR(s) above; continuing "
          "with the rest of the configuration".format(
              label, retries, "" if retries == 1 else "s"))


def _run(hostname, exec_prefix, remote_cmd, **kwargs):
    """
    Run `remote_cmd` on the server reached through `exec_prefix`, over SSH to `hostname`. The two exec_prefix shapes
    are not interchangeable:

      - kubectl (SMLM), e.g. "kubectl exec -n {ns} deploy/uyuni -c uyuni --". The `--` marks the rest as a literal argv for
        the container, so the command is appended as a plain string.
      - mgrctl (Uyuni), e.g. "mgrctl exec --". mgrctl takes the whole remote command as one quoted argument, so remote_cmd is
        re-quoted as a single argument. Otherwise the outer shell splits multi-token commands, and some parts run on the host
        instead of in the container.

    Neither mgrctl exec nor kubectl exec forwards stdin by default. When input_text is given, the matching flag is added
    (-i for mgrctl, -i or --stdin for kubectl), so the input reaches the container process.
    """
    prefix = exec_prefix.strip()
    needs_stdin = bool(kwargs.get("input_text"))
    if prefix.startswith("mgrctl exec"):
        stdin_flag = "-i " if needs_stdin else ""
        full_cmd = "mgrctl exec {}{}".format(stdin_flag, shlex.quote(remote_cmd))
    else:
        if needs_stdin and prefix.startswith("kubectl exec") and " -i " not in " {} ".format(prefix):
            prefix = prefix.replace("kubectl exec ", "kubectl exec -i ", 1)
        full_cmd = "{} {}".format(prefix, remote_cmd)
    return ssh_run(hostname, full_cmd, **kwargs)


def _fault_check(r):
    """
    Normalise a failed XML-RPC call to a non-zero exit code. spacecmd can exit 0 even when the server rejected the call,
    and then prints the fault to stderr, for example "ERROR: <Fault 2800: ...>". The shared passthrough helpers check for that
    output and report a failure, so every caller that checks the return code sees it.
    """
    if r.returncode == 0 and "ERROR: <Fault" in ((r.stdout or "") + (r.stderr or "")):
        r.returncode = 1
    return r


def _spacecmd(hostname, exec_prefix, args):
    return _fault_check(_run(hostname, exec_prefix, "spacecmd -- {}".format(args), check=False, capture=True))


def ensure_spacecmd_config(hostname, exec_prefix, username, password):
    """
    Write ~/.spacecmd/config (directory mode 700, file mode 600) inside the server container or host, so spacecmd
    calls authenticate without a prompt and the password never appears in argv or ps output. The file is overwritten on every
    call, so a changed admin password never leaves a stale cached credential.

    `server` is always written as "localhost". The command runs inside the server's own container, and that container cannot
    reach the externally routed hostname over HTTP. localhost is the one address that works for both deployment shapes.
    """
    config_text = "[spacecmd]\nserver=localhost\nusername={}\npassword={}\n".format(username, password)
    cmd = ("sh -c 'mkdir -p ~/.spacecmd && chmod 700 ~/.spacecmd && "
           "cat > ~/.spacecmd/config && chmod 600 ~/.spacecmd/config'")
    r = _run(hostname, exec_prefix, cmd, input_text=config_text, check=False)
    if r.returncode != 0:
        die("could not write spacecmd credentials on {}".format(hostname))


def activation_key_exists(hostname, exec_prefix, key_name):
    """
    Return True if `key_name` appears in the output of `spacecmd activationkey_list`. The list output is the only existence
    check used, because the exit code of activationkey_details for a missing key is not documented.
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_list")
    return key_name in (r.stdout or "")


def resolve_activation_key_name(hostname, exec_prefix, key_name):
    """
    Return the name the server stored for the key given as `key_name`. The server prefixes the current organization's
    numeric ID, so "dev-key" is stored as "1-dev-key". Commands that match the name exactly need the stored name. The function
    returns the first activationkey_list line that equals key_name or ends with "-" + key_name. It returns key_name unchanged
    when nothing matches, for example before the key exists.
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_list")
    lines = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
    if key_name in lines:
        return key_name
    for line in lines:
        if line.endswith("-" + key_name):
            return line
    return key_name


def ensure_activation_key(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently create the activation key described by the <prefix>_activation_key* config keys, where prefix is "smlm"
    or "uyuni". Creation accepts only the name, description, base channel, universal default and entitlements. The optional
    follow-ups are applied after it, each as a separate spacecmd call: child channels, config channels and deployment, groups,
    and contact method. Does nothing if <prefix>_activation_key is not set. ensure_spacecmd_config() must run first.
    """
    def k(suffix):
        return cfg.get("{}_activation_key{}".format(prefix, suffix))

    key_name = k("")
    if not key_name:
        return

    if activation_key_exists(hostname, exec_prefix, key_name):
        print("  Activation key '{}' already exists — leaving it alone".format(key_name))
        return

    base_channel = k("_base_channel")
    if not base_channel:
        die("{p}_activation_key_base_channel is required to create activation key '{k}'".format(
            p=prefix, k=key_name))

    desc = k("_desc") or key_name
    create_args = "activationkey_create -n {n} -d {d} -b {b}".format(
        n=shlex.quote(key_name), d=shlex.quote(desc), b=shlex.quote(base_channel))
    if (k("_universal_default") or "false") == "true":
        create_args += " -u"
    entitlements = k("_entitlements")
    if entitlements:
        create_args += " -e {}".format(shlex.quote(entitlements))

    r = _spacecmd(hostname, exec_prefix, create_args)
    if r.returncode != 0:
        die("could not create activation key '{}': {}".format(key_name, (r.stderr or r.stdout or "").strip()))
    print("  Created activation key '{}'".format(key_name))

    # Uyuni auto-prepends the org id to whatever name was just given (e.g.
    # "1-dev-key" -> stored as "1-1-dev-key") — resolve to the real name
    # now so every follow-up command below (which needs an exact match,
    # unlike the substring-tolerant existence check above) targets the key
    # that actually exists. See resolve_activation_key_name()'s docstring.
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    child_channels = (k("_child_channels") or "").split()
    if child_channels:
        # The call fails if a listed child channel is not on the server, for example a managertools channel
        # that was never enabled with mgr-sync. The return code is checked, so the failure is reported.
        r = _spacecmd(hostname, exec_prefix, "activationkey_addchildchannels {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(c) for c in child_channels)))
        if r.returncode != 0:
            warn("could not link child channels ({}) to activation key '{}' — check they're "
                 "actually synced on the server (`mgr-sync add channels`), not just referenced "
                 "in {}_activation_key_child_channels: {}".format(
                     ", ".join(child_channels), key_name, prefix,
                     (r.stderr or r.stdout or "").strip()))

    config_channels = (k("_config_channels") or "").split()
    if config_channels:
        _spacecmd(hostname, exec_prefix, "activationkey_addconfigchannels {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(c) for c in config_channels)))

    if (k("_enable_config_deployment") or "false") == "true":
        _spacecmd(hostname, exec_prefix, "activationkey_enableconfigdeployment {}".format(shlex.quote(key_name)))

    groups = (k("_groups") or "").split()
    if groups:
        _spacecmd(hostname, exec_prefix, "activationkey_addgroups {} {}".format(
            shlex.quote(key_name), " ".join(shlex.quote(g) for g in groups)))

    contact_method = k("_contact_method")
    if contact_method:
        _spacecmd(hostname, exec_prefix, "activationkey_setcontactmethod {} {}".format(
            shlex.quote(key_name), shlex.quote(contact_method)))


def ensure_appstreams(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently enable each "module:stream" pair in <prefix>_activation_key_appstreams (space-separated) on
    <prefix>_activation_key, through the generic 'api' passthrough calling activationkey.addAppStreams. The server has no list
    call for enabled AppStreams. A module that is already enabled fails with the duplicate-stream fault, and that failure counts
    as success. The function can therefore run on every configuration pass, including on a key that already exists. Does
    nothing if either field is unset.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = cfg.get("{}_activation_key_appstreams".format(prefix)) or ""
    if not key_name or not spec:
        return
    pairs = spec.split()
    for pair in pairs:
        if ":" not in pair:
            die("invalid {}_activation_key_appstreams entry '{}': expected 'module:stream'".format(prefix, pair))
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    for pair in pairs:
        module, stream = pair.split(":", 1)
        r = _api_call(hostname, exec_prefix, "activationkey.addAppStreams",
                      [key_name, [{"module": module, "stream": stream}]])
        out = "{}\n{}".format(r.stdout or "", r.stderr or "").lower()
        if r.returncode == 0:
            print("  AppStream '{}' enabled on '{}'".format(pair, key_name))
        elif "already exists in the activation key" in out:
            print("  AppStream '{}' already enabled on '{}' — leaving it alone".format(pair, key_name))
        else:
            die("could not add appstream '{}' to activation key '{}': {}".format(
                pair, key_name, (r.stderr or r.stdout or "").strip()))


def activation_key_packages(hostname, exec_prefix, key_name):
    """
    Returns the set of package names currently on activation key
    `key_name`, via spacecmd's native activationkey_listpackages (wraps
    activationkey.getDetails' packages array). Name-only — spacecmd's own
    listing/adding commands don't surface the underlying API's optional
    per-package 'arch' field, so this module doesn't attempt arch-qualified
    package matching either (see ensure_activation_key_packages).
    """
    r = _spacecmd(hostname, exec_prefix, "activationkey_listpackages {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_packages(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently ensure every package in <prefix>_activation_key_packages (space-separated) is on <prefix>_activation_key,
    through activationkey_addpackages. The current list from activationkey_listpackages is compared, and only the missing
    packages are added. Names only; architecture qualifiers are not supported. Unlike the creation-time follow-ups, this runs
    on every configuration pass. Does nothing if either field is unset.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_packages".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_packages(hostname, exec_prefix, key_name)
    missing = [p for p in spec if p not in existing]
    if not missing:
        print("  Activation key '{}' already has all requested packages — leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addpackages {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(p) for p in missing)))
    if r.returncode != 0:
        die("could not add packages to activation key '{}': {}".format(
            key_name, (r.stderr or r.stdout or "").strip()))
    print("  Added {} package(s) to activation key '{}': {}".format(len(missing), key_name, ", ".join(missing)))


def ensure_channels_synced(hostname, exec_prefix, channels):
    """
    Trigger 'mgr-sync add channel <label>' for each label in `channels` that is not already in the output of
    'spacecmd softwarechannel_list'. One channel is added per invocation. Does nothing for an empty list. mgr-sync uses SCC
    credentials, which are configured at install time and are separate from spacecmd's. ensure_spacecmd_config() must run
    first.
    """
    if not channels:
        return
    existing = _spacecmd(hostname, exec_prefix, "softwarechannel_list").stdout or ""
    for ch in channels:
        if ch in existing:
            continue
        print("  Syncing channel '{}' (not yet present) …".format(ch))
        r = _run(hostname, exec_prefix, "mgr-sync add channel {}".format(shlex.quote(ch)), check=False)
        if r.returncode != 0:
            die("could not sync channel '{}'".format(ch))


def pending_channels(hostname, exec_prefix, channels):
    """
    Return the subset of `channels` that have not finished syncing. An empty set means every channel is ready. A channel is
    ready when its reposync log, /var/log/rhn/reposync/<label>.log inside the server container, ends with "Sync completed.".
    This is the same signal the channel sync monitor in install_smlm.py uses. The check runs through the module's exec_prefix,
    so it works for both podman and Kubernetes deployments. Callers use it for a one-shot decision, without a blocking wait.
    """
    pending = set()
    for ch in channels:
        log_path = "/var/log/rhn/reposync/{}.log".format(ch)
        r = _run(hostname, exec_prefix,
                 "test -f {p} && tail -n 3 {p}".format(p=shlex.quote(log_path)), check=False)
        if r.returncode == 0 and "Sync completed." in (r.stdout or ""):
            continue
        pending.add(ch)
    return pending


def wait_for_channels_synced(hostname, exec_prefix, channels, timeout=1800, poll_interval=30):
    """
    Block until every label in `channels` has a completed reposync, as reported by pending_channels(). ensure_channels_synced()
    only checks that the channels exist and starts missing syncs. It does not wait for a sync that is already running.

    Registering a client against an activation key whose channels are still syncing can leave the client with an incomplete
    subscription, so registration waits here first.

    timeout=None waits without a deadline, which a background retry worker relies on. Any other value dies after that many
    seconds, listing the channels that are still pending. An empty list is a no-op.
    """
    if not channels:
        return
    remaining = set(channels)
    deadline = None if timeout is None else time.time() + timeout
    announced = False
    while True:
        remaining = pending_channels(hostname, exec_prefix, remaining)
        if not remaining:
            break
        if deadline is not None and time.time() >= deadline:
            die("timed out after {}s waiting for channel(s) to finish syncing: {}".format(
                timeout, ", ".join(sorted(remaining))))
        if not announced:
            print("  Waiting for channel(s) to finish syncing before continuing: {} …".format(
                ", ".join(sorted(remaining))))
            announced = True
        time.sleep(poll_interval)
    if announced:
        print("  All required channels are now fully synced")


def _stage_remote_file(hostname, exec_prefix, remote_path, content):
    """Writes `content` to `remote_path` (inside exec_prefix's target) via
    stdin — avoids ever embedding file content as a shell-quoted argv
    string. Caller is responsible for removing `remote_path` afterwards."""
    r = _run(hostname, exec_prefix, "cat > {}".format(shlex.quote(remote_path)), input_text=content, check=False)
    return r.returncode == 0


def config_channel_exists(hostname, exec_prefix, label):
    """
    Whether `label` already appears in `spacecmd configchannel_list`'s
    output. No dedicated existence-check subcommand exists (the underlying
    configchannel.channelExists XML-RPC method has no spacecmd wrapper) —
    same grep-the-list idiom as activation_key_exists.
    """
    r = _spacecmd(hostname, exec_prefix, "configchannel_list")
    return label in (r.stdout or "")


def ensure_config_channel_exists(hostname, exec_prefix, label, name, desc, chan_type="normal"):
    """
    Idempotently create a config channel. The -t/--type option accepts 'normal' or 'state'. The Uyuni and SMLM 5.1 documentation
    does not list that option, so check 'spacecmd help configchannel_create' on the target before relying on it.
    """
    if config_channel_exists(hostname, exec_prefix, label):
        print("  Config channel '{}' already exists — leaving it alone".format(label))
        return
    r = _spacecmd(hostname, exec_prefix, "configchannel_create -n {n} -l {l} -d {d} -t {t}".format(
        n=shlex.quote(name), l=shlex.quote(label), d=shlex.quote(desc), t=shlex.quote(chan_type)))
    if r.returncode != 0:
        die("could not create config channel '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created config channel '{}' (type: {})".format(label, chan_type))


def ensure_config_file(hostname, exec_prefix, label, path, content,
                        owner=None, group=None, mode=None, binary=False):
    """
    Idempotently ensure `path` exists with `content` in config channel `label`, through configchannel_addfile, which creates or
    updates the path. spacecmd's -f option reads a local file, so the content is first staged to a remote temporary file. The
    push is skipped when the sha256 of `content` already appears in the configchannel_filedetails output for that path.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    details = _spacecmd(hostname, exec_prefix, "configchannel_filedetails {} {}".format(
        shlex.quote(label), shlex.quote(path)))
    if details.returncode == 0 and digest in (details.stdout or ""):
        print("  Config file '{}' on '{}' already up to date — skipping".format(path, label))
        return

    remote_tmp = "/tmp/.lab-cfgfile-{}".format(hashlib.sha1("{}:{}".format(label, path).encode()).hexdigest()[:12])
    if not _stage_remote_file(hostname, exec_prefix, remote_tmp, content):
        die("could not stage config file content for '{}' on channel '{}'".format(path, label))

    add_args = "configchannel_addfile -c {c} -p {p} -f {f} -y".format(
        c=shlex.quote(label), p=shlex.quote(path), f=shlex.quote(remote_tmp))
    if owner:
        add_args += " -o {}".format(shlex.quote(owner))
    if group:
        add_args += " -g {}".format(shlex.quote(group))
    if mode:
        add_args += " -m {}".format(shlex.quote(mode))
    if binary:
        add_args += " -b"

    r = _spacecmd(hostname, exec_prefix, add_args)
    _run(hostname, exec_prefix, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
    if r.returncode != 0:
        die("could not push config file '{}' to channel '{}': {}".format(
            path, label, (r.stderr or r.stdout or "").strip()))
    print("  Pushed config file '{}' to channel '{}'".format(path, label))


def ensure_init_sls(hostname, exec_prefix, label, content):
    """
    The same as ensure_config_file, for a state channel's init.sls. spacecmd manages that file with
    configchannel_updateinitsls, and its path on the server is always /init.sls.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    details = _spacecmd(hostname, exec_prefix, "configchannel_filedetails {} /init.sls".format(shlex.quote(label)))
    if details.returncode == 0 and digest in (details.stdout or ""):
        print("  init.sls on '{}' already up to date — skipping".format(label))
        return

    remote_tmp = "/tmp/.lab-cfgfile-{}-initsls".format(hashlib.sha1(label.encode()).hexdigest()[:12])
    if not _stage_remote_file(hostname, exec_prefix, remote_tmp, content):
        die("could not stage init.sls content for state channel '{}'".format(label))

    r = _spacecmd(hostname, exec_prefix, "configchannel_updateinitsls -c {} -f {} -y".format(
        shlex.quote(label), shlex.quote(remote_tmp)))
    _run(hostname, exec_prefix, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
    if r.returncode != 0:
        die("could not push init.sls to state channel '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Pushed init.sls to state channel '{}'".format(label))


def ensure_config_channels(hostname, exec_prefix, cfg, prefix):
    """
    Create the config channels listed in <prefix>_config_channels. Each entry is a dict with label, name, description, type,
    files (path, content, owner, group, mode, binary) and init_sls. type defaults to "normal". A "state" channel takes its
    init.sls from init_sls, and its other files are handled the same way. Does nothing if the field is unset or empty.
    ensure_spacecmd_config() must run first.
    """
    channels = cfg.get("{}_config_channels".format(prefix)) or []
    for chan in channels:
        label = chan.get("label")
        if not label:
            die("{}_config_channels: an entry is missing required 'label'".format(prefix))
        name = chan.get("name") or label
        desc = chan.get("description") or name
        chan_type = chan.get("type") or "normal"

        ensure_config_channel_exists(hostname, exec_prefix, label, name, desc, chan_type)

        if chan_type == "state" and chan.get("init_sls"):
            ensure_init_sls(hostname, exec_prefix, label, chan["init_sls"])

        for f in chan.get("files") or []:
            path = f.get("path")
            if not path:
                die("config channel '{}': a file entry is missing required 'path'".format(label))
            content = f.get("content")
            if content is None:
                die("config channel '{}': file '{}' is missing required 'content'".format(label, path))
            ensure_config_file(hostname, exec_prefix, label, path, content,
                                owner=f.get("owner"), group=f.get("group"), mode=f.get("mode"),
                                binary=bool(f.get("binary")))


def distribution_exists(hostname, exec_prefix, name):
    """
    Return True if `name` is an exact line in the output of `spacecmd distribution_list`, which prints one name per line with
    no header.
    """
    r = _spacecmd(hostname, exec_prefix, "distribution_list")
    return name in [line.strip() for line in (r.stdout or "").splitlines()]


def ensure_distribution(hostname, exec_prefix, dist):
    """
    Idempotently create one autoinstall tree (the "Kickstart Distribution" in the Web UI) with distribution_create, from one
    entry in <prefix>_distributions: {"name", "path", "base_channel", "install_type"}. `path` must be a directory on the server
    that already holds an extracted installer tree, because distribution_create checks for the initrd and fails if the tree is
    missing. This function does not create or upload that tree: mirror the ISO's contents to the path first, for example with a
    loop mount. install_type is one of the labels that `distribution_create --help` lists on the server. That list changes
    between releases.

    A failed creation is reported as a warning, and the function then returns normally. A missing tree is an ordinary state
    while media is staged outside this project. Dying here would stop the unrelated steps that follow in the caller. A kickstart
    profile that refers to a distribution that failed is skipped with its own warning (see ensure_kickstart_profile()).
    """
    name = dist.get("name")
    if not name:
        die("distributions: an entry is missing required 'name'")
    if distribution_exists(hostname, exec_prefix, name):
        print("  Distribution '{}' already exists — leaving it alone".format(name))
        return
    path = dist.get("path")
    base_channel = dist.get("base_channel")
    install_type = dist.get("install_type")
    if not (path and base_channel and install_type):
        die("distribution '{}': path, base_channel and install_type are all required to "
            "create it".format(name))
    r = _spacecmd(hostname, exec_prefix, "distribution_create -n {} -p {} -b {} -t {}".format(
        shlex.quote(name), shlex.quote(path), shlex.quote(base_channel), shlex.quote(install_type)))
    if r.returncode != 0:
        warn("could not create distribution '{}' — its own install tree may not be populated "
             "at '{}' yet (a real product ISO extracted there): {}".format(
                 name, path, (r.stderr or r.stdout or "").strip()))
        return
    print("  Created distribution '{}'".format(name))


def ensure_distributions(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_distributions — see ensure_distribution()'s own
    docstring. No-op if the field is unset or empty."""
    for dist in cfg.get("{}_distributions".format(prefix)) or []:
        ensure_distribution(hostname, exec_prefix, dist)


def kickstart_exists(hostname, exec_prefix, name):
    """
    Return True if `name` is an exact line in the output of `spacecmd kickstart_list`, which prints one label per line with no
    header.
    """
    r = _spacecmd(hostname, exec_prefix, "kickstart_list")
    return name in [line.strip() for line in (r.stdout or "").splitlines()]


def kickstart_variables(hostname, exec_prefix, name):
    """
    Return the current custom variables of a kickstart profile as {key: value}, from kickstart_listvariables. Each line is
    "key = value". A profile always has at least "org = <id>", even with no variables of its own set.
    """
    r = _spacecmd(hostname, exec_prefix, "kickstart_listvariables {}".format(shlex.quote(name)))
    result = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def ensure_kickstart_profile(hostname, exec_prefix, ks):
    """
    Idempotently create the kickstart profile described by one entry of <prefix>_kickstart_profiles: {"name",
    "distribution", "root_password", "virt_type": "none" (default), "para_host", "qemu", "xenfv" or "xenpv", "variables":
    {"key": "value", ...}, "activation_keys": [...] and "child_channels": [...]}.

    `distribution` is a name reference into <prefix>_distributions, which must be defined there. kickstart_create requires the
    distribution to exist. root_password is used only at creation. The server stores a hash, so a repeat run cannot detect or
    change the password. Delete and recreate the profile to change it. variables and activation_keys are applied on every run
    with kickstart_addvariable and kickstart_addactivationkeys, after diffing against the current lists, so an existing entry is
    never added twice.
    """
    name = ks.get("name")
    if not name:
        die("kickstart_profiles: an entry is missing required 'name'")

    if kickstart_exists(hostname, exec_prefix, name):
        print("  Kickstart profile '{}' already exists — leaving it alone".format(name))
    else:
        distribution = ks.get("distribution")
        root_password = ks.get("root_password")
        if not (distribution and root_password):
            die("kickstart profile '{}': distribution and root_password are both required to "
                "create it".format(name))
        # The referenced distribution may legitimately not exist yet — its own
        # ensure_distribution() call warns (not dies) when the install tree isn't
        # populated, see that function's own docstring. Check first and skip
        # cleanly with the same reasoning, rather than letting kickstart_create's
        # own less-clear error stand in for it.
        if not distribution_exists(hostname, exec_prefix, distribution):
            warn("kickstart profile '{}': distribution '{}' doesn't exist yet (see "
                 "ensure_distribution()'s own warning above, if any) — skipping".format(
                     name, distribution))
            return
        virt_type = ks.get("virt_type") or "none"
        r = _spacecmd(hostname, exec_prefix, "kickstart_create -n {} -d {} -p {} -v {}".format(
            shlex.quote(name), shlex.quote(distribution), shlex.quote(root_password),
            shlex.quote(virt_type)))
        if r.returncode != 0:
            warn("could not create kickstart profile '{}': {}".format(
                name, (r.stderr or r.stdout or "").strip()))
            return
        print("  Created kickstart profile '{}'".format(name))

    existing_vars = kickstart_variables(hostname, exec_prefix, name)
    for key, value in (ks.get("variables") or {}).items():
        if existing_vars.get(key) == str(value):
            continue
        r = _spacecmd(hostname, exec_prefix, "kickstart_addvariable {} {} {}".format(
            shlex.quote(name), shlex.quote(key), shlex.quote(str(value))))
        if r.returncode != 0:
            warn("could not set variable '{}' on kickstart profile '{}': {}".format(
                key, name, (r.stderr or r.stdout or "").strip()))

    existing_keys = set(line.strip() for line in (_spacecmd(
        hostname, exec_prefix, "kickstart_listactivationkeys {}".format(shlex.quote(name))
    ).stdout or "").splitlines() if line.strip())
    missing_keys = [k for k in (ks.get("activation_keys") or []) if k not in existing_keys]
    if missing_keys:
        r = _spacecmd(hostname, exec_prefix, "kickstart_addactivationkeys {} {}".format(
            shlex.quote(name), " ".join(shlex.quote(k) for k in missing_keys)))
        if r.returncode != 0:
            warn("could not link activation key(s) ({}) to kickstart profile '{}': {}".format(
                ", ".join(missing_keys), name, (r.stderr or r.stdout or "").strip()))

    existing_channels = set(line.strip() for line in (_spacecmd(
        hostname, exec_prefix, "kickstart_listchildchannels {}".format(shlex.quote(name))
    ).stdout or "").splitlines() if line.strip())
    missing_channels = [c for c in (ks.get("child_channels") or []) if c not in existing_channels]
    if missing_channels:
        r = _spacecmd(hostname, exec_prefix, "kickstart_addchildchannels {} {}".format(
            shlex.quote(name), " ".join(shlex.quote(c) for c in missing_channels)))
        if r.returncode != 0:
            warn("could not link child channel(s) ({}) to kickstart profile '{}': {}".format(
                ", ".join(missing_channels), name, (r.stderr or r.stdout or "").strip()))


def ensure_kickstart_profiles(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_kickstart_profiles — see
    ensure_kickstart_profile()'s own docstring. No-op if the field is
    unset or empty. Must run AFTER ensure_distributions() (a profile
    references a distribution by name) — same real ordering hazard as
    ensure_system_groups() vs ensure_activation_key(), see that function's
    own docstring."""
    for ks in cfg.get("{}_kickstart_profiles".format(prefix)) or []:
        ensure_kickstart_profile(hostname, exec_prefix, ks)


def snippet_file_path(hostname, exec_prefix, name):
    """
    Return the absolute path where Uyuni stores a Kickstart Snippet's content, parsed from the "File:" line of
    snippet_details. The path depends on the organization ID, for example /var/lib/cobbler/snippets/spacewalk/1/<name> for the
    default organization, so it cannot be computed. Returns None if the snippet does not exist. snippet_details then prints a
    warning and exits non-zero.
    """
    r = _spacecmd(hostname, exec_prefix, "snippet_details {}".format(shlex.quote(name)))
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("File:"):
            return line[len("File:"):].strip()
    return None


def ensure_snippet(hostname, exec_prefix, name, content):
    """
    Idempotently create or update a Kickstart Snippet, a reusable named text fragment. A kickstart or AutoYaST profile
    includes it through the $SNIPPET('spacewalk/<org>/<name>') macro, which is the "Macro:" line in snippet_details.

    snippet_create is interactive. It prints the content and asks for confirmation, and it has no -y option. The content is
    staged to a remote temporary file, because the -f option reads a local path, and "y" is fed on stdin to confirm. Running
    snippet_create on an existing name overwrites its content. spacecmd has no separate update command.

    The content currently at snippet_file_path() is compared with `content`, and the create step is skipped when they match.
    """
    existing_path = snippet_file_path(hostname, exec_prefix, name)
    if existing_path:
        current = _run(hostname, exec_prefix, "cat {}".format(shlex.quote(existing_path)),
                        check=False, capture=True)
        if current.returncode == 0 and (current.stdout or "") == content:
            print("  Snippet '{}' already up to date — leaving it alone".format(name))
            return

    remote_tmp = "/tmp/.lab-snippet-{}".format(hashlib.sha1(name.encode()).hexdigest()[:12])
    if not _stage_remote_file(hostname, exec_prefix, remote_tmp, content):
        die("could not stage snippet content for '{}'".format(name))

    r = _run(hostname, exec_prefix,
              "spacecmd -- snippet_create -n {} -f {}".format(
                  shlex.quote(name), shlex.quote(remote_tmp)),
              input_text="y\n", check=False, capture=True)
    _run(hostname, exec_prefix, "rm -f {}".format(shlex.quote(remote_tmp)), check=False)
    if r.returncode != 0:
        die("could not create snippet '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  {} snippet '{}'".format("Updated" if existing_path else "Created", name))


def ensure_snippets(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_snippets: a list of {name, content} dicts. See
    ensure_snippet()'s own docstring. No-op if the field is unset or
    empty. Runs BEFORE distributions/kickstart profiles — a profile's own
    %pre/%post scripts or partitioning can reference a snippet via its real
    $SNIPPET(...) macro, so it should already exist by the time a profile
    referencing it gets created."""
    for entry in cfg.get("{}_snippets".format(prefix)) or []:
        name = entry.get("name")
        if not name:
            die("{}_snippets: an entry is missing required 'name'".format(prefix))
        content = entry.get("content")
        if content is None:
            die("snippet '{}': missing required 'content'".format(name))
        ensure_snippet(hostname, exec_prefix, name, content)


# ── Image management (Images -> Stores/Profiles/Build/Import) ──────────────
# These calls go through the raw api passthrough. spacecmd has no native subcommand for them. They use three
# handler namespaces: image.store.* (ImageStoreHandler), image.profile.* (ImageProfileHandler), and the
# unprefixed image.* (ImageInfoHandler, for importContainerImage and scheduleImageBuild). image.store.create and
# image.profile.create return a one-element list holding the numeric id.

def image_store_exists(hostname, exec_prefix, label):
    """
    Return True if `label` is among the stores that image.store.listImageStores returns. A server always has at least one
    store, "SUSE Manager OS Image Store", which is created by default.
    """
    r = _api_call(hostname, exec_prefix, "image.store.listImageStores", [])
    try:
        stores = json.loads(r.stdout or "[]")
    except (ValueError, TypeError):
        stores = []
    return label in [s.get("label") for s in stores]


def ensure_image_store(hostname, exec_prefix, store):
    """
    Idempotently create one image store (Images, then Stores, in the Web UI) with image.store.create, from one entry of
    <prefix>_image_stores: {"label", "uri", "type": "registry" or "os_image", "username", "password"}. type must be a label
    that image.store.listImageStoreTypes returns on the server, and that list can change between releases. username and
    password are optional. Omit them for a public registry, such as registry.suse.com.
    """
    label = store.get("label")
    if not label:
        die("image_stores: an entry is missing required 'label'")
    if image_store_exists(hostname, exec_prefix, label):
        print("  Image store '{}' already exists — leaving it alone".format(label))
        return
    uri = store.get("uri")
    store_type = store.get("type")
    if not (uri and store_type):
        die("image store '{}': uri and type are both required to create it".format(label))
    credentials = {}
    if store.get("username"):
        credentials["username"] = store["username"]
        credentials["password"] = store.get("password") or ""
    r = _api_call(hostname, exec_prefix, "image.store.create", [label, uri, store_type, credentials])
    if r.returncode != 0:
        die("could not create image store '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created image store '{}' ({})".format(label, uri))


def ensure_image_stores(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_image_stores — see ensure_image_store()'s own
    docstring. No-op if the field is unset or empty."""
    for store in cfg.get("{}_image_stores".format(prefix)) or []:
        ensure_image_store(hostname, exec_prefix, store)


def image_profile_exists(hostname, exec_prefix, label):
    """Whether `label` appears among image.profile.listImageProfiles' real
    JSON output."""
    r = _api_call(hostname, exec_prefix, "image.profile.listImageProfiles", [])
    try:
        profiles = json.loads(r.stdout or "[]")
    except (ValueError, TypeError):
        profiles = []
    return label in [p.get("label") for p in profiles]


def ensure_image_profile(hostname, exec_prefix, profile):
    """
    Idempotently create one image profile (build instructions: Images, then Profiles in the Web UI) with
    image.profile.create, from one entry of <prefix>_image_profiles: {"label", "type": "dockerfile" or "kiwi", "store", "path",
    "activation_key"}.

    store is a name reference into <prefix>_image_stores, which must be defined there. The server does not check it at
    creation, but a build against a missing store fails, so the field is required here. path is the Dockerfile or Kiwi source.
    A git-hosted Dockerfile uses the form "https://github.com/USER/project.git#branch:folder". A Kiwi source can be a local
    path or a similar git location. activation_key sets which software channels the build or import can use. Both container
    and OS image profiles require it.
    """
    label = profile.get("label")
    if not label:
        die("image_profiles: an entry is missing required 'label'")
    if image_profile_exists(hostname, exec_prefix, label):
        print("  Image profile '{}' already exists — leaving it alone".format(label))
        return
    image_type = profile.get("type")
    store = profile.get("store")
    path = profile.get("path")
    activation_key = profile.get("activation_key") or ""
    if not (image_type and store and path):
        die("image profile '{}': type, store and path are all required to create it".format(label))
    r = _api_call(hostname, exec_prefix, "image.profile.create",
                  [label, image_type, store, path, activation_key])
    if r.returncode != 0:
        die("could not create image profile '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created image profile '{}'".format(label))


def ensure_image_profiles(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_image_profiles — see ensure_image_profile()'s
    own docstring. No-op if the field is unset or empty. Must run AFTER
    ensure_image_stores() (a profile references a store by label)."""
    for profile in cfg.get("{}_image_profiles".format(prefix)) or []:
        ensure_image_profile(hostname, exec_prefix, profile)


def import_container_image(hostname, exec_prefix, name, version, build_host_id, store_label,
                            activation_key=""):
    """
    Schedule a container image import and inspection with image.importContainerImage. Each call schedules a new action,
    so the call is not idempotent. For that reason the automatic install flow does not run it, like the other explicit actions
    in this module.

    build_host_id is the numeric ID of a registered system that has the "Container Build Host" entitlement enabled. This
    module does not enable that entitlement. Do it in the Web UI or with spacecmd system_addentitlement. Find the ID with
    'spacecmd system_list'. The function returns the numeric ID of the scheduled action, and dies with the server's error
    otherwise, for example when the build host lacks the entitlement.

    The sixth positional argument is earliestOccurrence, a date. Passing None fails, because XML-RPC cannot send null. The
    call therefore sends "now" in UTC, as ensure_image_build() does.
    """
    earliest = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    r = _api_call(hostname, exec_prefix, "image.importContainerImage",
                  [name, version or "", build_host_id, store_label, activation_key, earliest])
    if r.returncode != 0:
        die("could not schedule import of image '{}:{}': {}".format(
            name, version or "latest", (r.stderr or r.stdout or "").strip()))
    print("  Scheduled import of image '{}:{}' from store '{}'".format(
        name, version or "latest", store_label))
    return r.stdout


def import_images(hostname, exec_prefix, cfg, prefix):
    """
    Runs every entry in <prefix>_image_imports through
    import_container_image() — see its own docstring for why this is a
    SEPARATE, explicit trigger (install_smlm.py's own
    --import-images/--run-recurring-schedules-style flag), never part of
    the automatic install flow: entry shape is {"name", "version",
    "build_host_id", "store", "activation_key"}.
    """
    imports = cfg.get("{}_image_imports".format(prefix)) or []
    if not imports:
        print("No {}_image_imports configured — nothing to import".format(prefix))
        return
    for entry in imports:
        name = entry.get("name")
        build_host_id = entry.get("build_host_id")
        store = entry.get("store")
        if not (name and build_host_id and store):
            die("{}_image_imports: an entry needs 'name', 'build_host_id' and 'store'".format(prefix))
        import_container_image(hostname, exec_prefix, name, entry.get("version"), build_host_id,
                                store, entry.get("activation_key") or "")


def monitoring_status(hostname, exec_prefix):
    """
    Return the server's bundled exporter status as a dict from admin.monitoring.getStatus. The keys are node, tomcat,
    postgres, taskomatic and self_monitoring, and each value is "enabled" or "disabled". A new server reports every key as
    "disabled". Takes no arguments.
    """
    r = _api_call(hostname, exec_prefix, "admin.monitoring.getStatus", [])
    try:
        result = json.loads(r.stdout or "[]")
        return result[0] if result else {}
    except (ValueError, TypeError, IndexError):
        return {}


def ensure_monitoring(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently enable the server's bundled Prometheus exporters (node, tomcat, postgres, taskomatic and
    self_monitoring) with admin.monitoring.enable. The call is gated by <prefix>_monitoring_enabled, a truthy flag. enable
    takes no arguments. It switches on exporters that the image already includes. Uyuni's monitoring is pull-based: an
    external Prometheus scrapes this server's exporter ports, and nothing is pushed to Prometheus. The function does nothing if
    the flag is false or unset, or if monitoring_status() already reports node as enabled.

    After a fresh enable, Tomcat and Taskomatic must be restarted before the exporters listen. The restart happens only on the
    change from disabled to enabled, so an already-enabled server is not disturbed. It runs systemctl through exec_prefix,
    because spacecmd has no restart command.

    Exporter ports to open on the server's firewall or security group for a remote Prometheus: node 9100, postgres 9187,
    Tomcat JMX 5556, Taskomatic JMX 5557 and Taskomatic direct 9800. The message-queue job uses "<server>:80/rhn/metrics",
    which is the existing web port.
    """
    if not (cfg.get("{}_monitoring_enabled".format(prefix)) in ("true", True)):
        return
    status = monitoring_status(hostname, exec_prefix)
    if status.get("node") == "enabled":
        print("  Server monitoring already enabled — leaving it alone")
        return
    r = _api_call(hostname, exec_prefix, "admin.monitoring.enable", [])
    if r.returncode != 0:
        die("could not enable server monitoring: {}".format((r.stderr or r.stdout or "").strip()))
    print("  Enabled server monitoring (node/tomcat/postgres/taskomatic exporters)")
    r = _run(hostname, exec_prefix, "systemctl restart tomcat taskomatic")
    if r.returncode != 0:
        warn("monitoring was enabled, but restarting tomcat/taskomatic to actually start the "
             "exporter listeners failed — restart them manually: {}".format(
                 (r.stderr or r.stdout or "").strip()))
    else:
        print("  Restarted tomcat/taskomatic so the exporters actually start listening")


def org_exists(hostname, exec_prefix, org_name):
    """
    Whether `org_name` already appears as an exact line in `spacecmd
    org_list`'s output (one org name per line, no header, per source). Exact
    match rather than the substring check used elsewhere in this module
    (activation_key_exists, config_channel_exists), since org names are
    typically short and more collision-prone (e.g. "Lab" vs "Lab2").
    """
    r = _spacecmd(hostname, exec_prefix, "org_list")
    return org_name in [line.strip() for line in (r.stdout or "").splitlines()]


def ensure_org(hostname, exec_prefix, org):
    """
    Idempotently create the organization described by one entry of <prefix>_orgs. Call it from the default
    administrator's session, which ensure_spacecmd_config() sets up. Organization creation is not scoped to an organization:
    org.create runs under whichever session is logged in.
    """
    name = org.get("name")
    if not name:
        die("orgs: an entry is missing required 'name'")
    if org_exists(hostname, exec_prefix, name):
        print("  Org '{}' already exists — leaving it alone".format(name))
        return

    admin_user = org.get("admin_user")
    admin_pass = org.get("admin_pass")
    admin_email = org.get("admin_email")
    if not (admin_user and admin_pass and admin_email):
        die("org '{}': admin_user, admin_pass and admin_email are all required to create it".format(name))
    first = org.get("admin_first_name") or admin_user
    last = org.get("admin_last_name") or name

    cmd = "org_create -n {n} -u {u} -f {f} -l {l} -e {e} -p {p}".format(
        n=shlex.quote(name), u=shlex.quote(admin_user), f=shlex.quote(first),
        l=shlex.quote(last), e=shlex.quote(admin_email), p=shlex.quote(admin_pass))
    if org.get("prefix"):
        cmd += " -P {}".format(shlex.quote(org["prefix"]))
    if org.get("pam"):
        cmd += " --pam"

    r = _spacecmd(hostname, exec_prefix, cmd)
    if r.returncode != 0:
        die("could not create org '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created org '{}' (admin: {})".format(name, admin_user))


def ensure_org_trust(hostname, exec_prefix, org_a, org_b):
    """
    Idempotently establish trust between org_a and org_b with org_addtrust. The call is skipped if org_listtrusts already
    lists org_b for org_a. Trust alone does not make a channel visible to another organization. The owning organization must
    also share the channel, through ensure_channel_sharing(). Call it from a session that has rights in both organizations,
    normally the default administrator's.
    """
    existing = _spacecmd(hostname, exec_prefix, "org_listtrusts {}".format(shlex.quote(org_a))).stdout or ""
    if org_b in existing:
        print("  Trust {} <-> {} already exists — leaving it alone".format(org_a, org_b))
        return
    r = _spacecmd(hostname, exec_prefix, "org_addtrust {} {}".format(shlex.quote(org_a), shlex.quote(org_b)))
    if r.returncode != 0:
        die("could not add trust between '{}' and '{}': {}".format(org_a, org_b, (r.stderr or r.stdout or "").strip()))
    print("  Trust established: {} <-> {}".format(org_a, org_b))


def ensure_channel_sharing(hostname, exec_prefix, channel_label, access="protected"):
    """
    Set a software channel's sharing level with channel.access.setOrgSharing, which has no spacecmd subcommand. access is
    "public", "private" or "protected". "protected" means visible to trusted organizations only. The current level is read with
    channel.access.getOrgSharing, and the check is a substring match on its output, because that output's format is not
    documented. Call it from a session that belongs to the channel's owning organization.
    """
    if access not in ("public", "private", "protected"):
        die("invalid channel access level '{}': expected public, private, or protected".format(access))

    current = _api_call(hostname, exec_prefix, "channel.access.getOrgSharing", [channel_label])
    if current.returncode == 0 and access in (current.stdout or ""):
        print("  Channel '{}' sharing already '{}' — leaving it alone".format(channel_label, access))
        return

    r = _api_call(hostname, exec_prefix, "channel.access.setOrgSharing", [channel_label, access])
    if r.returncode != 0:
        die("could not set channel '{}' sharing to '{}': {}".format(
            channel_label, access, (r.stderr or r.stdout or "").strip()))
    print("  Channel '{}' sharing set to '{}'".format(channel_label, access))


def ensure_orgs(hostname, exec_prefix, cfg, prefix, default_admin_user, default_admin_pass):
    """
    Orchestrate <prefix>_orgs, a list of organization dicts. Each dict has name, admin_user, admin_pass, admin_email,
    admin_first_name, admin_last_name, prefix, pam, trust_with, share_channels and share_channels_access. A dict can also carry
    the <prefix>_activation_key*, <prefix>_config_channels, <prefix>_access_groups, <prefix>_system_groups and <prefix>_users
    keys that the organization needs, using the same field names as the top-level configuration. Once the session is the
    organization's own administrator, the existing ensure_* functions apply to it unchanged.

    For each organization, in list order (so a later organization can trust an earlier one):
      1. Authenticate as the default administrator, and create the organization if it does not exist.
      2. Establish each trust_with relationship, still as the default administrator.
      3. Authenticate as the organization's own administrator, and provision its share_channels, config channels, activation
         keys, AppStreams and access groups. These are scoped to that organization automatically.

    The default administrator's session is restored before the function returns, so later steps do not run as the last
    organization. An entry without admin_user and admin_pass skips step 3. Use that for an organization that already exists and
    needs only its trusts and shared channels applied. The function does nothing if <prefix>_orgs is unset or empty.
    """
    orgs = cfg.get("{}_orgs".format(prefix)) or []
    if not orgs:
        return

    for org in orgs:
        name = org.get("name")
        if not name:
            die("{}_orgs: an entry is missing required 'name'".format(prefix))

        ensure_spacecmd_config(hostname, exec_prefix, default_admin_user, default_admin_pass)
        ensure_org(hostname, exec_prefix, org)

        for other in org.get("trust_with") or []:
            ensure_org_trust(hostname, exec_prefix, name, other)

        admin_user = org.get("admin_user")
        admin_pass = org.get("admin_pass")
        if not (admin_user and admin_pass):
            continue
        ensure_spacecmd_config(hostname, exec_prefix, admin_user, admin_pass)

        share_access = org.get("share_channels_access") or "protected"
        for ch in org.get("share_channels") or []:
            ensure_channel_sharing(hostname, exec_prefix, ch, share_access)

        ensure_config_channels(hostname, exec_prefix, org, prefix)
        # System groups BEFORE any activation key — same reorder, same
        # reason, as the top-level orchestration in install_smlm.py/
        # install_uyuni.py: ensure_activation_key()'s own group-linking dies
        # if the named group doesn't exist yet server-side.
        ensure_system_groups(hostname, exec_prefix, org, prefix)
        ensure_activation_key(hostname, exec_prefix, org, prefix)
        ensure_appstreams(hostname, exec_prefix, org, prefix)
        ensure_activation_key_packages(hostname, exec_prefix, org, prefix)
        ensure_users(hostname, exec_prefix, org, prefix)
        ensure_access_groups(hostname, exec_prefix, org, prefix)

    ensure_spacecmd_config(hostname, exec_prefix, default_admin_user, default_admin_pass)


def access_group_exists(hostname, exec_prefix, label):
    """
    Return True if `label` appears in the output of access.listRoles. The access namespace has no spacecmd subcommand, so the
    call goes through the generic 'api' passthrough. The check is a substring match, because the printed format of the
    AccessGroup list is not documented.
    """
    r = _api_call(hostname, exec_prefix, "access.listRoles", [])
    return label in (r.stdout or "")


def ensure_access_group(hostname, exec_prefix, label, description, permissions_from=None):
    """
    Idempotently create a custom RBAC access group ("User Access Group") with access.createRole. permissions_from, an optional
    list of existing role labels, supplies the permissions to copy, as createRole's third argument does.
    """
    if access_group_exists(hostname, exec_prefix, label):
        print("  Access group '{}' already exists — leaving it alone".format(label))
        return
    args = [label, description]
    if permissions_from:
        args.append(list(permissions_from))
    r = _api_call(hostname, exec_prefix, "access.createRole", args)
    if r.returncode != 0:
        die("could not create access group '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created access group '{}'".format(label))


def access_group_has_namespace(hostname, exec_prefix, label, namespace):
    """Whether `namespace` already appears in access.listPermissions(label)'s
    raw output — same substring-match heuristic as access_group_exists."""
    r = _api_call(hostname, exec_prefix, "access.listPermissions", [label])
    return r.returncode == 0 and namespace in (r.stdout or "")


def ensure_access_group_permissions(hostname, exec_prefix, label, permissions):
    """
    Grant each namespace in `permissions` that the group does not already have. `permissions` is a list of
    {"namespace": ..., "mode": "R" or "W"}, with mode optional. The missing namespaces are granted with one access.grantAccess
    call. access_group_has_namespace() is checked first, because repeating grantAccess for an already-granted namespace is not
    documented as safe. The function does nothing if every namespace is already granted or the list is empty.
    """
    to_grant = []
    modes = []
    have_modes = False
    for p in permissions or []:
        namespace = p.get("namespace")
        if not namespace:
            die("access group '{}': a permission entry is missing required 'namespace'".format(label))
        if access_group_has_namespace(hostname, exec_prefix, label, namespace):
            print("  Access group '{}' already has namespace '{}' — leaving it alone".format(label, namespace))
            continue
        to_grant.append(namespace)
        mode = p.get("mode")
        if mode:
            have_modes = True
        modes.append(mode or "R")

    if not to_grant:
        return
    args = [label, to_grant]
    if have_modes:
        args.append(modes)
    r = _api_call(hostname, exec_prefix, "access.grantAccess", args)
    if r.returncode != 0:
        die("could not grant namespace(s) {} to access group '{}': {}".format(
            to_grant, label, (r.stderr or r.stdout or "").strip()))
    print("  Granted {} namespace(s) to access group '{}'".format(len(to_grant), label))


def user_has_role(hostname, exec_prefix, username, role):
    """Whether `role` appears in 'spacecmd user_details USERNAME''s raw
    output (wraps user.listRoles server-side, per source)."""
    r = _spacecmd(hostname, exec_prefix, "user_details {}".format(shlex.quote(username)))
    return r.returncode == 0 and role in (r.stdout or "")


def ensure_user_role(hostname, exec_prefix, username, role):
    """
    Attach `role` to a user that already exists, with user_addrole. This function does not create the user. A missing
    username fails the same way any other user_addrole error does.

    user_addrole accepts only the fixed built-in roles: activation_key_admin, channel_admin, config_admin, image_admin, org_admin,
    regular_user, satellite_admin and system_group_admin. A custom access group's label is rejected with "Role with the label
    [X] cannot be assigned/revoked from the user", even for the default satellite_admin session. No XML-RPC method was found
    that attaches a user to a custom access group, so ensure_access_groups() cannot attach users to one. It warns instead of
    failing, so the built-in roles in the same configuration are still applied.
    """
    if user_has_role(hostname, exec_prefix, username, role):
        print("  User '{}' already has role '{}' — leaving it alone".format(username, role))
        return
    r = _spacecmd(hostname, exec_prefix, "user_addrole {} {}".format(shlex.quote(username), shlex.quote(role)))
    if r.returncode != 0:
        warn("could not add role '{}' to user '{}' — if '{}' is a custom access group's own "
             "label, this is a known, currently-unresolved API gap (see this function's own "
             "docstring), not a config mistake: {}".format(
                 role, username, role, (r.stderr or r.stdout or "").strip()))
        return
    print("  Added role '{}' to user '{}'".format(role, username))


def user_exists(hostname, exec_prefix, username):
    """
    Return True if `username` is an exact line in the output of `spacecmd user_list`, which prints one name per line with no
    header. The match is exact, as in org_exists.
    """
    r = _spacecmd(hostname, exec_prefix, "user_list")
    return username in [line.strip() for line in (r.stdout or "").splitlines()]


def ensure_user(hostname, exec_prefix, user):
    """
    Idempotently create the user account described by one entry of <prefix>_users, with user_create. Built-in roles (the
    fixed labels that `spacecmd user_listavailableroles` lists) are applied through `roles`, using ensure_user_role(). A repeat
    run does not grant a role the user already has.

    A custom access group's label becomes a role label once the group exists. ensure_users() runs before
    ensure_access_groups() at every call site, so a custom label in `roles` would fail, because that role does not exist yet.
    Put a user in a custom group's own users list instead, which runs in the right order. Use `roles` only for the built-in
    labels, which have no ordering dependency.
    """
    username = user.get("username")
    if not username:
        die("users: an entry is missing required 'username'")

    if user_exists(hostname, exec_prefix, username):
        print("  User '{}' already exists — leaving it alone".format(username))
    else:
        password = user.get("password")
        first_name = user.get("first_name")
        last_name = user.get("last_name")
        email = user.get("email")
        if not (password and first_name and last_name and email):
            die("user '{}': password, first_name, last_name and email are all required to "
                "create it".format(username))

        cmd = "user_create -u {u} -p {p} -f {f} -l {l} -e {e}".format(
            u=shlex.quote(username), p=shlex.quote(password),
            f=shlex.quote(first_name), l=shlex.quote(last_name), e=shlex.quote(email))
        if user.get("pam"):
            cmd += " --pam"
        r = _spacecmd(hostname, exec_prefix, cmd)
        if r.returncode != 0:
            die("could not create user '{}': {}".format(username, (r.stderr or r.stdout or "").strip()))
        print("  Created user '{}'".format(username))

    for role in user.get("roles") or []:
        ensure_user_role(hostname, exec_prefix, username, role)


def ensure_users(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_users: a list of {username, password, first_name,
    last_name, email, pam: false, roles: [...]} dicts — see ensure_user()
    for per-entry behavior. No-op if <prefix>_users is unset or empty.
    Called both at the top level (default-org users) and per-org from
    ensure_orgs (org-scoped users), same pattern as ensure_access_groups —
    and deliberately BEFORE ensure_access_groups() at each of those call
    sites, since an access group's own `users` list needs the account to
    already exist.
    """
    for user in cfg.get("{}_users".format(prefix)) or []:
        ensure_user(hostname, exec_prefix, user)


def ensure_access_groups(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_access_groups, a list of {label, description, permissions_from, permissions: [{namespace, mode}],
    users} dicts. Each entry creates the group if it is missing, grants its namespaces, and then tries to attach it to each
    existing username in `users` through ensure_user_role(). The function does nothing if the field is unset or empty. It runs
    at the top level, for default-organization users, and per organization from ensure_orgs().

    Creating groups and granting permissions work. The users attachment does not work, because the server rejects custom labels
    in user_addrole (see ensure_user_role()). That call warns, so the rest of the configuration still runs.
    """
    groups = cfg.get("{}_access_groups".format(prefix)) or []
    for group in groups:
        label = group.get("label")
        if not label:
            die("{}_access_groups: an entry is missing required 'label'".format(prefix))
        description = group.get("description") or label

        ensure_access_group(hostname, exec_prefix, label, description, group.get("permissions_from"))
        ensure_access_group_permissions(hostname, exec_prefix, label, group.get("permissions"))

        for username in group.get("users") or []:
            ensure_user_role(hostname, exec_prefix, username, label)


def ensure_ansible_control_node(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_ansible_control_nodes, a list of {system} dicts. For each system, it enables the "Ansible
    Control Node" add-on entitlement with system.addEntitlements, using the label "ansible_control_node". Then it schedules a
    highstate apply with system.scheduleApplyHighstate, which installs the ansible package on the system.

    The call is idempotent and safe on every configuration pass. addEntitlements ignores an entitlement the system already has,
    and a repeated highstate is idempotent on the Salt side. The function does nothing if the field is unset or empty. The
    numeric system ID comes from _system_id(), as in ensure_grafana_formula().

    This function only enables the entitlement and triggers the package install. It does not create the playbook or inventory
    files, which live on the control node and are managed outside this module. It also does not set up SSH keys from the
    control node to managed targets. install_ansible_control_node.py does both, over SSH to the control node.
    """
    entries = cfg.get("{}_ansible_control_nodes".format(prefix)) or []
    for entry in entries:
        system = entry.get("system")
        if not system:
            die("{}_ansible_control_nodes: an entry is missing required 'system'".format(prefix))

        sid = _system_id(hostname, exec_prefix, system)

        r = _api_call(hostname, exec_prefix, "system.addEntitlements", [sid, ["ansible_control_node"]])
        if r.returncode != 0:
            die("could not enable the Ansible Control Node entitlement on '{}': {}".format(
                system, (r.stderr or r.stdout or "").strip()))

        earliest = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        r = _api_call(hostname, exec_prefix, "system.scheduleApplyHighstate", [[sid], earliest, False])
        if r.returncode != 0:
            die("could not schedule a highstate apply on '{}' to install ansible: {}".format(
                system, (r.stderr or r.stdout or "").strip()))
        print("  Enabled the Ansible Control Node entitlement on '{}' (sid {}) and scheduled a "
              "highstate apply to install ansible".format(system, sid))


def ensure_container_build_hosts(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_image_build_hosts, a list of {system} dicts. For each system, it enables the "Container Build Host"
    add-on entitlement with system.addEntitlements, using the label "container_build_host". This entitlement is separate from
    "osimage_build_host", which belongs to the Kiwi-based OS-image build path. Then it schedules a highstate apply with
    system.scheduleApplyHighstate, which installs the container build tooling on the system. The two steps match
    ensure_ansible_control_node().

    An image import's build_host_id must name a system that has this entitlement. This function grants it, so a single list entry
    gives a working build host, with no manual step.

    The call is idempotent and safe on every configuration pass, for the same reasons as ensure_ansible_control_node(). The
    function does nothing if the field is unset or empty. It does not check that the system's software channels include the
    Containers module. That prerequisite is expected to come from the system's own activation key and channel setup, as for the
    other entitlement functions in this module.
    """
    entries = cfg.get("{}_image_build_hosts".format(prefix)) or []
    for entry in entries:
        system = entry.get("system")
        if not system:
            die("{}_image_build_hosts: an entry is missing required 'system'".format(prefix))

        sid = _system_id(hostname, exec_prefix, system)

        r = _api_call(hostname, exec_prefix, "system.addEntitlements", [sid, ["container_build_host"]])
        if r.returncode != 0:
            die("could not enable the Container Build Host entitlement on '{}': {}".format(
                system, (r.stderr or r.stdout or "").strip()))

        earliest = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        r = _api_call(hostname, exec_prefix, "system.scheduleApplyHighstate", [[sid], earliest, False])
        if r.returncode != 0:
            die("could not schedule a highstate apply on '{}' to install container build tooling: {}".format(
                system, (r.stderr or r.stdout or "").strip()))
        print("  Enabled the Container Build Host entitlement on '{}' (sid {}) and scheduled a "
              "highstate apply to install container build tooling".format(system, sid))


def ensure_mcp_server(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_mcp_server, a single dict that deploys the Uyuni MCP (Model Context Protocol) server from
    github.com/uyuni-project/mcp-server-uyuni against this server. An MCP-compliant AI client, such as Claude Desktop or Gemini
    CLI, can then inspect and manage the server. The environment variable names and defaults match that project's README.

    It runs as a standalone podman container next to the uyuni-server container, not inside it. The server talks to Uyuni over the
    external HTTPS API, so it needs no access to exec_prefix. exec_prefix is accepted only so the call shape matches the other
    ensure_* steps.

    The container runs with --network=host. With podman's default bridge network, the copy of the host's /etc/hosts that the
    container inherits maps the server's FQDN to 127.0.0.1, which resolves to the container itself. Host networking makes the
    container behave like any other process on the host. For the same reason, UYUNI_MCP_HOST is bound to 127.0.0.1 directly, not
    with a -p port mapping, which host networking does not allow. The effect is the same: the server is not reachable from other
    hosts by default.

    Only <prefix>_deployment == "podman" is supported. A Kubernetes deployment would need a Deployment and Service manifest, which
    this function does not write. In that case it warns and does nothing.

    It binds to 127.0.0.1 only. The server holds Uyuni credentials and can enable state-changing calls through
    UYUNI_MCP_WRITE_TOOLS_ENABLED. In HTTP mode it runs without authentication unless UYUNI_AUTH_SERVER or OAuth is configured,
    and this function configures neither. Reach it from another host through an explicit SSH tunnel or port forward.

    Config keys under <prefix>_mcp_server. All are optional, and the dict's presence enables the feature:
      "version"              image tag, e.g. "v0.2.1". Default "latest".
      "port"                 port on 127.0.0.1 to listen on. Default 8090.
      "user" / "password"    Uyuni credentials for the server's API calls. Default to <prefix>_admin_user and <prefix>_admin_pass,
                             the account ensure_spacecmd_config() already uses. The upstream README recommends a dedicated
                             low-privilege account, which this function does not create.
      "write_tools_enabled"  bool, default False. Maps to UYUNI_MCP_WRITE_TOOLS_ENABLED. With False the server exposes only
                             read tools: inspect and list.
      "ssl_verify"           bool, default False. Maps to UYUNI_MCP_SSL_VERIFY. The server's certificate is self-signed.

    The function always rewrites the environment file and recreates the container, so a changed password, version or port takes
    effect. Credentials go into a root-only (0600) env file on the remote host, passed to podman with --env-file. They are never
    passed with -e or on the command line, where podman inspect and ps would show them.
    """
    field = "{}_mcp_server".format(prefix)
    if cfg.get(field) is None:
        # NOT `if not cfg.get(field): return` — {} is a legitimate,
        # explicit "enable with every default" value and must not be
        # treated the same as the field being absent entirely.
        return
    entry = cfg[field]

    deployment = cfg.get("{}_deployment".format(prefix)) or "kubernetes"
    if deployment != "podman":
        warn("{0}_mcp_server is set but {0}_deployment is '{1}' — the Uyuni MCP Server addon "
             "only supports a podman-deployed target for now (it needs direct SSH+podman access "
             "to a host with the server on it); skipping".format(prefix, deployment))
        return

    version = entry.get("version") or "latest"
    port = entry.get("port") or 8090
    user = entry.get("user") or cfg.get("{}_admin_user".format(prefix)) or "admin"
    password = entry.get("password") or cfg.get("{}_admin_pass".format(prefix)) or "Smlm12345"
    write_tools = bool(entry.get("write_tools_enabled"))
    ssl_verify = bool(entry.get("ssl_verify"))

    env_content = "\n".join([
        "UYUNI_SERVER=https://{}".format(hostname),
        "UYUNI_USER={}".format(user),
        "UYUNI_PASS={}".format(password),
        "UYUNI_MCP_SSL_VERIFY={}".format("true" if ssl_verify else "false"),
        "UYUNI_MCP_WRITE_TOOLS_ENABLED={}".format("true" if write_tools else "false"),
        "UYUNI_MCP_TRANSPORT=http",
        "UYUNI_MCP_HOST=127.0.0.1",
        "UYUNI_MCP_PORT={}".format(port),
        "UYUNI_MCP_PUBLIC_URL=http://127.0.0.1:{}".format(port),
    ]) + "\n"

    env_path = "/etc/mcp-server-uyuni/uyuni-config.env"
    r = ssh_run(hostname,
                "mkdir -p /etc/mcp-server-uyuni && cat > {ep} && chmod 600 {ep}".format(
                    ep=shlex.quote(env_path)),
                input_text=env_content, check=False)
    if r.returncode != 0:
        die("could not write the MCP server's env file on '{}': {}".format(
            hostname, (r.stderr or r.stdout or "").strip()))

    image = "ghcr.io/uyuni-project/mcp-server-uyuni:{}".format(version)
    r = ssh_run(hostname,
                "podman rm -f mcp-server-uyuni >/dev/null 2>&1; "
                "podman run -d --name mcp-server-uyuni --restart=always --network=host "
                "--env-file {env_path} {image}".format(
                    env_path=shlex.quote(env_path), image=shlex.quote(image)),
                check=False, capture=True)
    if r.returncode != 0:
        die("could not start the mcp-server-uyuni container on '{}': {}".format(
            hostname, (r.stderr or r.stdout or "").strip()))

    r = ssh_run(hostname,
                "podman ps --filter name=mcp-server-uyuni --filter status=running -q",
                check=False, capture=True)
    if not (r.stdout or "").strip():
        die("mcp-server-uyuni container on '{}' exited immediately after starting — "
            "check 'podman logs mcp-server-uyuni' on that host".format(hostname))

    print("  Deployed the Uyuni MCP Server ({}) on '{}', listening on 127.0.0.1:{} only "
          "(write tools {}) — reach it via an SSH tunnel".format(
              image, hostname, port, "ENABLED" if write_tools else "disabled, read-only"))


def ansible_path_exists(hostname, exec_prefix, control_node_id, path):
    """
    Whether `path` already appears in ansible.listAnsiblePaths(control_node_id)'s
    raw output — no dedicated existence check exists (same substring-match
    heuristic used throughout this module wherever the raw print format of
    a struct/list wasn't confirmed from docs).
    """
    r = _api_call(hostname, exec_prefix, "ansible.listAnsiblePaths", [control_node_id])
    return r.returncode == 0 and path in (r.stdout or "")


def ensure_ansible_path(hostname, exec_prefix, control_node_id, path_type, path):
    """
    Idempotently register `path` as an ansible.AnsiblePath of `path_type` ("inventory" or "playbook") for control-node
    system `control_node_id`. `path` is a directory on the control node's own filesystem. This function does not create or
    upload anything there. The control node must already be a registered system with the "Ansible Control Node" entitlement,
    which ensure_ansible_control_node() enables.
    """
    if path_type not in ("inventory", "playbook"):
        die("invalid ansible path type '{}': expected 'inventory' or 'playbook'".format(path_type))
    if ansible_path_exists(hostname, exec_prefix, control_node_id, path):
        print("  Ansible {} path '{}' already registered on control node {} — leaving it alone".format(
            path_type, path, control_node_id))
        return
    r = _api_call(hostname, exec_prefix, "ansible.createAnsiblePath",
                  [{"type": path_type, "server_id": control_node_id, "path": path}])
    if r.returncode != 0:
        die("could not register ansible {} path '{}' on control node {}: {}".format(
            path_type, path, control_node_id, (r.stderr or r.stdout or "").strip()))
    print("  Registered ansible {} path '{}' on control node {}".format(path_type, path, control_node_id))


def remove_ansible_path(hostname, exec_prefix, path_id):
    """
    Remove one Ansible path by its numeric id, with ansible.removeAnsiblePath(sessionKey, pathId). It returns 1 on success.
    The server raises an error if the path does not exist or is not accessible.
    """
    r = _api_call(hostname, exec_prefix, "ansible.removeAnsiblePath", [path_id])
    if r.returncode != 0:
        die("could not remove ansible path id {}: {}".format(path_id, (r.stderr or r.stdout or "").strip()))


# Enabling the Ansible Control Node entitlement makes the server create two default AnsiblePath entries. They point at
# locations that do not exist on the control nodes this project provisions, which use /srv/ansible/. The Schedule Playbook
# flow can select a default path instead of a registered one and then fail. ensure_ansible_paths() removes the defaults.
_STALE_DEFAULT_ANSIBLE_PATHS = {("/etc/ansible/hosts", "inventory"), ("/etc/ansible/playbooks", "playbook")}


def remove_stale_default_ansible_paths(hostname, exec_prefix, control_node_id):
    """
    Remove the default Ansible paths that the server creates automatically (see _STALE_DEFAULT_ANSIBLE_PATHS) from
    `control_node_id`, when they are present. The function is idempotent. It does nothing if neither default is registered, as on
    a repeat run or on a server version that does not create them.

    ansible.listAnsiblePaths returns valid JSON through spacecmd's api passthrough, so the output is parsed rather than searched
    as text.
    """
    r = _api_call(hostname, exec_prefix, "ansible.listAnsiblePaths", [control_node_id])
    if r.returncode != 0:
        die("could not list ansible paths on control node {}: {}".format(
            control_node_id, (r.stderr or r.stdout or "").strip()))
    try:
        existing = json.loads(r.stdout or "[]")
    except ValueError:
        return
    for entry in existing:
        key = (entry.get("path"), entry.get("type"))
        if key in _STALE_DEFAULT_ANSIBLE_PATHS:
            remove_ansible_path(hostname, exec_prefix, entry.get("id"))
            print("  Removed stale default ansible {} path '{}' (id {}) on control node {} — "
                  "SMLM's own auto-created default, unused by this lab".format(
                      entry.get("type"), entry.get("path"), entry.get("id"), control_node_id))


def ensure_ansible_paths(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_ansible_paths, a list of {control_node_id | system, type, path} dicts. The function is idempotent
    and safe on every configuration pass. It does nothing if the field is unset or empty.

    Each entry names its control node in one of two ways. control_node_id is the numeric Uyuni system ID. system is a hostname,
    resolved through _system_id(). A numeric ID is known only after the system is registered and may change if the system is
    re-registered, so system is the preferred form in a lab JSON file.

    The function also removes the server's default Ansible paths from every control node it touches (see
    remove_stale_default_ansible_paths()). SMLM's Schedule Playbook flow otherwise selects one of those defaults, which can be
    broken, instead of the path registered here.
    """
    paths = cfg.get("{}_ansible_paths".format(prefix)) or []
    touched_control_nodes = set()
    for p in paths:
        control_node_id = p.get("control_node_id")
        system = p.get("system")
        path = p.get("path")
        path_type = p.get("type")
        if control_node_id is None and not system:
            die("{}_ansible_paths: an entry is missing required "
                "'control_node_id' or 'system'".format(prefix))
        if not path or not path_type:
            die("{}_ansible_paths: an entry is missing required 'type'/'path'".format(prefix))
        if control_node_id is None:
            control_node_id = _system_id(hostname, exec_prefix, system)
        ensure_ansible_path(hostname, exec_prefix, control_node_id, path_type, path)
        touched_control_nodes.add(control_node_id)

    for control_node_id in touched_control_nodes:
        remove_stale_default_ansible_paths(hostname, exec_prefix, control_node_id)


def schedule_ansible_playbook(hostname, exec_prefix, control_node_id, playbook_path, inventory_path,
                               earliest=None, action_chain_label="", test_mode=False,
                               extra_vars=None, flush_cache=False):
    """
    Schedule an Ansible playbook run with ansible.schedulePlaybook. There is no spacecmd subcommand for the 'ansible'
    namespace, so the call goes through the api passthrough. `earliest` is an ISO-8601 string. The default is the current UTC
    time, meaning "run as soon as possible". The passthrough converts an ISO-8601 string argument to a datetime, so no manual
    conversion is needed. The function returns the scheduled action id as a string. Pass it to ansible_playbook_status() to check
    on the run.

    Each call schedules a new run, so the call is not idempotent. Invoke it once per intended run, through the install scripts'
    --run-ansible-playbooks flag. Do not place it in the automatic ensure_* flow.
    """
    earliest = earliest or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    args = [playbook_path, inventory_path, control_node_id, earliest, action_chain_label or ""]
    if extra_vars or flush_cache:
        ansible_args = {}
        if extra_vars:
            ansible_args["extraVars"] = extra_vars
        if flush_cache:
            ansible_args["flushCache"] = True
        args.append(bool(test_mode))
        args.append(ansible_args)
    elif test_mode:
        args.append(True)

    r = _api_call(hostname, exec_prefix, "ansible.schedulePlaybook", args)
    if r.returncode != 0:
        die("could not schedule playbook '{}' on control node {}: {}".format(
            playbook_path, control_node_id, (r.stderr or r.stdout or "").strip()))
    action_id = (r.stdout or "").strip()
    print("  Scheduled playbook '{}' on control node {} (inventory: {}) — action id: {}".format(
        playbook_path, control_node_id, inventory_path, action_id))
    return action_id


def ansible_playbook_status(hostname, exec_prefix, action_id):
    """
    Return (details, output): the text of spacecmd's schedule_details and schedule_getoutput commands for a scheduled action id.
    These are native spacecmd commands, unlike the ansible namespace. The function is read-only, and callers decide what to do
    with the text.
    """
    details = _spacecmd(hostname, exec_prefix, "schedule_details {}".format(shlex.quote(str(action_id))))
    output = _spacecmd(hostname, exec_prefix, "schedule_getoutput {}".format(shlex.quote(str(action_id))))
    return (details.stdout or ""), (output.stdout or "")


def _api_call(hostname, exec_prefix, method, args):
    """
    Shared helper: JSON-encode `args` and call `method`, for example "contentmanagement.createProject", through spacecmd's
    generic 'api' passthrough. The ansible, access, contentmanagement and saltkey namespaces have no native spacecmd subcommands,
    so their calls all go through this helper.

    `args` is the caller's list of positional arguments after the session key: [] for a method with no arguments, [x] for one
    argument, [x, y] for two, and so on. spacecmd's -A option takes a single JSON value that is bound to the method's next
    parameter. It does not spread a list into positional arguments. For a method with one argument, the JSON must be that value
    on its own, for example -A "minionId", not a one-element array. A zero-argument method takes [].

    The command is built as `spacecmd -- api -A ... method`. The `--` separator is required, as in _spacecmd(). Without it the
    argument parser rejects -A.
    """
    args_json = json.dumps(args[0] if len(args) == 1 else args)
    return _fault_check(_run(hostname, exec_prefix, "spacecmd -- api -A {} {}".format(shlex.quote(args_json), method),
                              check=False, capture=True))


def content_project_exists(hostname, exec_prefix, label):
    """Whether `label` appears in contentmanagement.listProjects' raw
    output. Deliberately not using lookupProject's fault/exit-code behavior
    on a miss — unconfirmed, same caution as activation_key_exists earlier
    in this module."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjects", [])
    return r.returncode == 0 and label in (r.stdout or "")


def ensure_content_project(hostname, exec_prefix, label, name, description):
    """
    Idempotently create a CLM project with contentmanagement.createProject.
    """
    if content_project_exists(hostname, exec_prefix, label):
        print("  Content project '{}' already exists — leaving it alone".format(label))
        return
    r = _api_call(hostname, exec_prefix, "contentmanagement.createProject", [label, name, description])
    if r.returncode != 0:
        die("could not create content project '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created content project '{}'".format(label))


def content_source_exists(hostname, exec_prefix, project_label, source_label):
    """Whether `source_label` appears in
    contentmanagement.listProjectSources(project_label)'s raw output."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjectSources", [project_label])
    return r.returncode == 0 and source_label in (r.stdout or "")


def ensure_content_source(hostname, exec_prefix, project_label, source_label):
    """
    Idempotently attach `source_label` to CLM project `project_label` with contentmanagement.attachSource. The label is a
    software channel label. "software" is the only Source type in the current source.
    """
    if content_source_exists(hostname, exec_prefix, project_label, source_label):
        print("  Content project '{}' already has source '{}' — leaving it alone".format(
            project_label, source_label))
        return
    r = _api_call(hostname, exec_prefix, "contentmanagement.attachSource",
                  [project_label, "software", source_label])
    if r.returncode != 0:
        die("could not attach source '{}' to content project '{}': {}".format(
            source_label, project_label, (r.stderr or r.stdout or "").strip()))
    print("  Attached source '{}' to content project '{}'".format(source_label, project_label))


def ensure_content_filter(hostname, exec_prefix, project_label, filt):
    """
    Idempotently create a filter and attach it to CLM project `project_label`. The filter is a dict with name, rule ("allow"
    or "deny"), entity_type ("package", "erratum", "module" or "ptf"), matcher, field and value.

    A filter has no lookup-by-name API. It can be looked up only by its numeric id, and only createFilter returns that id. The
    idempotency check is therefore made at the project level: the function looks for the filter's name in the output of
    contentmanagement.listProjectFilters for the project. It is not a global existence check. The new filter's numeric id is taken
    from createFilter's printed return value with a regular expression, because the printed format of a struct through the api
    passthrough is not documented. If the id cannot be extracted, the function dies with the spacecmd api command to run by hand,
    so the filter is never left unattached without a message.
    """
    name = filt.get("name")
    rule = filt.get("rule")
    entity_type = filt.get("entity_type")
    matcher = filt.get("matcher")
    field = filt.get("field")
    value = filt.get("value")
    if not (name and rule and entity_type and matcher and field is not None and value is not None):
        die("content project '{}': a filter entry needs name/rule/entity_type/matcher/field/value".format(
            project_label))

    existing = _api_call(hostname, exec_prefix, "contentmanagement.listProjectFilters", [project_label])
    if existing.returncode == 0 and name in (existing.stdout or ""):
        print("  Content project '{}' already has filter '{}' — leaving it alone".format(project_label, name))
        return

    criteria = {"matcher": matcher, "field": field, "value": value}
    r = _api_call(hostname, exec_prefix, "contentmanagement.createFilter", [name, rule, entity_type, criteria])
    if r.returncode != 0:
        die("could not create filter '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))

    m = re.search(r"['\"]id['\"]\s*:\s*(\d+)", r.stdout or "")
    if not m:
        die("filter '{}' was created but its numeric id could not be parsed from spacecmd's output "
            "to attach it — attach it manually: spacecmd api -A '[\"{}\", <filter_id>]' "
            "contentmanagement.attachFilter (raw output was: {})".format(
                name, project_label, (r.stdout or "").strip()))
    filter_id = int(m.group(1))

    r = _api_call(hostname, exec_prefix, "contentmanagement.attachFilter", [project_label, filter_id])
    if r.returncode != 0:
        die("could not attach filter '{}' (id {}) to content project '{}': {}".format(
            name, filter_id, project_label, (r.stderr or r.stdout or "").strip()))
    print("  Created and attached filter '{}' (id {}) to content project '{}'".format(
        name, filter_id, project_label))


def content_environment_exists(hostname, exec_prefix, project_label, env_label):
    """Whether `env_label` appears in
    contentmanagement.listProjectEnvironments(project_label)'s raw output."""
    r = _api_call(hostname, exec_prefix, "contentmanagement.listProjectEnvironments", [project_label])
    return r.returncode == 0 and env_label in (r.stdout or "")


def ensure_content_environments(hostname, exec_prefix, project_label, environments):
    """
    Idempotently create the ordered lifecycle chain described by `environments`. Each entry is either a label string, where
    name and description default to the label, or a {"label", "name", "description"} dict. Each stage is created with one
    contentmanagement.createEnvironment call. Each stage's predecessorLabel is the label of the previous entry, and the first stage
    gets "". The function does nothing if `environments` is empty.

    A creation or build can stay in "building" indefinitely if the server's asynchronous align worker is already stuck from an
    earlier build in the same server session. This is not specific to any API call, so the wait in
    wait_for_content_environment() can time out. See build_content_project().
    """
    predecessor = ""
    for env in environments or []:
        if isinstance(env, str):
            label, name, description = env, env, env
        else:
            label = env.get("label")
            name = env.get("name") or label
            description = env.get("description") or name
        if not label:
            die("content project '{}': an environment entry is missing required 'label'".format(project_label))

        if content_environment_exists(hostname, exec_prefix, project_label, label):
            print("  Content project '{}' already has environment '{}' — leaving it alone".format(
                project_label, label))
        else:
            r = _api_call(hostname, exec_prefix, "contentmanagement.createEnvironment",
                          [project_label, predecessor, label, name, description])
            if r.returncode != 0:
                die("could not create environment '{}' in content project '{}': {}".format(
                    label, project_label, (r.stderr or r.stdout or "").strip()))
            print("  Created environment '{}' in content project '{}'".format(label, project_label))

        predecessor = label


def ensure_content_projects(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_content_projects, a list of {label, name, description, sources: [...], filters: [...], environments:
    [...]} dicts. The function does nothing if the field is unset or empty. It only defines the projects, sources, filters and
    environments. Builds and promotions are run separately, through the install scripts' --run-clm-actions flag.
    """
    projects = cfg.get("{}_content_projects".format(prefix)) or []
    for proj in projects:
        label = proj.get("label")
        if not label:
            die("{}_content_projects: an entry is missing required 'label'".format(prefix))
        name = proj.get("name") or label
        description = proj.get("description") or name

        ensure_content_project(hostname, exec_prefix, label, name, description)
        for source_label in proj.get("sources") or []:
            ensure_content_source(hostname, exec_prefix, label, source_label)
        for filt in proj.get("filters") or []:
            ensure_content_filter(hostname, exec_prefix, label, filt)
        ensure_content_environments(hostname, exec_prefix, label, proj.get("environments") or [])


def build_content_project(hostname, exec_prefix, project_label, message=None):
    """
    Trigger a build of CLM project `project_label` with contentmanagement.buildProject. The build populates the first
    environment's channels from the project's sources and filters. It is asynchronous: the call returns at once, and the work
    continues on the server. Poll it with content_environment_status() or wait_for_content_environment(). Each call starts a new
    build, so the call is not idempotent, and the automatic install flow does not run it.

    The asynchronous align worker on the server can become stuck after a few builds. Once it is stuck, every later build or
    environment creation stays in "building", whichever API call started it. A server restart is the only confirmed recovery.
    The cause is not identified, and it is likely an upstream bug.
    """
    args = [project_label, message] if message else [project_label]
    r = _api_call(hostname, exec_prefix, "contentmanagement.buildProject", args)
    if r.returncode != 0:
        die("could not build content project '{}': {}".format(project_label, (r.stderr or r.stdout or "").strip()))
    print("  Build triggered for content project '{}'".format(project_label))


def promote_content_project(hostname, exec_prefix, project_label, from_env):
    """
    Triggers promotion of CLM project `project_label` from environment
    `from_env` to its successor via contentmanagement.promoteProject.
    IMPORTANT (confirmed directly in ContentManager.java, not from the
    admin-guide prose, which is ambiguous about this): `from_env` is the
    stage being promoted FROM, not the destination — the server looks up
    its successor itself. Async, same caveats as build_content_project
    (including the confirmed-live "queue can get wedged after a few builds,
    independent of API surface" finding — see its docstring).
    """
    r = _api_call(hostname, exec_prefix, "contentmanagement.promoteProject", [project_label, from_env])
    if r.returncode != 0:
        die("could not promote content project '{}' from environment '{}': {}".format(
            project_label, from_env, (r.stderr or r.stdout or "").strip()))
    print("  Promotion triggered for content project '{}' from environment '{}'".format(project_label, from_env))


def content_environment_status(hostname, exec_prefix, project_label, env_label):
    """
    Return the environment's status field ("new", "building", "generating_repodata", "built" or "failed"), read from the
    raw output of contentmanagement.lookupEnvironment with a regular expression. Returns None if the status cannot be parsed.
    """
    r = _api_call(hostname, exec_prefix, "contentmanagement.lookupEnvironment", [project_label, env_label])
    if r.returncode != 0:
        return None
    m = re.search(r"['\"]status['\"]\s*:\s*['\"]([a-zA-Z_]+)['\"]", r.stdout or "")
    return m.group(1) if m else None


def wait_for_content_environment(hostname, exec_prefix, project_label, env_label,
                                  target_statuses=("built", "failed"), timeout=1800, interval=15,
                                  die_on_timeout=True):
    """
    Poll content_environment_status() every `interval` seconds until the status is one of `target_statuses`, by default
    "built" or "failed". Returns the final status string. The function gives up after `timeout` seconds.

    die_on_timeout defaults to True and dies with a clear message on timeout. With False it returns the last status seen, which
    may be None, so the caller can recover. install_uyuni.py's CLM retry wrapper uses that to restart the server and try again.
    """
    waited = 0
    status = None
    while waited < timeout:
        status = content_environment_status(hostname, exec_prefix, project_label, env_label)
        if status in target_statuses:
            return status
        time.sleep(interval)
        waited += interval
    if not die_on_timeout:
        return status
    die("timed out after {}s waiting for content environment '{}/{}' to reach {} (last seen: {})".format(
        timeout, project_label, env_label, target_statuses, status))


def run_content_lifecycle_actions(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_content_lifecycle_actions: a list of {"project", "action": "build" or "promote", "message" (build
    only), "from_env" (promote only), "wait": bool, "wait_env", "wait_timeout"} dicts, run in order. The actions are not
    idempotent. The install scripts run them through the --run-clm-actions flag, and not through the automatic ensure_* flow.
    """
    actions = cfg.get("{}_content_lifecycle_actions".format(prefix)) or []
    for a in actions:
        project = a.get("project")
        action = a.get("action")
        if not project or action not in ("build", "promote"):
            die("{}_content_lifecycle_actions: an entry needs 'project' and "
                "action 'build' or 'promote'".format(prefix))

        if action == "build":
            build_content_project(hostname, exec_prefix, project, a.get("message"))
        else:
            from_env = a.get("from_env")
            if not from_env:
                die("content_lifecycle_actions: a 'promote' entry requires 'from_env'")
            promote_content_project(hostname, exec_prefix, project, from_env)

        if a.get("wait"):
            wait_env = a.get("wait_env")
            if not wait_env:
                die("content_lifecycle_actions: 'wait' requires 'wait_env' (the environment to poll — "
                    "the first stage for a build, the successor stage for a promote)")
            status = wait_for_content_environment(hostname, exec_prefix, project, wait_env,
                                                   timeout=a.get("wait_timeout") or 1800)
            print("  Environment '{}/{}' reached status '{}'".format(project, wait_env, status))


def scap_scan_exists(hostname, exec_prefix, system, xccdf_path):
    """
    Whether a previous scan against `xccdf_path` already appears in
    'spacecmd scap_listxccdfscans <system>''s output. Heuristic (matches on
    path only, not path+profile — see module docstring): spacecmd's legacy
    XCCDF scan API has no built-in dedup.
    """
    r = _spacecmd(hostname, exec_prefix, "scap_listxccdfscans {}".format(shlex.quote(system)))
    return r.returncode == 0 and xccdf_path in (r.stdout or "")


def ensure_openscap_prerequisites(system, xccdf_path):
    """
    Install the OpenSCAP scanner and SUSE's scap-security-guide content package on `system` with zypper, through SSH to the
    system directly. The system is a lab client, not the server behind exec_prefix.

    Returns True if `xccdf_path` exists on `system` afterwards. The path is located with `rpm -ql scap-security-guide`, because the
    content path depends on the product and version. If the path is missing, the function returns False and warns, rather than
    dying. A scan against a missing profile is a scheduling problem to report through its own error, and it should not stop the
    whole install_smlm.py run.
    """
    r = ssh_run(system, "test -f {}".format(shlex.quote(xccdf_path)), check=False)
    if r.returncode == 0:
        return True
    r = ssh_run(system,
                "zypper --non-interactive install openscap-utils scap-security-guide",
                check=False, capture=True)
    if r.returncode != 0:
        warn("could not install openscap-utils/scap-security-guide on '{}' — the SCAP scan "
             "scheduled against it will likely fail once it actually runs: {}".format(
                 system, (r.stderr or r.stdout or "").strip()[:300]))
        return False
    r = ssh_run(system, "test -f {}".format(shlex.quote(xccdf_path)), check=False)
    if r.returncode != 0:
        warn("installed OpenSCAP content on '{}' but '{}' still doesn't exist there — "
             "check 'rpm -ql scap-security-guide' on that host for the real path this "
             "product/version actually installs content at".format(system, xccdf_path))
        return False
    print("  Installed OpenSCAP + SCAP Security Guide content on '{}'".format(system))
    return True


def ensure_scap_scan(hostname, exec_prefix, system, xccdf_path, profile=None):
    """
    Schedule a legacy XCCDF OpenSCAP scan with spacecmd's native scap_schedulexccdfscan. Before scheduling,
    ensure_openscap_prerequisites() installs the scanner and the content on `system`, so the path exists. Scheduling is skipped,
    though the prerequisites are still ensured, when scap_scan_exists() already finds a scan against the same path for this
    system. The check matches the path only, not the profile.
    """
    ensure_openscap_prerequisites(system, xccdf_path)
    if scap_scan_exists(hostname, exec_prefix, system, xccdf_path):
        print("  System '{}' already has a scan for '{}' — leaving it alone".format(system, xccdf_path))
        return
    xccdf_options = "profile {}".format(profile) if profile else ""
    r = _spacecmd(hostname, exec_prefix, "scap_schedulexccdfscan {} {} {}".format(
        shlex.quote(xccdf_path), shlex.quote(xccdf_options), shlex.quote(system)))
    if r.returncode != 0:
        die("could not schedule XCCDF scan of '{}' on '{}': {}".format(
            xccdf_path, system, (r.stderr or r.stdout or "").strip()))
    print("  Scheduled XCCDF scan of '{}' on '{}' (profile: {})".format(xccdf_path, system, profile or "default"))


def list_scap_scans(hostname, exec_prefix, system):
    """Raw text of 'spacecmd scap_listxccdfscans <system>'. Read-only."""
    return _spacecmd(hostname, exec_prefix, "scap_listxccdfscans {}".format(shlex.quote(system))).stdout or ""


def scap_scan_details(hostname, exec_prefix, xid):
    """Raw text of 'spacecmd scap_getxccdfscandetails <xid>'. Read-only."""
    return _spacecmd(hostname, exec_prefix, "scap_getxccdfscandetails {}".format(shlex.quote(str(xid)))).stdout or ""


def scap_scan_rule_results(hostname, exec_prefix, xid):
    """Raw text of 'spacecmd scap_getxccdfscanruleresults <xid>'. Read-only."""
    return _spacecmd(hostname, exec_prefix,
                      "scap_getxccdfscanruleresults {}".format(shlex.quote(str(xid)))).stdout or ""


def run_scap_scans(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_scap_scans: a list of {system or group, xccdf_path, profile} dicts, run in order through
    ensure_scap_scan(). Give exactly one of `system` or `group`. A group expands to every member of that system group through
    list_group_systems(), so one entry can scan a whole group. The scans are one-shot work, so the automatic install flow does
    not run them. Use the install scripts' --run-scap-scans flag. The function does nothing if the field is unset or empty.
    """
    scans = cfg.get("{}_scap_scans".format(prefix)) or []
    for s in scans:
        system = s.get("system")
        group = s.get("group")
        xccdf_path = s.get("xccdf_path")
        if bool(system) == bool(group):
            die("{}_scap_scans: an entry needs exactly one of 'system' or 'group'".format(prefix))
        if not xccdf_path:
            die("{}_scap_scans: an entry is missing 'xccdf_path'".format(prefix))
        targets = [system] if system else [
            line.strip() for line in list_group_systems(hostname, exec_prefix, group).splitlines()
            if line.strip()
        ]
        if group and not targets:
            warn("{}_scap_scans: group '{}' has no members — nothing to scan".format(prefix, group))
        for target in targets:
            ensure_scap_scan(hostname, exec_prefix, target, xccdf_path, profile=s.get("profile"))


def list_systems_by_patch_status(hostname, exec_prefix, cve_id, patch_status_labels=None):
    """
    Return the raw output of audit.listSystemsByPatchStatus(cveId[, statusLabels]). spacecmd has no subcommand for the audit
    namespace, so the call goes through the api passthrough. It is a read-only query, with nothing to schedule and no idempotency
    concern. `patch_status_labels`, if given, is a list of labels from {"AFFECTED_PATCH_INAPPLICABLE",
    "AFFECTED_PATCH_APPLICABLE", "NOT_AFFECTED", "PATCHED"} to filter by.
    """
    args = [cve_id, list(patch_status_labels)] if patch_status_labels else [cve_id]
    r = _api_call(hostname, exec_prefix, "audit.listSystemsByPatchStatus", args)
    if r.returncode != 0:
        die("could not audit CVE '{}': {}".format(cve_id, (r.stderr or r.stdout or "").strip()))
    return r.stdout or ""


def list_images_by_patch_status(hostname, exec_prefix, cve_id, patch_status_labels=None):
    """
    Return the raw output of audit.listImagesByPatchStatus(cveId[, statusLabels]). This is the counterpart of
    list_systems_by_patch_status() for container and OS images, not registered systems. The audit namespace has these two
    methods only. It is read-only, and it takes the same optional `patch_status_labels` filter.

    The SMLM 5.2 beta policy methods in the system.scap namespace (listPolicies, listScapContent, listTailoringFiles,
    scheduleBetaXccdfScanCustom and scheduleBetaXccdfScanWithPolicy) exist on the server. Their catalog objects can only be
    uploaded through the Web UI, and the beta scan calls need beta features enabled in the acting user's account preferences.
    This module uses the older system.scap.scheduleXccdfScan instead, which reads XCCDF files already on the target system and
    needs no catalog objects or beta flag.
    """
    args = [cve_id, list(patch_status_labels)] if patch_status_labels else [cve_id]
    r = _api_call(hostname, exec_prefix, "audit.listImagesByPatchStatus", args)
    if r.returncode != 0:
        die("could not audit images for CVE '{}': {}".format(cve_id, (r.stderr or r.stdout or "").strip()))
    return r.stdout or ""


# ─── SCAP Beta policy-based scanning (system.scap.*) — examples ─────────────
# The five functions below wrap the Beta system.scap methods. Parameter names and structure follow the 5.2 API reference.
# The acting user must enable beta features in the Web UI account preferences. The XML-RPC API has no toggle for that.
# listPolicies, listScapContent and listTailoringFiles are read-only. Their catalog objects are uploaded through the Web UI.
# The usual flow is to upload the content, policy or tailoring file in the Web UI, use the list functions to find its
# numeric id, and then trigger a scan with one of the two schedule functions.

def list_scap_content(hostname, exec_prefix):
    """
    Returns system.scap.listScapContent(sessionKey) — every SCAP content
    object (a DataStream + XCCDF file pair) already uploaded via the Web
    UI, each a dict with real fields id/name/description/
    dataStreamFileName/xccdfFileName. Pure read-only catalog lookup: use
    the returned "id" as schedule_beta_xccdf_scan_custom()'s own
    scap_content_id argument.
    """
    r = _api_call(hostname, exec_prefix, "system.scap.listScapContent", [])
    if r.returncode != 0:
        die("could not list SCAP content: {}".format((r.stderr or r.stdout or "").strip()))
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die("system.scap.listScapContent returned unparseable output: {}".format(r.stdout))


def list_scap_policies(hostname, exec_prefix):
    """
    Returns system.scap.listPolicies(sessionKey) — every saved SCAP policy
    (a content+profile+tailoring combination, pre-bundled via the Web UI so
    a scan can be triggered by policy id alone), each a dict with real
    fields id/policyName/description/scapContentId/xccdfProfileId/
    tailoringFileId/tailoringProfileId/ovalFiles/advancedArgs/
    fetchRemoteResources. Pure read-only catalog lookup: use the returned
    "id" as schedule_beta_xccdf_scan_with_policy()'s own policy_id argument.
    """
    r = _api_call(hostname, exec_prefix, "system.scap.listPolicies", [])
    if r.returncode != 0:
        die("could not list SCAP policies: {}".format((r.stderr or r.stdout or "").strip()))
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die("system.scap.listPolicies returned unparseable output: {}".format(r.stdout))


def list_scap_tailoring_files(hostname, exec_prefix):
    """
    Returns system.scap.listTailoringFiles(sessionKey) — every saved SCAP
    tailoring file (an override of a content's own default XCCDF profile
    rules) already uploaded via the Web UI, each a dict with real fields
    id/name/fileName/orgId. Pure read-only catalog lookup: use the
    returned "id" as either schedule_beta_xccdf_scan_custom()'s own
    tailoring_file_id argument, or as a policy's own tailoringFileId when
    building one through the Web UI.
    """
    r = _api_call(hostname, exec_prefix, "system.scap.listTailoringFiles", [])
    if r.returncode != 0:
        die("could not list SCAP tailoring files: {}".format((r.stderr or r.stdout or "").strip()))
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die("system.scap.listTailoringFiles returned unparseable output: {}".format(r.stdout))


def schedule_beta_xccdf_scan_with_policy(hostname, exec_prefix, systems, policy_id, date=None):
    """
    Schedules a SCAP scan against `systems` (a list of hostnames/minion
    ids, resolved to numeric sids via _system_id()) using an existing,
    already-uploaded SCAP policy — see list_scap_policies() for real
    policy ids on this server — via
    system.scap.scheduleBetaXccdfScanWithPolicy(sessionKey, sids, policyId,
    date). `date` is an ISO-8601 string (default: the current UTC time,
    i.e. "run as soon as possible") — spacecmd's own 'api' passthrough
    auto-converts a top-level ISO-8601-looking string into a real
    dateTime.iso8601 before the XML-RPC call, the same mechanism
    schedule_ansible_playbook() already relies on; no manual DateTime
    construction needed here either. Returns the real numeric SCAP action
    id. NOT IDEMPOTENT: each call schedules a brand-new scan, same
    one-shot reasoning as every other schedule_* function in this module —
    meant to be invoked explicitly, never as part of the automatic
    ensure_* flow.
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sids = [_system_id(hostname, exec_prefix, s) for s in systems]
    r = _api_call(hostname, exec_prefix, "system.scap.scheduleBetaXccdfScanWithPolicy",
                  [sids, int(policy_id), date])
    if r.returncode != 0:
        die("could not schedule SCAP policy scan (policy {}) on {}: {}".format(
            policy_id, systems, (r.stderr or r.stdout or "").strip()))
    action_id = (r.stdout or "").strip()
    print("  Scheduled SCAP scan (policy {}) on {} — action id: {}".format(policy_id, systems, action_id))
    return action_id


def schedule_beta_xccdf_scan_custom(hostname, exec_prefix, systems, scap_content_id, xccdf_profile_id,
                                    tailoring_file_id=None, tailoring_profile_id=None, oval_files=None,
                                    advanced_args=None, fetch_remote_resources=False, date=None):
    """
    Schedules a SCAP scan against `systems` with an explicit
    content+profile combination instead of a pre-saved policy (see
    schedule_beta_xccdf_scan_with_policy() for that path), via
    system.scap.scheduleBetaXccdfScanCustom(sessionKey, sids, params,
    date). `scap_content_id` comes from list_scap_content(); the real API
    doesn't expose a way to discover a content's own valid
    `xccdf_profile_id` values — read them directly out of the uploaded
    XCCDF/DataStream document, or from the Web UI's own scan-scheduling
    form. `tailoring_file_id`/`tailoring_profile_id` (see
    list_scap_tailoring_files()) let a saved tailoring override the
    content's own default profile rules; `oval_files`/`advanced_args`/
    `fetch_remote_resources` map straight onto the real params struct's own
    same-named optional keys (fetch_remote_resources defaults to False,
    matching the API's own documented default). `date` handling is
    identical to schedule_beta_xccdf_scan_with_policy() above. Returns the
    real numeric SCAP action id. NOT IDEMPOTENT — same one-shot reasoning
    as every other schedule_* function in this module.
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sids = [_system_id(hostname, exec_prefix, s) for s in systems]
    params = {"scapContentId": int(scap_content_id), "xccdfProfileId": xccdf_profile_id}
    if tailoring_file_id is not None:
        params["tailoringFileId"] = int(tailoring_file_id)
    if tailoring_profile_id:
        params["tailoringProfileId"] = tailoring_profile_id
    if oval_files:
        params["ovalFiles"] = oval_files
    if advanced_args:
        params["advancedArgs"] = advanced_args
    if fetch_remote_resources:
        params["fetchRemoteResources"] = True

    r = _api_call(hostname, exec_prefix, "system.scap.scheduleBetaXccdfScanCustom", [sids, params, date])
    if r.returncode != 0:
        die("could not schedule custom SCAP scan (content {}, profile {}) on {}: {}".format(
            scap_content_id, xccdf_profile_id, systems, (r.stderr or r.stdout or "").strip()))
    action_id = (r.stdout or "").strip()
    print("  Scheduled custom SCAP scan (content {}, profile {}) on {} — action id: {}".format(
        scap_content_id, xccdf_profile_id, systems, action_id))
    return action_id


# ─── SCAP policy creation (Web UI REST route, not XML-RPC) ──────────
# Policy creation has no XML-RPC method. The Web UI exposes it as a REST route, POST /rhn/manager/api/audit/scap/policy/create,
# which takes JSON with the ScapPolicyJson field names. Every /rhn/manager/api/* route is exempt from CSRF checks, so a
# session cookie from a scripted login is enough. This is a private, internal API, not a versioned public one, and it may
# change between releases.
#
# Content and tailoring files are uploaded through the same controller as multipart POST requests. This module does not
# upload them. The files would first have to be copied into the container that spacecmd runs in, and this project has no
# helper for that copy yet.

_SCAP_WEB_COOKIE_JAR = "/tmp/lab-in-a-box-scap-session.jar"


def scap_web_login(hostname, exec_prefix, user, password):
    """
    Log into the Web UI with POST /rhn/manager/api/login, and store the session cookie in a cookie jar inside the container.
    SCAP policy creation has no XML-RPC method, so it needs this session. The login runs with curl inside the same container that
    spacecmd execs into, through _run(), where the web server is reachable at https://localhost. The cookie jar is a file in the
    container, so it survives across separate exec calls, and create_scap_policy() reuses it. The function dies if the login
    fails, since a wrong password is a configuration error.
    """
    login_json = json.dumps({"login": user, "password": password})
    cmd = ("curl -sk -c {jar} -o /dev/null -w '%{{http_code}}' "
           "-H 'Content-Type: application/json' -X POST -d {data} "
           "https://localhost/rhn/manager/api/login").format(
               jar=shlex.quote(_SCAP_WEB_COOKIE_JAR), data=shlex.quote(login_json))
    r = _run(hostname, exec_prefix, cmd, check=False, capture=True)
    status = (r.stdout or "").strip()
    if r.returncode != 0 or status != "200":
        die("could not log into the Web UI as '{}' (HTTP {}): {}".format(
            user, status or "?", (r.stderr or "").strip()))


def scap_policy_exists(hostname, exec_prefix, policy_name):
    """
    Whether a SCAP policy named `policy_name` already exists, via
    list_scap_policies() — the real XML-RPC read path, and the SAME
    underlying ScapPolicy database row the web-only create route below
    writes to (both go through the same ScapFactory/ScapPolicy domain
    classes, confirmed directly from the real source), so this stays a
    reliable idempotency check despite the two paths using different
    protocols.
    """
    policies = list_scap_policies(hostname, exec_prefix)
    return any(p.get("policyName") == policy_name for p in policies)


def create_scap_policy(hostname, exec_prefix, policy_name, scap_content_id, xccdf_profile_id,
                       description=None, earliest=None, tailoring_file=None, tailoring_profile_id=None,
                       oval_files=None, advanced_args=None, fetch_remote_resources=False):
    """
    Create a SCAP policy with POST /rhn/manager/api/audit/scap/policy/create. This is the only mechanism for it. Call
    scap_web_login() first on the same host and exec_prefix, so the cookie jar exists.

    policy_name, scap_content_id (from list_scap_content()) and xccdf_profile_id are required. Every other parameter is optional
    and uses the same field names as the server's ScapPolicyJson. `earliest`, if given, is a string in ISO_LOCAL_DATE_TIME format,
    for example "2026-10-01T00:00:00". This is a plain JSON string field, unlike the XML-RPC schedule calls, which convert a
    top-level ISO-8601 argument. The function returns the numeric policy id. It is not idempotent on its own. Use
    ensure_scap_policies(), which checks for existing policies first.
    """
    body = {"policyName": policy_name, "scapContentId": int(scap_content_id), "xccdfProfileId": xccdf_profile_id}
    if description:
        body["description"] = description
    if earliest:
        body["earliest"] = earliest
    if tailoring_file:
        body["tailoringFile"] = tailoring_file
    if tailoring_profile_id:
        body["tailoringProfileId"] = tailoring_profile_id
    if oval_files:
        body["ovalFiles"] = oval_files
    if advanced_args:
        body["advancedArgs"] = advanced_args
    if fetch_remote_resources:
        body["fetchRemoteResources"] = True

    body_json = json.dumps(body)
    cmd = ("curl -sk -b {jar} -c {jar} -H 'Content-Type: application/json' -X POST -d {data} "
           "https://localhost/rhn/manager/api/audit/scap/policy/create").format(
               jar=shlex.quote(_SCAP_WEB_COOKIE_JAR), data=shlex.quote(body_json))
    r = _run(hostname, exec_prefix, cmd, check=False, capture=True)
    if r.returncode != 0:
        die("could not create SCAP policy '{}': {}".format(policy_name, (r.stderr or "").strip()))
    try:
        result = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die("SCAP policy create for '{}' returned unparseable output (session cookie expired or "
            "scap_web_login() was never called?): {}".format(policy_name, r.stdout))
    if not result.get("success"):
        die("could not create SCAP policy '{}': {}".format(
            policy_name, "; ".join(result.get("messages") or []) or r.stdout))
    policy_id = result.get("data")
    print("  Created SCAP policy '{}' (id: {})".format(policy_name, policy_id))
    return policy_id


def ensure_scap_policies(hostname, exec_prefix, cfg, prefix, admin_user, admin_pass):
    """
    Orchestrate <prefix>_scap_policies: a list of dicts with the ScapPolicyJson keys. policy_name, scap_content_id and
    xccdf_profile_id are required. description, earliest, tailoring_file, tailoring_profile_id, oval_files, advanced_args and
    fetch_remote_resources are optional, and they match create_scap_policy()'s parameters.

    The function is idempotent. It logs in once with scap_web_login(), using `admin_user` and `admin_pass` from the caller, which
    are the admin account the caller resolved. The config key names differ between install_smlm.py and install_uyuni.py, so the
    caller passes them explicitly. It lists the existing policies once, and creates only the policies that are not present by
    name. The function does nothing if the field is unset or empty.
    """
    policies = cfg.get("{}_scap_policies".format(prefix)) or []
    if not policies:
        return
    scap_web_login(hostname, exec_prefix, admin_user, admin_pass)
    existing = list_scap_policies(hostname, exec_prefix)
    existing_names = {p.get("policyName") for p in existing}

    for p in policies:
        policy_name = p.get("policy_name")
        scap_content_id = p.get("scap_content_id")
        xccdf_profile_id = p.get("xccdf_profile_id")
        if not policy_name or scap_content_id is None or not xccdf_profile_id:
            die("{}_scap_policies: an entry needs 'policy_name', 'scap_content_id' and "
                "'xccdf_profile_id'".format(prefix))
        if policy_name in existing_names:
            print("  SCAP policy '{}' already exists — leaving it alone".format(policy_name))
            continue
        create_scap_policy(hostname, exec_prefix, policy_name, scap_content_id, xccdf_profile_id,
                           description=p.get("description"), earliest=p.get("earliest"),
                           tailoring_file=p.get("tailoring_file"),
                           tailoring_profile_id=p.get("tailoring_profile_id"),
                           oval_files=p.get("oval_files"), advanced_args=p.get("advanced_args"),
                           fetch_remote_resources=bool(p.get("fetch_remote_resources")))


# ─── dev/QA/prod environment topology ────────────────────────────────────────
# System groups, multi-key activation-key/group linkage, custom-info tags,
# recurring patch schedules, and a thin composition layer over all of the
# above plus CLM (already built) — see the module docstring's "dev/QA/prod
# environment topology" entry for what's confirmed vs. deferred vs. heuristic.

def ensure_activation_keys(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_activation_keys: a list of dicts. Each uses the same <prefix>_activation_key* field names as the
    top-level single-key fields, and each entry is applied with one call. A lab can define several named activation keys, one per
    environment for example, in one organization. The function does nothing if the field is unset or empty.
    """
    keys = cfg.get("{}_activation_keys".format(prefix)) or []
    for key_cfg in keys:
        ensure_activation_key(hostname, exec_prefix, key_cfg, prefix)
        ensure_appstreams(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_packages(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_groups(hostname, exec_prefix, key_cfg, prefix)
        ensure_activation_key_child_channels(hostname, exec_prefix, key_cfg, prefix)


def activation_key_child_channels(hostname, exec_prefix, key_name):
    """Returns the set of child channel labels currently linked to
    activation key `key_name`, via spacecmd's native
    activationkey_listchildchannels."""
    r = _spacecmd(hostname, exec_prefix, "activationkey_listchildchannels {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_child_channels(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently link every child channel in <prefix>_activation_key_child_channels to <prefix>_activation_key, with
    activationkey_addchildchannels. The current list comes from activationkey_listchildchannels, so the call is idempotent. It runs
    on every configuration pass, not only when the key is created.

    ensure_activation_key() links child channels only when it creates the key. A key that already exists therefore never gets a
    child channel added from a later edit of the lab JSON. This function closes that gap. The function does nothing if the key or
    the child-channels field is unset. It is harmless alongside the creation-time linking.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_child_channels".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_child_channels(hostname, exec_prefix, key_name)
    missing = [c for c in spec if c not in existing]
    if not missing:
        print("  Activation key '{}' already linked to all requested child channels — "
              "leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addchildchannels {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(c) for c in missing)))
    if r.returncode != 0:
        warn("could not link child channels ({}) to activation key '{}' — check they're "
             "actually synced on the server (`mgr-sync add channels`), not just referenced "
             "in {}_activation_key_child_channels: {}".format(
                 ", ".join(missing), key_name, prefix, (r.stderr or r.stdout or "").strip()))
        return
    print("  Linked {} child channel(s) to activation key '{}': {}".format(
        len(missing), key_name, ", ".join(missing)))


def activation_key_groups(hostname, exec_prefix, key_name):
    """Returns the set of system group names currently linked to
    activation key `key_name`, via spacecmd's native
    activationkey_listgroups."""
    r = _spacecmd(hostname, exec_prefix, "activationkey_listgroups {}".format(shlex.quote(key_name)))
    if r.returncode != 0:
        return set()
    return set(line.strip() for line in (r.stdout or "").splitlines() if line.strip())


def ensure_activation_key_groups(hostname, exec_prefix, cfg, prefix):
    """
    Idempotently link every system group in <prefix>_activation_key_groups (space-separated) to <prefix>_activation_key,
    with activationkey_addgroups. The current list comes from activationkey_listgroups, so the call is idempotent. It runs on
    every configuration pass, like ensure_activation_key_packages(). ensure_activation_key() applies the same field only when it
    creates the key, so calling both is harmless. The function does nothing if the key or the groups field is unset.
    """
    key_name = cfg.get("{}_activation_key".format(prefix))
    spec = (cfg.get("{}_activation_key_groups".format(prefix)) or "").split()
    if not key_name or not spec:
        return
    key_name = resolve_activation_key_name(hostname, exec_prefix, key_name)

    existing = activation_key_groups(hostname, exec_prefix, key_name)
    missing = [g for g in spec if g not in existing]
    if not missing:
        print("  Activation key '{}' already linked to all requested groups — leaving it alone".format(key_name))
        return

    r = _spacecmd(hostname, exec_prefix, "activationkey_addgroups {} {}".format(
        shlex.quote(key_name), " ".join(shlex.quote(g) for g in missing)))
    if r.returncode != 0:
        die("could not link groups to activation key '{}': {}".format(
            key_name, (r.stderr or r.stdout or "").strip()))
    print("  Linked {} group(s) to activation key '{}': {}".format(len(missing), key_name, ", ".join(missing)))


def group_exists(hostname, exec_prefix, name):
    """Whether `name` appears in 'spacecmd group_list''s output."""
    r = _spacecmd(hostname, exec_prefix, "group_list")
    return r.returncode == 0 and name in (r.stdout or "")


def ensure_system_group(hostname, exec_prefix, name, description=None):
    """
    Idempotently create a system group with group_create.
    """
    if group_exists(hostname, exec_prefix, name):
        print("  System group '{}' already exists — leaving it alone".format(name))
        return
    r = _spacecmd(hostname, exec_prefix, "group_create {} {}".format(
        shlex.quote(name), shlex.quote(description or name)))
    if r.returncode != 0:
        die("could not create system group '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created system group '{}'".format(name))


def list_group_systems(hostname, exec_prefix, name):
    """Raw text of 'spacecmd group_listsystems <name>' — one system name
    per line, per source. Read-only."""
    return _spacecmd(hostname, exec_prefix, "group_listsystems {}".format(shlex.quote(name))).stdout or ""


def group_has_system(hostname, exec_prefix, group_name, system):
    """Whether `system` appears in group_name's member list."""
    return system in list_group_systems(hostname, exec_prefix, group_name)


def ensure_group_systems(hostname, exec_prefix, group_name, systems):
    """
    Idempotently ensure every system name in `systems` is a member of `group_name`, with group_addsystems. Only the names not
    already listed by group_listsystems are added. The function does nothing if `systems` is empty.

    group_addsystems skips any name that is not a registered system, as long as at least one name in the same call is valid. If
    every name is invalid, the call fails. A lab's hostname list often contains systems that are not registered yet, so the
    failure is reported as a warning rather than a fatal error. Once those systems register, a later run adds them.
    """
    if not systems:
        return
    existing = list_group_systems(hostname, exec_prefix, group_name)
    missing = [s for s in systems if s not in existing]
    if not missing:
        print("  System group '{}' already has all requested systems — leaving it alone".format(group_name))
        return
    r = _spacecmd(hostname, exec_prefix, "group_addsystems {} {}".format(
        shlex.quote(group_name), " ".join(shlex.quote(s) for s in missing)))
    if r.returncode != 0:
        warn("could not add system(s) ({}) to group '{}' — they may not be registered systems yet: "
             "{}".format(", ".join(missing), group_name, (r.stderr or r.stdout or "").strip()))
        return
    print("  Added {} system(s) to group '{}': {}".format(len(missing), group_name, ", ".join(missing)))


def ensure_system_groups(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_system_groups, a list of {name, description, systems: [...]} dicts. The function is idempotent and
    safe on every configuration pass. It does nothing if the field is unset or empty.
    """
    groups = cfg.get("{}_system_groups".format(prefix)) or []
    for g in groups:
        name = g.get("name")
        if not name:
            die("{}_system_groups: an entry is missing required 'name'".format(prefix))
        ensure_system_group(hostname, exec_prefix, name, g.get("description"))
        ensure_group_systems(hostname, exec_prefix, name, g.get("systems") or [])


def custom_info_key_exists(hostname, exec_prefix, name):
    """Whether `name` appears in 'spacecmd custominfo_listkeys''s output."""
    r = _spacecmd(hostname, exec_prefix, "custominfo_listkeys")
    return r.returncode == 0 and name in (r.stdout or "")


def ensure_custom_info_key(hostname, exec_prefix, name, description=None):
    """
    Idempotently define an organization-level custom info key with custominfo_createkey. A value cannot be set for a key on any
    system until the key is defined.
    """
    if custom_info_key_exists(hostname, exec_prefix, name):
        print("  Custom info key '{}' already exists — leaving it alone".format(name))
        return
    r = _spacecmd(hostname, exec_prefix, "custominfo_createkey {} {}".format(
        shlex.quote(name), shlex.quote(description or name)))
    if r.returncode != 0:
        die("could not create custom info key '{}': {}".format(name, (r.stderr or r.stdout or "").strip()))
    print("  Created custom info key '{}'".format(name))


def ensure_custom_info_keys(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_custom_info_keys, a list of {name, description} dicts. The function does nothing if the field is unset
    or empty.
    """
    keys = cfg.get("{}_custom_info_keys".format(prefix)) or []
    for k in keys:
        name = k.get("name")
        if not name:
            die("{}_custom_info_keys: an entry is missing required 'name'".format(prefix))
        ensure_custom_info_key(hostname, exec_prefix, name, k.get("description"))


def ensure_system_tag(hostname, exec_prefix, system, key, value):
    """
    Set a custom-info key=value pair on `system` with system_addcustomvalue. Uyuni has no tag object, so a tag is a custom-info
    value. The call is treated as an upsert, without a pre-check: system_updatecustomvalue is an alias for the same underlying
    call. The key must already be defined with ensure_custom_info_key(), or the call fails.
    """
    r = _spacecmd(hostname, exec_prefix, "system_addcustomvalue {} {} {}".format(
        shlex.quote(key), shlex.quote(value), shlex.quote(system)))
    if r.returncode != 0:
        die("could not set tag '{}={}' on system '{}': {}".format(
            key, value, system, (r.stderr or r.stdout or "").strip()))
    print("  Set tag '{}={}' on system '{}'".format(key, value, system))


def ensure_system_tags(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_system_tags, a list of {system, tags: {key: value, ...}} dicts. The function does nothing if the field
    is unset or empty.
    """
    entries = cfg.get("{}_system_tags".format(prefix)) or []
    for entry in entries:
        system = entry.get("system")
        tags = entry.get("tags") or {}
        if not system or not tags:
            die("{}_system_tags: an entry needs 'system' and a non-empty 'tags' map".format(prefix))
        for key, value in tags.items():
            ensure_system_tag(hostname, exec_prefix, system, key, str(value))


def group_id_for(hostname, exec_prefix, group_name):
    """
    Best-effort numeric id lookup for a system group by name, by regexing
    'spacecmd group_details <name>''s human-readable output — spacecmd's
    exact display format for this wasn't independently confirmed, so this
    is a heuristic. Returns None if no id could be parsed; callers should
    fall back to an explicit group_id given directly in the JSON, same
    precedent as Ansible integration's control_node_id.
    """
    r = _spacecmd(hostname, exec_prefix, "group_details {}".format(shlex.quote(group_name)))
    if r.returncode != 0:
        return None
    m = re.search(r"(?im)^\s*(?:group\s*)?id\s*:\s*(\d+)\s*$", r.stdout or "")
    return int(m.group(1)) if m else None


def ensure_recurring_schedule(hostname, exec_prefix, entity_type, entity_id, cron_expr,
                               name, schedule_type="highstate", states=None, test=None, extra=None):
    """
    Create a recurring action with recurring.highstate.create or recurring.custom.create. spacecmd does not wrap these calls, so
    they go through the api passthrough. `entity_type` is "minion", "group" or "org", and `entity_id` is its numeric id;
    group_id_for() resolves a system group's id. `schedule_type` is "highstate" for a Salt highstate, or "custom" for an ordered
    `states` list, which is required in that case.

    `name` is required, and the action fails without it. `test`, if given, maps to the optional dry-run boolean. `extra` is merged
    into the action properties as given, for other options the API accepts.

    `cron_expr` must be a Quartz cron expression with 6 or 7 fields: seconds, minutes, hours, day of month, month, day of week,
    and optionally year. A standard 5-field Unix cron string is rejected. One of day of month and day of week must be "?" when the
    other has a value. Examples: "0 0 2 * * ?" runs daily at 02:00, and "0 0 3 ? * MON" runs every Monday at 03:00.

    The function is idempotent. recurring.listByEntity lists the existing actions for the entity, and creation is skipped when an
    action with the same name exists. The call relies on _fault_check() to report a rejected create. spacecmd exits with 0 even
    when the server returns a fault.
    """
    if schedule_type not in ("highstate", "custom"):
        die("invalid recurring schedule type '{}': expected 'highstate' or 'custom'".format(schedule_type))

    r = _api_call(hostname, exec_prefix, "recurring.listByEntity", [entity_type, entity_id])
    existing_names = set()
    if r.returncode == 0 and (r.stdout or "").strip():
        try:
            existing_names = {a.get("name") for a in json.loads(r.stdout) if isinstance(a, dict)}
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    if name in existing_names:
        print("  Recurring {} schedule '{}' for {} {} already exists — leaving it alone".format(
            schedule_type, name, entity_type, entity_id))
        return

    props = {"entity_type": entity_type, "entity_id": entity_id, "cron_expr": cron_expr, "name": name}
    if schedule_type == "custom":
        if not states:
            die("recurring schedule type 'custom' requires a non-empty 'states' list")
        props["states"] = list(states)
    if test is not None:
        props["test"] = bool(test)
    if extra:
        props.update(extra)

    method = "recurring.{}.create".format(schedule_type)
    r = _api_call(hostname, exec_prefix, method, [props])
    if r.returncode != 0:
        die("could not create recurring {} schedule '{}' for {} {}: {}".format(
            schedule_type, name, entity_type, entity_id, (r.stderr or r.stdout or "").strip()))
    print("  Created recurring {} schedule '{}' for {} {} (cron: {})".format(
        schedule_type, name, entity_type, entity_id, cron_expr))


def ensure_recurring_schedules(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_recurring_actions: a list of dicts:
      {"name": "...", "entity_type": "minion"|"group"|"org",
       "entity": "<system-or-group-name>" (resolved to a numeric id below),
       "cron_expr": "...", "schedule_type": "highstate"|"custom" (default
       "highstate"), "states": [...] (required if schedule_type=custom)}
    `entity` is resolved via _system_id() for "minion" or group_id_for()
    for "group"; "org" isn't resolved (Uyuni's own org-id numbering starts
    at 1, same convention already used elsewhere in this module — pass the
    numeric id directly as `entity`). No-op if the field is unset/empty.
    """
    for entry in cfg.get("{}_recurring_actions".format(prefix)) or []:
        name = entry.get("name")
        entity_type = entry.get("entity_type")
        entity = entry.get("entity")
        cron_expr = entry.get("cron_expr")
        if not (name and entity_type and entity and cron_expr):
            die("{}_recurring_actions: an entry is missing one of name/entity_type/entity/"
                "cron_expr".format(prefix))

        if entity_type == "minion":
            entity_id = _system_id(hostname, exec_prefix, entity)
        elif entity_type == "group":
            entity_id = group_id_for(hostname, exec_prefix, entity)
            if entity_id is None:
                die("{}_recurring_actions: no system group named '{}' found".format(prefix, entity))
        elif entity_type == "org":
            entity_id = int(entity)
        else:
            die("{}_recurring_actions: invalid entity_type '{}' (expected minion/group/org)".format(
                prefix, entity_type))

        ensure_recurring_schedule(hostname, exec_prefix, entity_type, entity_id, cron_expr, name,
                                   schedule_type=entry.get("schedule_type", "highstate"),
                                   states=entry.get("states"))


def _system_id(hostname, exec_prefix, target_system):
    """
    Resolves `target_system` (a hostname/minion id) to its real numeric
    system id via system.getId — needed for formula.* below, which (unlike
    every spacecmd-native call in this module) takes a numeric sid, not a
    hostname string. Real, confirmed method (documentation.suse.com/
    multi-linux-manager's own API reference, system.getId(sessionKey,
    name) -> array of {id, name, last_checkin, ...} structs — one per
    system whose name/hostname matches, since Uyuni doesn't enforce unique
    hostnames). Dies on zero or more-than-one match — this project's own
    FQDN convention (every node's hostname IS its minion id, see module
    docstring) means more than one match is a genuine ambiguity, not
    something to guess through.
    """
    r = _api_call(hostname, exec_prefix, "system.getId", [target_system])
    if r.returncode != 0:
        die("could not resolve system id for '{}': {}".format(
            target_system, (r.stderr or r.stdout or "").strip()))
    try:
        matches = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        die("system.getId('{}') returned unparseable output: {}".format(target_system, r.stdout))
    if not matches:
        die("no system named '{}' found on the server".format(target_system))
    if len(matches) > 1:
        die("more than one system named '{}' found on the server — genuinely ambiguous".format(
            target_system))
    return matches[0]["id"]


# ── Virtual Host Managers (Systems -> Virtual Host Managers) ────────────────
# These calls go through the api passthrough. spacecmd has no native subcommand for them. They use the
# virtualhostmanager.* namespace, where create(sessionKey, label, moduleName, parameters) returns an int.
#
# For AWS, moduleName is "AmazonEC2", the gatherer's class name. Its parameters are access_key_id, secret_access_key,
# region and zone. The Web UI sends the same name. Only the AWS module is implemented here.

def virtual_host_manager_exists(hostname, exec_prefix, label):
    """
    Return True if `label` appears in the raw output of virtualhostmanager.listVirtualHostManagers. The check is a substring
    match, because the printed format of the list is not documented. The labels this project creates are specific enough that a
    substring match is safe.
    """
    r = _api_call(hostname, exec_prefix, "virtualhostmanager.listVirtualHostManagers", [])
    return r.returncode == 0 and label in (r.stdout or "")


def ensure_virtual_host_manager_aws(hostname, exec_prefix, vhm):
    """
    Idempotently create one Amazon EC2 Virtual Host Manager from one entry of <prefix>_virtual_host_managers: {label,
    access_key_id, secret_access_key, region, zone}. The module name is "AmazonEC2", and these four fields are its parameters.

    access_key_id and secret_access_key must be long-lived AWS credentials. A temporary SSO or STS session would expire and stop the
    gatherer's periodic polling. For that reason this function does not use the cloud-account resolution that the VM backends use.
    ensure_virtual_host_managers() shows how the credentials reach this function.

    VirtualHostManager.create refuses a duplicate label, so an existing manager is detected first and skipped. The function dies
    on any other API failure.
    """
    label = vhm.get("label")
    if not label:
        die("virtual_host_managers: an entry is missing required 'label'")
    if virtual_host_manager_exists(hostname, exec_prefix, label):
        print("  Virtual Host Manager '{}' already exists — leaving it alone".format(label))
        return
    access_key_id = vhm.get("access_key_id")
    secret_access_key = vhm.get("secret_access_key")
    region = vhm.get("region")
    zone = vhm.get("zone")
    if not (access_key_id and secret_access_key and region and zone):
        die("virtual host manager '{}': access_key_id, secret_access_key, region and zone are "
            "all required to create it".format(label))
    params = {
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "region": region,
        "zone": zone,
    }
    r = _api_call(hostname, exec_prefix, "virtualhostmanager.create", [label, "AmazonEC2", params])
    if r.returncode != 0:
        die("could not create Virtual Host Manager '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Created Amazon EC2 Virtual Host Manager '{}' (region: {}, zone: {})".format(
        label, region, zone))


def ensure_virtual_host_manager_libvirt(hostname, exec_prefix, vhm):
    """
    Idempotently create one Libvirt Virtual Host Manager from one entry of <prefix>_virtual_host_managers: {label, uri,
    sasl_username, sasl_password}. The module name is "Libvirt", and uri, sasl_username and sasl_password are its parameters.

    The gatherer appends "?no_tty=1" to the URI itself, so pass a bare libvirt URI, for example
    "qemu+ssh://root@nuc6.mydemo.lab/system". That form uses SSH key authentication, as the rest of this project does.

    The server requires every parameter of the module to be present and non-empty, even sasl_username and sasl_password, which a
    qemu+ssh:// URI does not use. When a value is not supplied, the function sends a placeholder non-empty string. The gatherer
    reads the SASL fields only for SASL-authenticating URI schemes.
    """
    label = vhm.get("label")
    if not label:
        die("virtual_host_managers: an entry is missing required 'label'")
    if virtual_host_manager_exists(hostname, exec_prefix, label):
        print("  Virtual Host Manager '{}' already exists — leaving it alone".format(label))
        return
    uri = vhm.get("uri")
    if not uri:
        die("virtual host manager '{}': 'uri' is required to create it".format(label))
    params = {
        "uri": uri,
        "sasl_username": vhm.get("sasl_username") or "n/a",
        "sasl_password": vhm.get("sasl_password") or "n/a",
    }
    r = _api_call(hostname, exec_prefix, "virtualhostmanager.create", [label, "Libvirt", params])
    if r.returncode != 0:
        die("could not create Virtual Host Manager '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Created Libvirt Virtual Host Manager '{}' ({})".format(label, uri))


def ensure_virtual_host_managers(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_virtual_host_managers: a list of dicts, dispatched
    on `type`: "aws" ({label, access_key_id, secret_access_key, region,
    zone} — see ensure_virtual_host_manager_aws()'s own docstring for why
    long-lived static credentials, not this project's usual
    resolve_cloud_account() SSO/STS mechanism, are what this needs) or
    "libvirt" ({label, uri, sasl_username, sasl_password} — see
    ensure_virtual_host_manager_libvirt()'s own docstring). No-op if the
    field is unset or empty.

    Credential resolution mirrors the rest of this project's own
    resolve_credential() convention (libs/addon_common.py) at the CALLER's
    level, not here — install_smlm.py resolves
    smlm_vhm_aws_access_key/smlm_vhm_aws_secret_key (directly, or via a
    named/auto-discovered 'aws' kind credentials file) BEFORE calling this,
    and fills them into each entry's access_key_id/secret_access_key here.
    This function itself only ever sees already-resolved plaintext values.
    """
    for vhm in cfg.get("{}_virtual_host_managers".format(prefix)) or []:
        vhm_type = vhm.get("type") or "aws"
        if vhm_type == "aws":
            ensure_virtual_host_manager_aws(hostname, exec_prefix, vhm)
        elif vhm_type == "libvirt":
            ensure_virtual_host_manager_libvirt(hostname, exec_prefix, vhm)
        else:
            die("virtual host manager '{}': type '{}' is not supported — only 'aws'/'libvirt' "
                "are currently implemented".format(vhm.get("label"), vhm_type))


def ensure_grafana_formula(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_grafana_formulas, which apply SUSE's grafana Salt formula to a target system through the server's
    built-in formula support. The formula installs and configures Grafana on the target. It adds a Prometheus data source, and when
    reportdb is enabled it creates a read-only reportdb Postgres user and the formula's own dashboards.

    This is separate from the standalone install_grafana.py and install_prometheus.py addons, which run podman containers and use
    no Salt formula. spacecmd has no subcommand for the formula namespace, so the calls go through the api passthrough. Each entry
    makes two calls: formula.setFormulasOfServer, which enables the formula, and formula.setSystemFormulaData, which configures it.
    Repeating the same configuration overwrites the data with the same values, so the call is safe to repeat.

    Each entry is {system, admin_user, admin_pass, prometheus: [{key, url, user, password}, ...], reportdb, is_hub, dashboards:
    {uyuni, uyuni_clients, postgresql, apache}}. Only "system" is required. Missing fields take the formula's own defaults:
    admin_user and admin_pass are "admin", prometheus points at http://localhost:9090, reportdb and is_hub are False, and the
    dashboards uyuni, uyuni_clients, postgresql and apache are True. The formula's Kubernetes and SAP dashboard toggles are left at
    their default of False.

    The target needs a monitoring add-on subscription, and Prometheus must already be installed on it. Grafana is not available on
    SMLM Proxy. The function does not check these conditions. The server's error reports a missing one.
    """
    entries = cfg.get("{}_grafana_formulas".format(prefix)) or []
    for entry in entries:
        system = entry.get("system")
        if not system:
            die("{}_grafana_formulas: an entry is missing required 'system'".format(prefix))

        sid = _system_id(hostname, exec_prefix, system)

        r = _api_call(hostname, exec_prefix, "formula.setFormulasOfServer", [sid, ["grafana"]])
        if r.returncode != 0:
            die("could not enable the 'grafana' formula on '{}': {}".format(
                system, (r.stderr or r.stdout or "").strip()))

        prometheus = entry.get("prometheus") or [{"key": "Prometheus", "url": "http://localhost:9090"}]
        dashboards = entry.get("dashboards") or {}
        content = {
            "grafana": {
                "enabled": True,
                "admin_user": entry.get("admin_user") or "admin",
                "admin_pass": entry.get("admin_pass") or "admin",
                "datasources": {
                    "prometheus": prometheus,
                    "reportdb": {
                        "enabled": bool(entry.get("reportdb")),
                        "is_hub": bool(entry.get("is_hub")),
                    },
                },
                "dashboards": {
                    "add_uyuni_dashboard": dashboards.get("uyuni", True),
                    "add_uyuni_clients_dashboard": dashboards.get("uyuni_clients", True),
                    "add_postgresql_dasboard": dashboards.get("postgresql", True),
                    "add_apache_dashboard": dashboards.get("apache", True),
                },
            },
        }
        r = _api_call(hostname, exec_prefix, "formula.setSystemFormulaData", [sid, "grafana", content])
        if r.returncode != 0:
            die("could not configure the 'grafana' formula on '{}': {}".format(
                system, (r.stderr or r.stdout or "").strip()))
        print("  Applied the 'grafana' formula to '{}'".format(system))


def ensure_environments(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_environments, a composition of the other features. Each entry is {label, system_group,
    activation_key, custom_info_tags: {k: v}, recurring_schedule: {...}}.

    `system_group` and `activation_key` are name references to entries defined elsewhere, in <prefix>_system_groups and
    <prefix>_activation_keys, or in the single top-level <prefix>_activation_key. The function does not create them. It links the
    existing group to the existing key with ensure_activation_key_groups(), and it applies custom_info_tags to every system in the
    group. The function is idempotent.

    recurring_schedule is not applied here. run_environment_schedules() applies it, through the install scripts'
    --run-recurring-schedules flag. The function does nothing if the field is unset or empty.
    """
    environments = cfg.get("{}_environments".format(prefix)) or []
    for env in environments:
        label = env.get("label")
        if not label:
            die("{}_environments: an entry is missing required 'label'".format(prefix))

        group_name = env.get("system_group")
        key_name = env.get("activation_key")
        if group_name and key_name:
            link_cfg = {"{}_activation_key".format(prefix): key_name,
                        "{}_activation_key_groups".format(prefix): group_name}
            ensure_activation_key_groups(hostname, exec_prefix, link_cfg, prefix)

        tags = env.get("custom_info_tags") or {}
        if tags and group_name:
            for system in list_group_systems(hostname, exec_prefix, group_name).splitlines():
                system = system.strip()
                if not system:
                    continue
                for key, value in tags.items():
                    ensure_system_tag(hostname, exec_prefix, system, key, str(value))


def run_environment_schedules(hostname, exec_prefix, cfg, prefix):
    """
    Apply the recurring_schedule of every <prefix>_environments entry. The install scripts run this through the
    --run-recurring-schedules flag. Each entry's system_group is resolved to a numeric group id with group_id_for(). An entry can
    set 'group_id' under recurring_schedule to skip that resolution. Each schedule is then created by ensure_recurring_schedule().
    `name` defaults to "<environment label>-recurring-schedule". The function does nothing if <prefix>_environments is unset or
    empty, or if no entry has a recurring_schedule.
    """
    environments = cfg.get("{}_environments".format(prefix)) or []
    for env in environments:
        sched = env.get("recurring_schedule")
        if not sched:
            continue
        label = env.get("label", "?")
        group_name = env.get("system_group")
        group_id = sched.get("group_id")
        if group_id is None:
            if not group_name:
                die("environment '{}': recurring_schedule needs 'system_group' or an explicit "
                    "'group_id'".format(label))
            group_id = group_id_for(hostname, exec_prefix, group_name)
            if group_id is None:
                die("environment '{}': could not resolve system group '{}' to a numeric id — "
                    "supply 'group_id' explicitly in recurring_schedule".format(label, group_name))
        cron = sched.get("cron")
        if not cron:
            die("environment '{}': recurring_schedule needs 'cron'".format(label))
        name = sched.get("name") or "{}-recurring-schedule".format(label)
        ensure_recurring_schedule(hostname, exec_prefix, "group", group_id, cron, name,
                                   schedule_type=sched.get("type") or "highstate",
                                   states=sched.get("states"), extra=sched.get("extra"))


# ── Client registration (Salt bootstrap) ───────────────────────────────────
#
# For install_client_registration.py — registers some OTHER host (not the
# server itself) as a Salt client. `hostname`/`exec_prefix` below are always
# the SERVER's (for spacecmd/saltkey calls); the client is addressed
# separately via ssh_run(client_hostname, ...) for the bootstrap curl, since
# it's just a plain SSH target, not something reached through exec_prefix.

def saltkey_pending(hostname, exec_prefix):
    """Raw stdout of saltkey.pendingList — minion IDs awaiting acceptance."""
    return (_api_call(hostname, exec_prefix, "saltkey.pendingList", []).stdout or "")


def saltkey_accepted(hostname, exec_prefix, minion_id):
    """Whether minion_id already appears in saltkey.acceptedList's output —
    i.e. this client is already a fully registered system."""
    r = _api_call(hostname, exec_prefix, "saltkey.acceptedList", [])
    return r.returncode == 0 and minion_id in (r.stdout or "")


def saltkey_accept(hostname, exec_prefix, minion_id):
    """Accept a pending minion key via saltkey.accept. Dies on failure."""
    r = _api_call(hostname, exec_prefix, "saltkey.accept", [minion_id])
    if r.returncode != 0:
        die("could not accept salt key for '{}': {}".format(minion_id, (r.stderr or r.stdout or "").strip()))


def _channel_package_nvr(hostname, exec_prefix, channel, pkg_name):
    """
    Return the exact NVR-EA string for `pkg_name` in `channel`, as in "openssl-1.0.2k-19.el7:1.x86_64". The package list is
    read from softwarechannel_listallpackages, which prints one NVR-EA per line. The match uses the package name only, up to the
    first '-' that starts a version number. This is enough for the few package names the module uses, and it is not a general NVR
    parser. Returns None if the package is not in the channel.
    """
    r = _spacecmd(hostname, exec_prefix, "softwarechannel_listallpackages {}".format(shlex.quote(channel)))
    prefix = pkg_name + "-"
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(prefix) and line[len(prefix):len(prefix) + 1].isdigit():
            return line
    return None


def _stage_channel_package_on_client(hostname, exec_prefix, channel, pkg_name, client_hostname, dest_dir):
    """
    Copy the RPM for `pkg_name` from the server's package store onto client_hostname, and return the path on the client. The
    RPM is found under /var/spacewalk/packages/ on the server, and it is copied as base64 over two separate SSH connections. No
    HTTP or HTTPS request is made, so the copy works when the client cannot reach the server's TLS endpoint. The server side uses
    exec_prefix, which works for both the podman and the Kubernetes deployment. The client side is a plain SSH command.

    Returns None if the package is not in `channel`, or if its file cannot be located or copied. The Kubernetes deployment mode has
    not been exercised.
    """
    nvr = _channel_package_nvr(hostname, exec_prefix, channel, pkg_name)
    if not nvr:
        return None
    # The NVR-EA can contain an optional ":<epoch>" between the release and the architecture, for example
    # "openssl-1.0.2k-19.el7:1.x86_64". The .rpm file name does not include the epoch.
    filename = re.sub(r":\d+\.", ".", nvr) + ".rpm"
    r = _run(hostname, exec_prefix, "find /var/spacewalk/packages -iname {}".format(shlex.quote(filename)),
             check=False, capture=True)
    paths = [p for p in (r.stdout or "").splitlines() if p.strip()]
    if not paths:
        return None
    r2 = _run(hostname, exec_prefix, "base64 {}".format(shlex.quote(paths[0])), check=False, capture=True)
    if r2.returncode != 0 or not (r2.stdout or "").strip():
        return None
    dest = "{}/{}".format(dest_dir, filename)
    r3 = ssh_run(client_hostname, "mkdir -p {} && base64 -d > {}".format(
        shlex.quote(dest_dir), shlex.quote(dest)), input_text=r2.stdout, check=False)
    if r3.returncode != 0:
        return None
    print("  Staged '{}' from channel '{}' onto '{}' (server TLS unreachable from this "
          "client — copied via SSH instead, see ensure_client_registered()'s own docstring)".format(
              filename, channel, client_hostname))
    return dest


def _try_wget_legacy_bootstrap(hostname, exec_prefix, client_hostname, server_fqdn, script_name,
                                env, base_channel):
    """
    Fallback for a client whose curl cannot negotiate TLS with this server. Some older distributions ship a curl that cannot
    use the server's TLS 1.2-only policy. The fallback changes only the client:

    1. It ensures wget is installed on the client, staging it from `base_channel` through
       _stage_channel_package_on_client() when it is missing. GNU Wget uses the system OpenSSL and negotiates the server's TLS. The
       bootstrap script prefers wget when both are present, so its own fetches also succeed.
    2. It runs the bootstrap through wget instead of curl.
    3. If package installation still fails, and the cause is an unresolved OpenSSL dependency (the salt minion package needs
       OPENSSL_1.0.2 symbols that an old openssl-libs does not provide), it stages openssl and openssl-libs from `base_channel`. It
       installs them with rpm -Uvh --force, in one transaction, and then retries the bootstrap once. yum's own downloader still uses
       the old libcurl, so the upgraded packages are installed first. The bootstrap then skips that yum step.

    The OpenSSL repair is attempted once. If it does not resolve the failure, the function reports the bootstrap output and stops.
    """
    have_wget = ssh_run(client_hostname, "command -v wget", check=False).returncode == 0
    if not have_wget:
        if not base_channel or not _stage_channel_package_on_client(
                hostname, exec_prefix, base_channel, "wget", client_hostname, "/tmp/.lab-legacy-tls"):
            return False
        r = ssh_run(client_hostname, "rpm -Uvh --force /tmp/.lab-legacy-tls/wget-*.rpm", check=False)
        if r.returncode != 0:
            return False

    bootstrap_url = "https://{}/pub/bootstrap/{}".format(server_fqdn, script_name)
    # Fetch-to-file-then-run rather than a `<(...)` process substitution —
    # that's a bashism, not guaranteed on every client's login shell; a
    # plain mktemp+run, same idiom as the curl-based bootstrap_cmd above,
    # works on any POSIX sh.
    wget_cmd = (
        "_tmp=$(mktemp)\n"
        "wget -qO \"$_tmp\" --no-check-certificate {url}\n"
        "{env} /bin/bash \"$_tmp\"\n"
        "_rc=$?\n"
        "rm -f \"$_tmp\"\n"
        "exit $_rc"
    ).format(url=shlex.quote(bootstrap_url), env=env)
    r = ssh_run(client_hostname, wget_cmd, check=False, capture=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "bootstrap complete" in out.lower():
        return True
    if "openssl" not in out.lower() or "failed to install" not in out.lower():
        return r.returncode == 0

    print("  '{}': bootstrap needs a newer OpenSSL than this client has — staging one from "
          "'{}' (see ensure_client_registered()'s own docstring)".format(client_hostname, base_channel))
    staged = []
    for pkg in ("openssl-libs", "openssl"):
        path = _stage_channel_package_on_client(
            hostname, exec_prefix, base_channel, pkg, client_hostname, "/tmp/.lab-legacy-tls")
        if path:
            staged.append(path)
    if not staged:
        return False
    r2 = ssh_run(client_hostname, "rpm -Uvh --force /tmp/.lab-legacy-tls/*.rpm", check=False)
    if r2.returncode != 0:
        return False

    r3 = ssh_run(client_hostname, wget_cmd, check=False, capture=True)
    out3 = (r3.stdout or "") + (r3.stderr or "")
    return r3.returncode == 0 and "bootstrap complete" in out3.lower()


def _ensure_client_can_resolve_server(client_hostname, server_fqdn, server_ip=None):
    """
    Push a static /etc/hosts entry for server_fqdn onto client_hostname. A client on a different network, such as an AWS VPC,
    may not be able to resolve the server's FQDN through its own DNS.

    The address is resolved on the automation node with socket.gethostbyname, because the automation node always has working DNS for
    the lab. The line "<ip> <fqdn>" is appended to the client's /etc/hosts only if it is not already there. If the FQDN cannot be
    resolved on the automation node either, the function does nothing, and the bootstrap then reports the resolution error.

    server_ip, if given, is used as the address instead of resolving server_fqdn. Use it when the name means something different on
    the automation node than on the client's network.
    """
    if not server_ip:
        try:
            server_ip = socket.gethostbyname(server_fqdn)
        except socket.gaierror:
            return
    hosts_line = "{} {}".format(server_ip, server_fqdn)
    ssh_run(client_hostname,
            "grep -qF {line} /etc/hosts || echo {line} >> /etc/hosts".format(
                line=shlex.quote(hosts_line)),
            check=False)


def ensure_client_registered(hostname, exec_prefix, client_hostname, server_fqdn, activation_key,
                              reactivation_key=None, retry_limit=30, retry_interval=10, base_channel=None,
                              profile_name=None, server_ip=None):
    """
    Register client_hostname as a Salt client of the Uyuni or SMLM server reached through (hostname, exec_prefix). The
    registration uses an activation key that the caller has already created with ensure_activation_key(). The function is
    idempotent: it does nothing when the client's key is already accepted. The minion ID is client_hostname, unless profile_name is
    given.

    The steps are:
      1. Generate a bootstrap script for this activation key on the server, with mgr-bootstrap --activation-keys=<key>. The file
         /pub/bootstrap/bootstrap.sh does not exist until a script is generated. A fetch from a fresh server returns the login page
         instead. The script is named after the activation key, so several keys do not overwrite each other's script.
      2. Run the script on the client over SSH. The client is a plain SSH target, not part of the server's exec_prefix.
      3. Poll the server's pending-key list until the minion ID appears, up to retry_limit attempts.
      4. Accept the key.

    base_channel, optional, names the activation key's base software channel. Packages for the TLS fallback are staged from it, as
    described in _try_wget_legacy_bootstrap(). Without it, that fallback is skipped.

    profile_name, optional, is the system's name on the server. It is passed to the bootstrap script as PROFILENAME, which sets it
    as the minion ID. The key checks then look for that ID, while SSH still targets client_hostname.

    A client on another network may not resolve server_fqdn through its own DNS. The function therefore first writes a static
    /etc/hosts entry for the server on the client, through _ensure_client_can_resolve_server(), before any bootstrap attempt. That
    covers the script fetch, the checks inside the bootstrap, and the later salt connection.
    """
    minion_id = profile_name or client_hostname
    if saltkey_accepted(hostname, exec_prefix, minion_id):
        print("  '{}' is already a registered client — leaving it alone".format(client_hostname))
        return

    _ensure_client_can_resolve_server(client_hostname, server_fqdn, server_ip)

    # Resolve to Uyuni's real, org-id-prefixed key name (see
    # resolve_activation_key_name's docstring) — both mgr-bootstrap and the
    # client's own ACTIVATION_KEYS env var need an exact match.
    activation_key = resolve_activation_key_name(hostname, exec_prefix, activation_key)
    script_name = "{}.sh".format(re.sub(r"[^A-Za-z0-9_.-]", "_", activation_key))
    print("  Generating the bootstrap script for activation key '{}'".format(activation_key))
    r = _run(hostname, exec_prefix,
             "mgr-bootstrap --activation-keys={} --script={}".format(
                 shlex.quote(activation_key), shlex.quote(script_name)),
             check=False, capture=True)
    if r.returncode != 0:
        die("could not generate the bootstrap script on '{}' for key '{}': {}".format(
            hostname, activation_key, (r.stderr or r.stdout or "").strip()))

    print("  Bootstrapping '{}' against '{}'".format(client_hostname, server_fqdn))
    env = "ACTIVATION_KEYS={}".format(shlex.quote(activation_key))
    if profile_name:
        env += " PROFILENAME={}".format(shlex.quote(profile_name))
    if reactivation_key:
        env += " REACTIVATION_KEY={}".format(shlex.quote(reactivation_key))
    # On an old client, a plain `curl -Sks <url> | /bin/bash` can fail with no server-side change able to fix it. CentOS 7's
    # curl links NSS, which cannot negotiate the server's TLS 1.2 policy. The pipeline still exits 0, so the failure is silent
    # and the later salt-key poll times out. The system OpenSSL can negotiate the same endpoint. Python's ssl module always uses
    # the system OpenSSL, so the fallback fetches the script with Python. It tries python3 first, then python2. Certificates are
    # not verified, matching curl -k.
    bootstrap_url = "https://{}/pub/bootstrap/{}".format(server_fqdn, script_name)
    bootstrap_cmd = (
        "_url={url}\n"
        "_tmp=$(mktemp)\n"
        "if ! curl -Sks \"$_url\" -o \"$_tmp\" 2>/tmp/.lab-bootstrap-curl-err; then\n"
        "  python3 -c \"import ssl,urllib.request,sys; ctx=ssl._create_unverified_context(); "
        "open(sys.argv[1],'wb').write(urllib.request.urlopen(sys.argv[2], context=ctx).read())\" "
        "\"$_tmp\" \"$_url\" 2>/dev/null || \\\n"
        "  python2 -c \"import urllib2,sys; open(sys.argv[1],'wb').write(urllib2.urlopen(sys.argv[2]).read())\" "
        "\"$_tmp\" \"$_url\" 2>/dev/null || {{\n"
        "    echo 'bootstrap: curl failed and no working python3/python2 HTTPS fallback found "
        "(see /tmp/.lab-bootstrap-curl-err for curl'\"'\"'s own error)' >&2\n"
        "    cat /tmp/.lab-bootstrap-curl-err >&2\n"
        "    exit 1\n"
        "  }}\n"
        "fi\n"
        "{env} /bin/bash \"$_tmp\"\n"
        "_rc=$?\n"
        "rm -f \"$_tmp\" /tmp/.lab-bootstrap-curl-err\n"
        "exit $_rc"
    ).format(url=shlex.quote(bootstrap_url), env=env)
    r = ssh_run(client_hostname, bootstrap_cmd, check=False, capture=True)
    bootstrap_out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        die("bootstrap script failed on '{}' (rc={})".format(client_hostname, r.returncode))

    def _wait_for_pending_key():
        for _ in range(retry_limit):
            if minion_id in saltkey_pending(hostname, exec_prefix):
                return True
            if saltkey_accepted(hostname, exec_prefix, minion_id):
                return None  # already accepted by something else while polling
            time.sleep(retry_interval)
        return False

    print("  Waiting for '{}''s salt key to appear …".format(client_hostname))
    appeared = _wait_for_pending_key()
    if appeared is None:
        print("  '{}' is already accepted".format(client_hostname))
        return
    if not appeared:
        # See _try_wget_legacy_bootstrap(). A curl TLS failure exits the pipeline as if nothing went wrong, so the only symptom
        # is the timeout. The fallback is cheap, and it does nothing when the client does not need it.
        if base_channel and _try_wget_legacy_bootstrap(
                hostname, exec_prefix, client_hostname, server_fqdn, script_name, env, base_channel):
            appeared = _wait_for_pending_key()
            if appeared is None:
                print("  '{}' is already accepted".format(client_hostname))
                return
        if not appeared:
            die("'{}''s salt key never appeared as pending after bootstrap ({}s), including "
                "after the legacy-TLS-client recovery attempt — check connectivity to {}:4505/4506 "
                "and the bootstrap script's own output: {}".format(
                    client_hostname, retry_limit * retry_interval, server_fqdn,
                    bootstrap_out[-500:] if bootstrap_out else "(no output captured)"))

    saltkey_accept(hostname, exec_prefix, minion_id)
    print("  Accepted salt key for '{}'".format(client_hostname))


# ── Config export: read a live server back into lab-in-a-box JSON ──────────
# These are the reverse of the ensure_* functions. They run read-only calls and parse the results into the <prefix>_* shapes
# that the ensure_* functions consume, so the output can be pasted into a lab JSON file.
# Limits:
#   - Passwords are stored as one-way hashes. A user or org admin imported this way needs its password filled in by hand.
#   - A custom access group's member list is queryable only for the organization of the calling session. The users field is
#     therefore exported only for that organization.

def describe_activation_key(hostname, exec_prefix, key_name, prefix):
    """
    Read one activation key's configuration with activationkey_details and return it as a dict, using the same
    <prefix>_activation_key* field names that ensure_activation_key() reads. The result is a valid entry for
    <prefix>_activation_keys.

    key_name is the key's label as the server stores it, including the numeric organization prefix, for example "1-sles15sp7". The
    returned value has that prefix removed, because the prefix is applied by the server at creation time and is not part of the
    lab JSON.
    """
    text = _spacecmd(hostname, exec_prefix, "activationkey_details {}".format(
        shlex.quote(key_name))).stdout or ""

    def field(label):
        m = re.search(r"^{}:\s*(.*)$".format(re.escape(label)), text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    def section(header):
        # The section ends at the next header, a line followed by a dashes-only line, or at the end of the string. It does not
        # end at a blank line, because an empty section is followed by only one blank line.
        m = re.search(r"^{}\n-+\n(.*?)(?=\n[A-Za-z][^\n]*\n-+\n|\Z)".format(re.escape(header)),
                       text, re.MULTILINE | re.DOTALL)
        return [line.strip() for line in (m.group(1).splitlines() if m else []) if line.strip()]

    channel_lines = section("Software Channels")
    base_channel = channel_lines[0].lstrip("|- ").strip() if channel_lines else ""
    child_channels = [line.lstrip("|- ").strip() for line in channel_lines[1:]]

    entry = {
        "{}_activation_key".format(prefix): re.sub(r"^\d+-", "", key_name),
        "{}_activation_key_desc".format(prefix): field("Description"),
        "{}_activation_key_base_channel".format(prefix): base_channel,
    }
    if child_channels:
        entry["{}_activation_key_child_channels".format(prefix)] = " ".join(child_channels)
    groups = section("System Groups")
    if groups:
        entry["{}_activation_key_groups".format(prefix)] = " ".join(groups)
    config_channels = section("Configuration Channels")
    if config_channels:
        entry["{}_activation_key_config_channels".format(prefix)] = " ".join(config_channels)
    entitlements = field("Entitlements") or ",".join(section("Entitlements"))
    if entitlements:
        entry["{}_activation_key_entitlements".format(prefix)] = entitlements
    packages = section("Packages")
    if packages:
        entry["{}_activation_key_packages".format(prefix)] = " ".join(packages)
    if (field("Universal Default") or "").strip().lower() == "true":
        entry["{}_activation_key_universal_default".format(prefix)] = "true"
    contact_method = field("Contact Method")
    if contact_method and contact_method != "default":
        entry["{}_activation_key_contact_method".format(prefix)] = contact_method
    return entry


def describe_system_group(hostname, exec_prefix, name):
    """
    Reads one system group's live members via spacecmd's native
    group_details, and returns a dict matching one <prefix>_system_groups
    entry: {"name": ..., "description": ..., "systems": [...]}.
    """
    text = _spacecmd(hostname, exec_prefix, "group_details {}".format(shlex.quote(name))).stdout or ""
    m = re.search(r"^Description:\s*(.*)$", text, re.MULTILINE)
    description = m.group(1).strip() if m else name
    m = re.search(r"^Members\n-+\n(.*?)\Z", text, re.MULTILINE | re.DOTALL)
    systems = [line.strip() for line in (m.group(1).splitlines() if m else []) if line.strip()]
    entry = {"name": name, "description": description}
    if systems:
        entry["systems"] = systems
    return entry


def describe_access_groups(hostname, exec_prefix):
    """
    Reads every custom access group VISIBLE TO THE CURRENT SESSION'S OWN
    ORG via access.listRoles + access.listPermissions, and returns a list
    of <prefix>_access_groups entries: {"label", "description",
    "permissions": [{"namespace", "mode"}]}. No `users` field — see this
    module's own top-of-section note on why that can't be recovered for
    any org other than the caller's own, and even for the caller's own org
    there's no API to map a namespace-permission grant back to the
    individual users holding that role (only the reverse: user -> roles,
    itself org-scoped and, for custom labels specifically, further gated by
    the same real getAssignableRoles restriction ensure_user_role()'s
    docstring documents). Callers wanting `users` populated must add it by
    hand.
    """
    r = _api_call(hostname, exec_prefix, "access.listRoles", [])
    try:
        roles = json.loads(r.stdout or "[]")
    except (ValueError, TypeError):
        roles = []

    groups = []
    for role in roles:
        label = role.get("label")
        if not label:
            continue
        perms_r = _api_call(hostname, exec_prefix, "access.listPermissions", [label])
        try:
            perms = json.loads(perms_r.stdout or "[]")
        except (ValueError, TypeError):
            perms = []
        permissions = [
            {"namespace": p["namespace"], "mode": (p.get("access_mode") or {}).get("value", "R")}
            for p in perms if p.get("namespace")
        ]
        entry = {"label": label, "description": role.get("description") or label}
        if permissions:
            entry["permissions"] = permissions
        groups.append(entry)
    return groups


def export_config(hostname, exec_prefix, admin, password, prefix):
    """
    Reads a live server's current configuration back into a dict shaped
    exactly like a lab JSON's "smlm"/"uyuni" top-level section (same
    <prefix>_* field names ensure_channels_synced/ensure_activation_keys/
    ensure_system_groups/ensure_access_groups/ensure_orgs already consume)
    — the reverse of every ensure_* function in this module. Read-only:
    issues no write calls at all.

    Covers: every software channel currently on the server
    (<prefix>_channels), every activation key with its full detail
    (<prefix>_activation_keys, via describe_activation_key), every system
    group with its current members (<prefix>_system_groups, via
    describe_system_group), the CURRENT org's own custom access groups
    (<prefix>_access_groups, via describe_access_groups — see its own
    docstring for the real org-scoping limit), and every OTHER org
    (<prefix>_orgs) with its own username list (org_listusers, confirmed
    live to work cross-org even though user.getDetails does not) — each
    flagged with an "_export_note" key (not a real schema field — strip it
    before use) since admin_pass/password/first_name/last_name/email can
    never be recovered from a live server (passwords are one-way hashed)
    and must be filled in by hand before this is usable to actually
    recreate that org/its users elsewhere.
    """
    ensure_spacecmd_config(hostname, exec_prefix, admin, password)

    channels = [line.strip() for line in
                (_spacecmd(hostname, exec_prefix, "softwarechannel_list").stdout or "").splitlines()
                if line.strip()]

    key_names = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "activationkey_list").stdout or "").splitlines()
                 if line.strip()]
    activation_keys = [describe_activation_key(hostname, exec_prefix, k, prefix) for k in key_names]

    group_names = [line.strip() for line in
                   (_spacecmd(hostname, exec_prefix, "group_list").stdout or "").splitlines()
                   if line.strip()]
    system_groups = [describe_system_group(hostname, exec_prefix, g) for g in group_names]

    access_groups = describe_access_groups(hostname, exec_prefix)

    org_names = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "org_list").stdout or "").splitlines()
                 if line.strip()]
    details = _spacecmd(hostname, exec_prefix, "user_details {}".format(shlex.quote(admin))).stdout or ""
    m = re.search(r"^Organisation:\s*(.*)$", details, re.MULTILINE)
    own_org = m.group(1).strip() if m else None

    orgs = []
    for org_name in org_names:
        if org_name == own_org:
            continue
        users = [line.strip() for line in
                 (_spacecmd(hostname, exec_prefix, "org_listusers {}".format(shlex.quote(org_name))).stdout
                  or "").splitlines() if line.strip()]
        orgs.append({
            "name": org_name,
            "_export_note": "admin_user/admin_pass/admin_email cannot be recovered from a live "
                             "server (passwords are one-way hashed) — fill these in by hand before "
                             "this org can be recreated elsewhere. 'existing_users' below is a "
                             "best-effort username list (org_listusers); none of their own "
                             "password/first_name/last_name/email could be recovered either "
                             "(user.getDetails is hard org-scoped, even for a satellite_admin) — "
                             "use it as a checklist, not a ready-to-use {}_users list.".format(prefix),
            "existing_users": users,
        })

    result = {
        "{}_admin".format(prefix): admin,
        "{}_org".format(prefix): own_org or "",
        "{}_channels".format(prefix): channels,
    }
    if activation_keys:
        result["{}_activation_keys".format(prefix)] = activation_keys
    if system_groups:
        result["{}_system_groups".format(prefix)] = system_groups
    if access_groups:
        result["{}_access_groups".format(prefix)] = access_groups
    if orgs:
        result["{}_orgs".format(prefix)] = orgs
    return result


# ─── Maintenance windows (calendars and schedules) ────────────────────────────
# The calls go through the api passthrough, against the maintenance.* namespace. spacecmd has no native subcommand for it.

def ensure_maintenance_calendar(hostname, exec_prefix, label, ical=None, url=None):
    """
    Idempotently creates a Maintenance Calendar via maintenance.createCalendar
    (raw ICal text) or maintenance.createCalendarWithUrl (a URL Uyuni
    downloads ICal data from) — exactly one of `ical`/`url` must be given.
    Skipped if `label` already appears in maintenance.listCalendarLabels'
    output (no separate "exists" check in the real API) — deliberately
    create-only, never touches an already-existing calendar's own content;
    see sync_maintenance_calendar() below for the update-aware version
    ensure_maintenance_calendars()'s own orchestrator actually uses.
    """
    if bool(ical) == bool(url):
        die("maintenance calendar '{}': exactly one of 'ical' or 'url' is required".format(label))
    r = _api_call(hostname, exec_prefix, "maintenance.listCalendarLabels", [])
    if r.returncode == 0 and label in (r.stdout or ""):
        print("  Maintenance calendar '{}' already exists — leaving it alone".format(label))
        return
    method = "maintenance.createCalendar" if ical else "maintenance.createCalendarWithUrl"
    r = _api_call(hostname, exec_prefix, method, [label, ical or url])
    if r.returncode != 0:
        die("could not create maintenance calendar '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Created maintenance calendar '{}'".format(label))


def maintenance_calendar_details(hostname, exec_prefix, label):
    """
    Return the maintenance.getCalendarDetails struct for `label`: id, orgId, label and ical, plus url if the calendar was
    created from one. Returns None if the calendar does not exist. The server then raises an error, which surfaces here as a
    non-zero return code. sync_maintenance_calendar() uses the result to decide between create, update and no change.

    The server's JSON response for this call is a one-element array that wraps the struct. The function unwraps it, so callers
    get the plain struct.
    """
    r = _api_call(hostname, exec_prefix, "maintenance.getCalendarDetails", [label])
    if r.returncode != 0:
        return None
    try:
        parsed = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, list):
        return parsed[0] if parsed else None
    return parsed


def update_maintenance_calendar(hostname, exec_prefix, label, ical=None, url=None, reschedule_strategy=None):
    """
    Updates an ALREADY-EXISTING Maintenance Calendar's ical/url content via
    the real maintenance.updateCalendar(sessionKey, label, {ical|url},
    rescheduleStrategy), implemented in MaintenanceHandler.java. This is the one thing
    ensure_maintenance_calendar() above
    deliberately never does. `reschedule_strategy` (a list of strings, the
    real API's own type) defaults to ["Fail"] — its own documented safer
    option: real confirmed values are "Cancel" (cancels any already-
    scheduled action that falls outside the new calendar's windows) or
    "Fail" (refuses the whole update instead, leaving the calendar
    untouched, if such a conflict exists) — this never silently cancels a
    scheduled action unless the caller explicitly opts into "Cancel".
    """
    if bool(ical) == bool(url):
        die("maintenance calendar '{}': exactly one of 'ical' or 'url' is required".format(label))
    details = {"ical": ical} if ical else {"url": url}
    strategy = reschedule_strategy or ["Fail"]
    r = _api_call(hostname, exec_prefix, "maintenance.updateCalendar", [label, details, strategy])
    if r.returncode != 0:
        die("could not update maintenance calendar '{}': {}".format(
            label, (r.stderr or r.stdout or "").strip()))
    print("  Updated maintenance calendar '{}'".format(label))


def sync_maintenance_calendar(hostname, exec_prefix, label, ical=None, url=None, reschedule_strategy=None):
    """
    Ensures maintenance calendar `label` exists AND its content matches
    `ical`/`url` — creates it (ensure_maintenance_calendar()) if missing,
    updates it (update_maintenance_calendar()) if it exists with DIFFERENT
    content (compared via a real maintenance.getCalendarDetails lookup, not
    blindly re-pushed every run), or does nothing if content already
    matches. This is what ensure_maintenance_calendars()'s own orchestrator
    actually calls — makes the lab-JSON file a genuine source of truth for
    an existing calendar's schedule, not just its initial creation.
    """
    if bool(ical) == bool(url):
        die("maintenance calendar '{}': exactly one of 'ical' or 'url' is required".format(label))
    existing = maintenance_calendar_details(hostname, exec_prefix, label)
    if existing is None:
        ensure_maintenance_calendar(hostname, exec_prefix, label, ical=ical, url=url)
        return
    current = existing.get("ical") if ical else existing.get("url")
    desired = ical or url
    # ical text is compared after .rstrip(). The server strips trailing whitespace from stored ical, so a value that ends in a
    # newline would never compare equal and would trigger an update on every run. Trailing whitespace has no meaning in iCalendar.
    if (current or "").rstrip() == (desired or "").rstrip():
        print("  Maintenance calendar '{}' already has the desired content — leaving it alone".format(label))
        return
    update_maintenance_calendar(hostname, exec_prefix, label, ical=ical, url=url,
                                reschedule_strategy=reschedule_strategy)


def ensure_maintenance_calendars(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_maintenance_calendars, a list of {label, ical, url} dicts. Each calendar is synced with
    sync_maintenance_calendar(), so a change to an existing calendar's schedule in the lab JSON reaches the server on the next
    run. Calendars are not only created.
    """
    for entry in cfg.get("{}_maintenance_calendars".format(prefix)) or []:
        label = entry.get("label")
        if not label:
            die("{}_maintenance_calendars: an entry is missing 'label'".format(prefix))
        sync_maintenance_calendar(hostname, exec_prefix, label,
                                  ical=entry.get("ical"), url=entry.get("url"))


def ensure_maintenance_schedule(hostname, exec_prefix, name, schedule_type, calendar=None):
    """
    Idempotently creates a Maintenance Schedule via maintenance.createSchedule
    (3-arg form if `calendar` is omitted, 4-arg form — real, separate
    overload, confirmed in the same source — if given). `schedule_type` is
    "single" or "multi" (Uyuni's own real ScheduleType labels). Skipped if
    `name` already appears in maintenance.listScheduleNames' output.
    """
    if schedule_type not in ("single", "multi"):
        die("maintenance schedule '{}': invalid schedule_type '{}' (expected single/multi)".format(
            name, schedule_type))
    r = _api_call(hostname, exec_prefix, "maintenance.listScheduleNames", [])
    if r.returncode == 0 and name in (r.stdout or ""):
        print("  Maintenance schedule '{}' already exists — leaving it alone".format(name))
        return
    args = [name, schedule_type, calendar] if calendar else [name, schedule_type]
    r = _api_call(hostname, exec_prefix, "maintenance.createSchedule", args)
    if r.returncode != 0:
        die("could not create maintenance schedule '{}': {}".format(
            name, (r.stderr or r.stdout or "").strip()))
    print("  Created maintenance schedule '{}' (type: {}{})".format(
        name, schedule_type, ", calendar: {}".format(calendar) if calendar else ""))


def ensure_maintenance_schedule_systems(hostname, exec_prefix, schedule_name, systems):
    """
    Assigns a maintenance schedule to systems via maintenance.assignScheduleToSystems.
    NOT idempotency-checked per-system (the real API has no
    "list systems already on this schedule minus these" diff — reassigning
    an already-assigned system is harmless, confirmed by the method's own
    apidoc: it just re-associates). `systems` are hostnames, resolved to
    sids via _system_id(). rescheduleStrategy is hardcoded to ["Cancel"] —
    the real method requires a non-null list and "Cancel" (cancel actions
    outside the new maintenance windows) is the safer of the two documented
    options for a freshly-assigned schedule with no windows defined yet.
    """
    sids = [_system_id(hostname, exec_prefix, s) for s in systems]
    r = _api_call(hostname, exec_prefix, "maintenance.assignScheduleToSystems",
                  [schedule_name, sids, ["Cancel"]])
    if r.returncode != 0:
        die("could not assign maintenance schedule '{}' to {}: {}".format(
            schedule_name, systems, (r.stderr or r.stdout or "").strip()))
    print("  Assigned maintenance schedule '{}' to: {}".format(schedule_name, ", ".join(systems)))


def ensure_maintenance_schedules(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_maintenance_schedules: a list of
    {name, type, calendar, systems: [...]} dicts. `systems`, if given,
    assigns the schedule to those systems right after creating it.
    """
    for entry in cfg.get("{}_maintenance_schedules".format(prefix)) or []:
        name = entry.get("name")
        schedule_type = entry.get("type")
        if not (name and schedule_type):
            die("{}_maintenance_schedules: an entry is missing 'name' or 'type'".format(prefix))
        ensure_maintenance_schedule(hostname, exec_prefix, name, schedule_type,
                                     calendar=entry.get("calendar"))
        systems = entry.get("systems") or []
        if systems:
            ensure_maintenance_schedule_systems(hostname, exec_prefix, name, systems)


# ─── Action chains ───────────────────────────────────────────────────────────
# No spacecmd-native subcommand exists for this namespace. The calls go through the api passthrough.

def ensure_action_chain(hostname, exec_prefix, label, actions):
    """
    Idempotently creates an Action Chain (actionchain.createChain) and adds
    each of `actions` to it (each: {"system": "...", "type": "script"|
    "highstate", "script": "..." (required for type=script, run as
    root:root with a 300s timeout — a fixed, harmless default, not
    per-action configurable here)}). Skipped (entirely, actions included)
    if `label` already appears in actionchain.listChains' output — the
    real API has no per-action idempotency check, and this project's own
    established convention is to treat a whole multi-step construct as one
    unit once its own label already exists, same as ensure_kickstart_profile
    et al.

    Deliberately does NOT call actionchain.scheduleChain — adding an action
    to a chain already creates a real, concrete scheduled Action row
    server-side (confirmed in the Java source: addScriptRun immediately
    calls ActionChainManager.scheduleScriptRuns), but the chain itself stays
    in "pending, unscheduled" state (no execution date set) until
    scheduleChain runs — matching this project's own established pattern of
    creating one-shot/example objects without triggering them (kickstart
    profiles, image imports) rather than executing them automatically.
    """
    r = _api_call(hostname, exec_prefix, "actionchain.listChains", [])
    if r.returncode == 0 and label in (r.stdout or ""):
        print("  Action chain '{}' already exists — leaving it alone".format(label))
        return
    r = _api_call(hostname, exec_prefix, "actionchain.createChain", [label])
    if r.returncode != 0:
        die("could not create action chain '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))

    for action in actions:
        system = action.get("system")
        atype = action.get("type", "script")
        if not system:
            die("action chain '{}': an action is missing 'system'".format(label))
        sid = _system_id(hostname, exec_prefix, system)
        if atype == "script":
            script = action.get("script")
            if not script:
                die("action chain '{}': a 'script' action on '{}' is missing 'script'".format(
                    label, system))
            body_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
            r = _api_call(hostname, exec_prefix, "actionchain.addScriptRun",
                          [sid, label, "root", "root", 300, body_b64])
        elif atype == "highstate":
            r = _api_call(hostname, exec_prefix, "actionchain.addApplyHighstate", [sid, label])
        else:
            die("action chain '{}': invalid action type '{}' (expected script/highstate)".format(
                label, atype))
        if r.returncode != 0:
            die("could not add a '{}' action for '{}' to action chain '{}': {}".format(
                atype, system, label, (r.stderr or r.stdout or "").strip()))
    print("  Created action chain '{}' with {} action(s), unscheduled".format(label, len(actions)))


def ensure_action_chains(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_action_chains: a list of {label, actions: [...]} dicts."""
    for entry in cfg.get("{}_action_chains".format(prefix)) or []:
        label = entry.get("label")
        actions = entry.get("actions") or []
        if not label or not actions:
            die("{}_action_chains: an entry needs 'label' and a non-empty 'actions' list".format(prefix))
        ensure_action_chain(hostname, exec_prefix, label, actions)


# ─── Custom software channels, packages and patches ─────────────────────────
# Package upload uses the rhnpush client tool, which speaks its own multipart protocol, so there is no plain XML-RPC call
# for it. rhnpush is installed in the uyuni-server container.

def ensure_custom_channel(hostname, exec_prefix, label, name, summary, arch_label,
                           parent_label="", checksum_type="sha256"):
    """
    Idempotently creates a custom software channel via channel.software.create.
    `parent_label` empty means a new BASE channel; a real existing base
    channel's label makes this a CHILD channel. gpgCheck is hardcoded False
    (no GPG key management here — this project builds+pushes its own
    packages, not a real vendor-signed repo) via the real 8-arg overload
    that takes an explicit gpgCheck bool (confirmed present, distinct from
    the 7-arg overload that defaults gpgCheck to true).
    """
    if channel_exists(hostname, exec_prefix, label):
        print("  Custom channel '{}' already exists — leaving it alone".format(label))
        return
    r = _api_call(hostname, exec_prefix, "channel.software.create",
                  [label, name, summary, arch_label, parent_label, checksum_type, {}, False])
    if r.returncode != 0:
        die("could not create custom channel '{}': {}".format(label, (r.stderr or r.stdout or "").strip()))
    print("  Created custom channel '{}' ({})".format(label, name))


def channel_exists(hostname, exec_prefix, label):
    """Whether `label` appears in softwarechannel_list's output."""
    r = _spacecmd(hostname, exec_prefix, "softwarechannel_list")
    return r.returncode == 0 and label in (r.stdout or "")


def channel_package_exists(hostname, exec_prefix, channel_label, package_name):
    """Whether `package_name` appears in softwarechannel_listallpackages <channel_label>'s output."""
    r = _spacecmd(hostname, exec_prefix,
                  "softwarechannel_listallpackages {}".format(shlex.quote(channel_label)))
    return r.returncode == 0 and package_name in (r.stdout or "")


def ensure_rhnpush_available(hostname, exec_prefix):
    """
    Ensures the real rhnpush client is on PATH inside exec_prefix's target,
    installing it (package name 'rhnpush', confirmed real on SUSE builds —
    client/tools/mgr-push in the real source) via zypper if missing.
    Returns True if usable, False (with a warn(), not die() — this is a
    genuinely optional capability, same "don't hard-fail the whole run over
    one optional tool" reasoning as helm/kubectl elsewhere in this project)
    otherwise.
    """
    r = _run(hostname, exec_prefix, "command -v rhnpush", check=False, capture=True)
    if r.returncode == 0:
        return True
    r = _run(hostname, exec_prefix, "zypper --non-interactive install rhnpush", check=False, capture=True)
    if r.returncode != 0:
        warn("rhnpush is not available and could not be installed — skipping package push "
             "({})".format((r.stderr or r.stdout or "").strip()[:200]))
        return False
    return True


def ensure_channel_package(hostname, exec_prefix, channel_label, local_rpm_path, username, password):
    """
    Idempotently pushes one RPM, at `local_rpm_path` on THIS machine (the
    automation node running install_smlm.py — e.g. this project's own
    packaging/nfpm.yaml build output), into `channel_label` via rhnpush on
    the server. Only supports a podman-deployed target (needs direct
    host+podman access to move a binary file all the way into the
    container) — same restriction, and same reasoning, as
    ensure_mcp_server(); callers check <prefix>_deployment before calling
    this, matching that function's own convention.

    Staging is real scp (binary-safe — ssh_run()'s own input_text path
    uses text-mode subprocess, which would corrupt an RPM) onto the host's
    /tmp, then `podman cp` from there into the uyuni-server container's
    /tmp — mgrctl itself has no file-copy subcommand, only exec. Skips
    (heuristically, by package NAME parsed from the filename, not full
    NEVRA) if channel_package_exists() already sees a same-named package
    in the channel — rhnpush itself has no separate "already pushed"
    check exposed as a clean idempotent flag. Talks to localhost's own
    HTTPS API (--server=localhost, matching ensure_spacecmd_config's own
    "always localhost, never the external FQDN" reasoning, since this now
    runs INSIDE the same container as that API). Credentials passed via
    --username/--password on argv IS how rhnpush's own CLI is designed (no
    config-file/session alternative it honors non-interactively) —
    accepted here as this tool's real, unavoidable interface, unlike
    spacecmd's own config-file-based avoidance elsewhere.
    """
    filename = Path(local_rpm_path).name
    pkg_name = re.sub(r"-[0-9][^-]*-[0-9][^-]*\.[a-z0-9_]+\.rpm$", "", filename)
    if channel_package_exists(hostname, exec_prefix, channel_label, pkg_name):
        print("  Package '{}' already in channel '{}' — leaving it alone".format(pkg_name, channel_label))
        return
    if not ensure_rhnpush_available(hostname, exec_prefix):
        return

    remote_tmp = "/tmp/{}".format(filename)
    r = scp_to(hostname, local_rpm_path, remote_tmp)
    if r.returncode != 0:
        die("could not copy '{}' to '{}': {}".format(
            local_rpm_path, hostname, (r.stderr or r.stdout or "").strip()))
    r = ssh_run(hostname, "podman cp {} uyuni-server:{}".format(
        shlex.quote(remote_tmp), shlex.quote(remote_tmp)), check=False, capture=True)
    if r.returncode != 0:
        die("could not copy '{}' into the uyuni-server container: {}".format(
            filename, (r.stderr or r.stdout or "").strip()))

    r = _run(hostname, exec_prefix,
             "rhnpush --server=localhost --channel={} --username={} --password={} "
             "--nosig {}".format(shlex.quote(channel_label), shlex.quote(username),
                                  shlex.quote(password), shlex.quote(remote_tmp)),
             check=False, capture=True)
    if r.returncode != 0:
        die("could not push '{}' into channel '{}': {}".format(
            filename, channel_label, (r.stderr or r.stdout or "").strip()))
    print("  Pushed '{}' into channel '{}'".format(filename, channel_label))


def ensure_custom_channels(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_custom_channels: a list of {label, name, summary,
    arch_label, parent_label, checksum_type, packages: [local RPM paths]}
    dicts. Only supports a podman-deployed target — see
    ensure_channel_package()'s own docstring for why; warns and skips
    package pushes (channel creation itself still runs — that part IS
    deployment-agnostic, plain channel.software.create) for any other
    <prefix>_deployment.
    """
    entries = cfg.get("{}_custom_channels".format(prefix)) or []
    if not entries:
        return
    deployment = cfg.get("{}_deployment".format(prefix)) or "kubernetes"
    admin = cfg.get("{}_admin_user".format(prefix)) or "admin"
    password = cfg.get("{}_admin_pass".format(prefix)) or "Smlm12345"

    for entry in entries:
        label = entry.get("label")
        if not label:
            die("{}_custom_channels: an entry is missing 'label'".format(prefix))
        ensure_custom_channel(hostname, exec_prefix, label,
                               entry.get("name") or label, entry.get("summary") or label,
                               entry.get("arch_label", "channel-x86_64"),
                               entry.get("parent_label", ""),
                               entry.get("checksum_type", "sha256"))
        packages = entry.get("packages") or []
        if packages and deployment != "podman":
            warn("{0}_custom_channels: channel '{1}' has packages to push but {0}_deployment is "
                 "'{2}' — pushing a package needs direct SSH+podman host access; skipping the "
                 "push (the channel itself was still created/left alone)".format(prefix, label, deployment))
            continue
        for local_rpm_path in packages:
            ensure_channel_package(hostname, exec_prefix, label, local_rpm_path, admin, password)


def package_id_for(hostname, exec_prefix, channel_label, package_name):
    """
    Best-effort numeric package id lookup via spacecmd's
    softwarechannel_listlatestpackages, whose raw output includes each
    package's id in parens per this module's already-established
    substring-parsing convention elsewhere (e.g. group_id_for). Returns
    None if not found. Needed by ensure_errata() (real errata.create takes
    numeric packageIds, not names).
    """
    r = _spacecmd(hostname, exec_prefix,
                  "softwarechannel_listlatestpackages {}".format(shlex.quote(channel_label)))
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if package_name in line:
            m = re.search(r"\((\d+)\)\s*$", line.strip())
            if m:
                return int(m.group(1))
    return None


def ensure_patch_api_allowlisted(hostname, exec_prefix, channel_label):
    """
    Ensures `channel_label` is present in /etc/rhn/rhn.conf's real
    java.allow_adding_patches_via_api key (comma-separated channel labels)
    — confirmed real and required: errata.create's own Java source
    (ErrataHandler.java) reads exactly this config key via
    Config.get().getList(ConfigDefaults.ALLOW_ADDING_PATCHES_VIA_API) and
    refuses any channel not listed, BEFORE creating anything. Idempotent:
    reads the current value, appends only if missing, and does nothing if
    already present. Restarts tomcat so the change actually takes effect
    (rhn.conf is read at startup, matching the same "restart the relevant
    services" requirement already confirmed for web.oidc.* in this same
    module's ensure_mcp_server-adjacent research).
    """
    r = _run(hostname, exec_prefix,
             "grep -E '^java.allow_adding_patches_via_api' /etc/rhn/rhn.conf",
             check=False, capture=True)
    current = (r.stdout or "").strip()
    existing = [c.strip() for c in current.split("=", 1)[1].split(",")] if "=" in current else []
    if channel_label in existing:
        print("  '{}' already allow-listed for API-created patches — leaving it alone".format(
            channel_label))
        return
    existing.append(channel_label)
    new_line = "java.allow_adding_patches_via_api = {}".format(",".join(existing))
    if current:
        cmd = "sed -i 's|^java.allow_adding_patches_via_api.*|{}|' /etc/rhn/rhn.conf".format(
            new_line.replace("|", r"\|"))
    else:
        cmd = "echo {} >> /etc/rhn/rhn.conf".format(shlex.quote(new_line))
    r = _run(hostname, exec_prefix, cmd, check=False)
    if r.returncode != 0:
        die("could not allow-list '{}' for API-created patches: {}".format(
            channel_label, (r.stderr or r.stdout or "").strip()))
    r = _run(hostname, exec_prefix, "systemctl restart tomcat", check=False)
    if r.returncode != 0:
        warn("allow-listed '{}' for API-created patches but could not restart tomcat to apply it "
             "— errata.create will keep failing until it's restarted".format(channel_label))
    print("  Allow-listed '{}' for API-created patches (java.allow_adding_patches_via_api)".format(
        channel_label))


def errata_exists(hostname, exec_prefix, advisory_name):
    """Whether `advisory_name` appears in errata_list's output."""
    r = _spacecmd(hostname, exec_prefix, "errata_list")
    return r.returncode == 0 and advisory_name in (r.stdout or "")


def ensure_errata(hostname, exec_prefix, channel_label, advisory_name, entry):
    """
    Idempotently creates a custom patch/errata via errata.create, scoped to
    `channel_label` (which must already be allow-listed — see
    ensure_patch_api_allowlisted(), called by the caller before this).
    `entry` is one <prefix>_patches list item: {synopsis, advisory_release,
    advisory_type, product, topic, description, solution, severity,
    packages: [names in channel_label, optional]}. Skips if
    errata_exists() already sees this advisory name.
    """
    if errata_exists(hostname, exec_prefix, advisory_name):
        print("  Patch '{}' already exists — leaving it alone".format(advisory_name))
        return
    package_ids = []
    for pkg_name in entry.get("packages") or []:
        pid = package_id_for(hostname, exec_prefix, channel_label, pkg_name)
        if pid is None:
            warn("patch '{}': package '{}' not found in channel '{}' — omitting it from this "
                 "patch".format(advisory_name, pkg_name, channel_label))
            continue
        package_ids.append(pid)

    errata_info = {
        "synopsis": entry.get("synopsis") or advisory_name,
        "advisory_name": advisory_name,
        "advisory_release": entry.get("advisory_release", 1),
        "advisory_type": entry.get("advisory_type", "Bug Fix Advisory"),
        "product": entry.get("product", "lab-in-a-box"),
        "topic": entry.get("topic") or advisory_name,
        "description": entry.get("description") or advisory_name,
        "solution": entry.get("solution", "Update the affected package(s)."),
        "severity": entry.get("severity", "Low"),
    }
    r = _api_call(hostname, exec_prefix, "errata.create",
                  [errata_info, [], [], package_ids, [channel_label]])
    if r.returncode != 0:
        die("could not create patch '{}': {}".format(advisory_name, (r.stderr or r.stdout or "").strip()))
    print("  Created patch '{}' in channel '{}'".format(advisory_name, channel_label))


def ensure_patches(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_patches: a list of {advisory_name, channel, ...}
    dicts (see ensure_errata()'s own docstring for the rest of each entry's
    shape). Allow-lists each distinct `channel` for API-created patches
    exactly once before creating any patch in it.
    """
    allowlisted = set()
    for entry in cfg.get("{}_patches".format(prefix)) or []:
        advisory_name = entry.get("advisory_name")
        channel_label = entry.get("channel")
        if not advisory_name or not channel_label:
            die("{}_patches: an entry needs 'advisory_name' and 'channel'".format(prefix))
        if channel_label not in allowlisted:
            ensure_patch_api_allowlisted(hostname, exec_prefix, channel_label)
            allowlisted.add(channel_label)
        ensure_errata(hostname, exec_prefix, channel_label, advisory_name, entry)


# ─── Image builds (distinct from image imports) ─────────────────────────────
# scheduleImageBuild is called through the api passthrough, with the parameters the handler takes.

def ensure_image_build(hostname, exec_prefix, profile_label, build_host_id, version="latest"):
    """
    Schedules an image build via image.scheduleImageBuild — needs an
    existing image PROFILE (ensure_image_profile, already implemented) and
    a system with the "Container Build Host" entitlement already enabled
    (ensure_container_build_hosts, already implemented — this function
    doesn't check that itself, same "orchestration order is the caller's
    job" convention as image imports' own build_host_id). NOT idempotency-
    checked (no "already building/built this version" list method
    confirmed) — scheduling a build is inherently a one-shot real action,
    same class as import_images()/schedule_ansible_playbook, not part of
    the automatic ensure_* flow's own no-op-on-repeat contract.
    """
    earliest = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    r = _api_call(hostname, exec_prefix, "image.scheduleImageBuild",
                  [profile_label, version, build_host_id, earliest])
    if r.returncode != 0:
        die("could not schedule image build for profile '{}': {}".format(
            profile_label, (r.stderr or r.stdout or "").strip()))
    print("  Scheduled image build for profile '{}' (version: {}, build host sid {})".format(
        profile_label, version, build_host_id))


def build_images(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_image_builds: a list of {profile, build_host,
    version} dicts. `build_host` is a hostname, resolved via _system_id().
    NOT part of the automatic ensure_* flow — same one-shot reasoning as
    import_images(); meant to be run via an explicit trigger flag, not on
    every install_smlm.py invocation.
    """
    for entry in cfg.get("{}_image_builds".format(prefix)) or []:
        profile = entry.get("profile")
        build_host = entry.get("build_host")
        if not profile or not build_host:
            die("{}_image_builds: an entry needs 'profile' and 'build_host'".format(prefix))
        build_host_id = _system_id(hostname, exec_prefix, build_host)
        ensure_image_build(hostname, exec_prefix, profile, build_host_id,
                            version=entry.get("version", "latest"))


# ─── Stored system profiles (package profiles saved from an existing system) ─

def ensure_system_profile(hostname, exec_prefix, system, label, description):
    """
    Idempotently saves a package profile from `system` via spacecmd's
    native system_createpackageprofile (real, confirmed command — no raw
    'api' passthrough needed here). Skipped if `label` already appears in
    system_listpackageprofiles' output — real Uyuni package profiles are
    org-wide objects, not per-system, so this list (not a per-system one)
    is the right existence check.
    """
    r = _spacecmd(hostname, exec_prefix, "system_listpackageprofiles")
    if r.returncode == 0 and label in (r.stdout or ""):
        print("  Package profile '{}' already exists — leaving it alone".format(label))
        return
    r = _spacecmd(hostname, exec_prefix, "system_createpackageprofile {} -n {} -d {}".format(
        shlex.quote(system), shlex.quote(label), shlex.quote(description)))
    if r.returncode != 0:
        die("could not save package profile '{}' from '{}': {}".format(
            label, system, (r.stderr or r.stdout or "").strip()))
    print("  Saved package profile '{}' from '{}'".format(label, system))


def ensure_system_profiles(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_system_profiles: a list of {system, label, description} dicts."""
    for entry in cfg.get("{}_system_profiles".format(prefix)) or []:
        system = entry.get("system")
        label = entry.get("label")
        if not system or not label:
            die("{}_system_profiles: an entry needs 'system' and 'label'".format(prefix))
        ensure_system_profile(hostname, exec_prefix, system, label,
                               entry.get("description") or label)


# ─── Custom info VALUES on specific systems (distinct from the custom info
# KEY definitions ensure_custom_info_keys already manages) ──────────────────

def ensure_system_custom_value(hostname, exec_prefix, system, key, value):
    """
    Idempotently sets one custom info value on `system` via spacecmd's
    native system_addcustomvalue (real, confirmed command — wraps
    system.setCustomValues under the hood). Skipped if system_listcustomvalues
    already shows this exact key: value pair for this system — setCustomValues
    itself is a plain overwrite either way, so this is purely a "don't print
    a false 'set' every re-run" nicety, not a correctness requirement.
    """
    r = _spacecmd(hostname, exec_prefix, "system_listcustomvalues {}".format(shlex.quote(system)))
    if r.returncode == 0 and re.search(r"(?im)^\s*{}\s*:\s*{}\s*$".format(
            re.escape(key), re.escape(value)), r.stdout or ""):
        print("  System '{}' already has {}={} — leaving it alone".format(system, key, value))
        return
    r = _spacecmd(hostname, exec_prefix, "system_addcustomvalue {} {} {}".format(
        shlex.quote(key), shlex.quote(value), shlex.quote(system)))
    if r.returncode != 0:
        die("could not set custom value '{}'='{}' on '{}': {}".format(
            key, value, system, (r.stderr or r.stdout or "").strip()))
    print("  Set custom value '{}'='{}' on '{}'".format(key, value, system))


def ensure_system_custom_values(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_system_custom_values: a list of {system, key,
    value} dicts. The KEY itself must already be defined server-wide (see
    <prefix>_custom_info_keys / ensure_custom_info_keys, already
    implemented) — setCustomValues on an undefined key fails, surfaced via
    this function's own die().
    """
    for entry in cfg.get("{}_system_custom_values".format(prefix)) or []:
        system = entry.get("system")
        key = entry.get("key")
        value = entry.get("value")
        if not (system and key and value is not None):
            die("{}_system_custom_values: an entry needs 'system', 'key' and 'value'".format(prefix))
        ensure_system_custom_value(hostname, exec_prefix, system, key, str(value))


# ─── Organization-to-organization system transfers ──────────────────────────

def org_id_for(hostname, exec_prefix, org_name):
    """
    Return the numeric id of the organization named `org_name`, from org.listOrgs. That call takes no arguments and returns a
    list of {id, name, active_users, systems, system_groups, trusts} structs. Returns None if no organization has that name.

    spacecmd's org_details output does not include an id field, so the id cannot be parsed from it.
    """
    r = _api_call(hostname, exec_prefix, "org.listOrgs", [])
    if r.returncode != 0:
        return None
    try:
        orgs = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    for org in orgs:
        if isinstance(org, dict) and org.get("name") == org_name:
            return org.get("id")
    return None


def ensure_org_system_transfer(hostname, exec_prefix, to_org, systems):
    """
    Move already registered `systems`, given by hostname, into `to_org` with org.transferSystems. The acting administrator must
    be an administrator of the organization, and the source and destination organizations must already trust each other. Use
    ensure_org_trust() first. This function does not create the trust.

    Once a system has been moved, the default administrator's session can no longer look it up by name. A repeat run therefore
    cannot resolve it. The function treats a failed lookup as "already transferred", logs a message, and skips the system instead
    of stopping the install.
    """
    to_org_id = org_id_for(hostname, exec_prefix, to_org)
    if to_org_id is None:
        die("org system transfer: could not resolve a numeric id for org '{}'".format(to_org))

    sids = []
    to_transfer = []
    for s in systems:
        r = _api_call(hostname, exec_prefix, "system.getId", [s])
        matches = None
        if r.returncode == 0:
            try:
                matches = json.loads(r.stdout)
            except (json.JSONDecodeError, TypeError):
                matches = None
        if not matches:
            print("  '{}' isn't visible to resolve a system id for — likely already in org "
                  "'{}' (or another org) from a previous run; skipping".format(s, to_org))
            continue
        sids.append(matches[0]["id"])
        to_transfer.append(s)

    if not sids:
        return
    r = _api_call(hostname, exec_prefix, "org.transferSystems", [to_org_id, sids])
    if r.returncode != 0:
        die("could not transfer {} to org '{}': {}".format(
            to_transfer, to_org, (r.stderr or r.stdout or "").strip()))
    print("  Transferred to org '{}': {}".format(to_org, ", ".join(to_transfer)))


def ensure_org_system_transfers(hostname, exec_prefix, cfg, prefix):
    """Orchestrates <prefix>_org_system_transfers: a list of {org, systems: [...]} dicts."""
    for entry in cfg.get("{}_org_system_transfers".format(prefix)) or []:
        org = entry.get("org")
        systems = entry.get("systems") or []
        if not org or not systems:
            die("{}_org_system_transfers: an entry needs 'org' and a non-empty 'systems' list".format(
                prefix))
        ensure_org_system_transfer(hostname, exec_prefix, org, systems)


# ─── External authentication (SAML 2.0 SSO via Keycloak) ────────────────────
# Uyuni's SSO is SAML 2.0. The OIDC settings (web.oidc.* in rhn.conf) belong to the MCP server's optional OAuth mode, which is
# a separate feature. The rhn.conf keys this module writes are:
#   java.sso = true
#   java.sso.onelogin.saml2.sp.entityid = https://<smlm-fqdn>/rhn/manager/sso/metadata
#   java.sso.onelogin.saml2.sp.assertion_consumer_service.url = https://<smlm-fqdn>/rhn/manager/sso/acs
#   java.sso.onelogin.saml2.idp.entityid = https://<keycloak-fqdn>:<port>/realms/<realm>
#   java.sso.onelogin.saml2.idp.single_sign_on_service.url = https://<keycloak-fqdn>:<port>/realms/<realm>/protocol/saml
# SSO maps the SAML uid attribute to an existing Uyuni username. It does not create accounts. The users must exist before SSO
# is enabled, so callers run this after ensure_users() and ensure_orgs().
#
# Keycloak runs as a standalone podman container on keycloak_host, a separate host that the browser can reach. It is configured
# with kcadm.sh, through podman exec on that host. The configuration is a realm, a SAML client whose id is the SP entity id, a
# "uid" attribute mapper, and the demo user.


_PM_INSTALL = {
    "dnf": "dnf install -y {pkg}",
    "zypper": "zypper --non-interactive install {pkg}",
    "apt-get": "apt-get update -qq && apt-get install -y {pkg}",
}
# Real package name for docker differs on Debian/Ubuntu (docker.io, since
# "docker" there is an unrelated old package) — see ensure_podman_available's
# docker-fallback branch below.
_DOCKER_PKG = {"dnf": "docker", "zypper": "docker", "apt-get": "docker.io"}


def ensure_podman_available(host):
    """
    Ensure a podman-compatible command is on PATH on `host`. The function checks which of dnf, zypper and apt-get exists on
    the host, and installs podman with it. It does not guess the operating system from /etc/os-release.

    Some hosts, such as Amazon Linux 2023, have no podman package in their repositories. There the function installs docker and
    creates a podman command that runs docker. The calls this module makes (run -d --name, exec, inspect and cp) have the same
    syntax in both CLIs.

    The function dies if neither podman nor docker can be installed. A container runtime is required for SSO, which deploys
    Keycloak in a container.
    """
    r = ssh_run(host, "command -v podman", check=False)
    if r.returncode == 0:
        return
    for pm in ("dnf", "zypper", "apt-get"):
        r = ssh_run(host, "command -v {}".format(pm), check=False)
        if r.returncode != 0:
            continue
        r = ssh_run(host, _PM_INSTALL[pm].format(pkg="podman"), check=False, capture=True)
        if r.returncode == 0:
            print("  Installed podman on '{}' (via {})".format(host, pm))
            return
        r = ssh_run(host, _PM_INSTALL[pm].format(pkg=_DOCKER_PKG[pm]), check=False, capture=True)
        if r.returncode != 0:
            die("could not install podman OR docker on '{}' via {}: {}".format(
                host, pm, (r.stderr or r.stdout or "").strip()))
        ssh_run(host, "systemctl enable --now docker", check=False)
        r = ssh_run(host, "ln -sf $(command -v docker) /usr/local/bin/podman", check=False, capture=True)
        if r.returncode != 0:
            die("installed docker on '{}' but could not shim it as 'podman': {}".format(
                host, (r.stderr or r.stdout or "").strip()))
        print("  '{}' has no podman package available — installed docker instead and shimmed "
              "'podman' onto it (via {})".format(host, pm))
        return
    die("no supported package manager (dnf/zypper/apt-get) found on '{}' to install a "
        "container runtime with".format(host))


def _ensure_keycloak_tls_cert(keycloak_host):
    """
    Ensure a self-signed TLS certificate and key exist on `keycloak_host` at /etc/keycloak-tls/cert.pem and key.pem. They
    are kept on the host, outside the container, and ensure_keycloak() bind-mounts them in, so they survive a container
    recreation.

    Browsers that enforce HTTPS-only navigation do not reach a plain-HTTP Keycloak, so Keycloak serves HTTPS. A self-signed certificate
    is enough for this lab, because no public CA is involved in the SAML redirect.

    The key is world-readable (644 for the certificate, 755 for the directory). The official Keycloak image runs as a non-root user,
    which must read the mounted key, and the root-owned openssl defaults would block it.
    """
    r = ssh_run(keycloak_host, "test -f /etc/keycloak-tls/cert.pem && test -f /etc/keycloak-tls/key.pem",
                check=False)
    if r.returncode == 0:
        # Re-assert readable permissions on every call. A certificate created by an earlier version can have root-only modes,
        # so existence alone is not enough.
        ssh_run(keycloak_host, "chmod 755 /etc/keycloak-tls; "
                "chmod 644 /etc/keycloak-tls/key.pem /etc/keycloak-tls/cert.pem", check=False)
        return
    subj = "/CN={}".format(keycloak_host)
    r = ssh_run(keycloak_host,
                "mkdir -p /etc/keycloak-tls && chmod 755 /etc/keycloak-tls && "
                "openssl req -x509 -newkey rsa:2048 -nodes -days 3650 "
                "-keyout /etc/keycloak-tls/key.pem -out /etc/keycloak-tls/cert.pem "
                "-subj {} && "
                "chmod 644 /etc/keycloak-tls/key.pem /etc/keycloak-tls/cert.pem".format(shlex.quote(subj)),
                check=False, capture=True)
    if r.returncode != 0:
        die("could not generate a self-signed TLS certificate for Keycloak on '{}': {}".format(
            keycloak_host, (r.stderr or r.stdout or "").strip()))
    print("  Generated a self-signed TLS certificate for Keycloak on '{}'".format(keycloak_host))


def _keycloak_java_opts(keycloak_host):
    """
    Pick the JVM heap and metaspace caps for the Keycloak container, sized to the host's total RAM as reported by free -m.
    One fixed value for every host fails in one of two ways. A cap sized for a small host causes an out-of-memory kill during the
    Quarkus build step on that host, and it also starves a large host during sustained admin-API use. Sizing from the host's RAM
    avoids both.
    """
    r = ssh_run(keycloak_host, "free -m", check=False, capture=True)
    total_mb = 0
    if r.returncode == 0:
        m = re.search(r"(?m)^Mem:\s+(\d+)", r.stdout or "")
        if m:
            total_mb = int(m.group(1))
    if 0 < total_mb < 1200:
        return "-Xms128m -Xmx384m -XX:MaxMetaspaceSize=128m"
    return "-Xms256m -Xmx1024m -XX:MaxMetaspaceSize=512m"


def ensure_keycloak(keycloak_host, port, realm, admin_user, admin_password):
    """
    Idempotently deploy Keycloak, the official quay.io/keycloak/keycloak image, as a standalone podman container on
    `keycloak_host`. It runs in dev mode, but is served over HTTPS with the certificate from _ensure_keycloak_tls_cert(). `port` is
    published to the container's HTTPS listener, port 8443. The plain HTTP listener on 8080 stays inside the container, and kcadm
    reaches it there through _kcadm(). The function ensures podman is present first, with ensure_podman_available().

    It checks the container's running state with podman inspect -f {{.State.Running}}. A stopped or crashed container is
    recreated. A container that is running but fails the HTTPS readiness probe is also recreated, so an older plain-HTTP container
    is replaced on the next run. The JVM caps are set through JAVA_OPTS_APPEND, sized to the host by _keycloak_java_opts(). The
    function waits up to 120 seconds for the realm endpoint to answer, because the build step on a small VM can take a minute or
    more. Callers can run kcadm against Keycloak as soon as the function returns.
    """
    ensure_podman_available(keycloak_host)

    def _fresh_run():
        _ensure_keycloak_tls_cert(keycloak_host)
        java_opts = _keycloak_java_opts(keycloak_host)
        r = ssh_run(keycloak_host,
                    "podman rm -f keycloak >/dev/null 2>&1; "
                    "podman run -d --name keycloak -p {port}:8443 "
                    "-v /etc/keycloak-tls:/etc/keycloak-tls:ro "
                    "-e KEYCLOAK_ADMIN={admin} -e KEYCLOAK_ADMIN_PASSWORD={password} "
                    "-e KC_HTTPS_CERTIFICATE_FILE=/etc/keycloak-tls/cert.pem "
                    "-e KC_HTTPS_CERTIFICATE_KEY_FILE=/etc/keycloak-tls/key.pem "
                    "-e JAVA_OPTS_APPEND={java_opts} "
                    "quay.io/keycloak/keycloak:26.1.1 start-dev".format(
                        port=port, admin=shlex.quote(admin_user), password=shlex.quote(admin_password),
                        java_opts=shlex.quote(java_opts)),
                    check=False, capture=True)
        if r.returncode != 0:
            die("could not start the Keycloak container on '{}': {}".format(
                keycloak_host, (r.stderr or r.stdout or "").strip()))
        print("  Started Keycloak on '{}:{}' over HTTPS (self-signed cert, dev mode)".format(
            keycloak_host, port))

    def _wait_ready(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            r = ssh_run(keycloak_host,
                        "curl -sfk https://localhost:{}/realms/master >/dev/null".format(port),
                        check=False)
            if r.returncode == 0:
                return True
            r = ssh_run(keycloak_host, "podman inspect -f '{{.State.Running}}' keycloak",
                        check=False, capture=True)
            if r.returncode == 0 and r.stdout.strip() != "true":
                return False  # container exited — no point burning the rest of the deadline
            time.sleep(5)
        return False

    r = ssh_run(keycloak_host, "podman inspect -f '{{.State.Running}}' keycloak", check=False, capture=True)
    exists = r.returncode == 0
    running = exists and r.stdout.strip() == "true"

    if running:
        if _wait_ready(30):
            print("  Keycloak container already running (HTTPS OK) on '{}' — leaving it alone".format(
                keycloak_host))
            return
        print("  Keycloak container on '{}' is running but not answering over HTTPS — recreating it".format(
            keycloak_host))
    elif exists:
        # Restart a stopped container in place first, so a healthy container is not recreated and its configuration is kept.
        # Recreate it only when the restart does not bring it up cleanly.
        r2 = ssh_run(keycloak_host, "podman start keycloak", check=False, capture=True)
        if r2.returncode == 0 and _wait_ready(120):
            print("  Restarted the existing (stopped) Keycloak container on '{}'".format(keycloak_host))
            return
        print("  Existing Keycloak container on '{}' didn't come up cleanly — recreating it".format(
            keycloak_host))

    _fresh_run()
    if not _wait_ready(120):
        die("Keycloak on '{}:{}' never became ready within 120s".format(keycloak_host, port))


def _kcadm(keycloak_host, port, admin_user, admin_password, realm_admin_args, args):
    """
    Runs one `kcadm.sh` command inside the keycloak container (real bundled
    admin CLI at /opt/keycloak/bin/kcadm.sh), always logging in fresh first
    (`config credentials` — kcadm caches a token in its own config dir
    inside the container between calls, but re-logging in every call is
    simpler and safe: idempotent, no meaningful cost). `args` is the
    sub-command and its own flags as one pre-quoted string.
    """
    login = ("/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 "
              "--realm master --user {} --password {}".format(
                  shlex.quote(admin_user), shlex.quote(admin_password)))
    cmd = "podman exec keycloak sh -c {}".format(shlex.quote("{} && /opt/keycloak/bin/kcadm.sh {}".format(
        login, args)))
    return ssh_run(keycloak_host, cmd, check=False, capture=True)


def ensure_keycloak_realm(keycloak_host, port, realm, admin_user, admin_password):
    """Idempotently creates a Keycloak realm (kcadm get/create realms)."""
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get realms/{}".format(shlex.quote(realm)))
    if r.returncode == 0:
        print("  Keycloak realm '{}' already exists — leaving it alone".format(realm))
        return
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "create realms -s realm={} -s enabled=true".format(shlex.quote(realm)))
    if r.returncode != 0:
        die("could not create Keycloak realm '{}': {}".format(realm, (r.stderr or r.stdout or "").strip()))
    print("  Created Keycloak realm '{}'".format(realm))


def _ensure_keycloak_client_sls_url(keycloak_host, port, admin_user, admin_password, realm,
                                     client_id, sls_url):
    """
    Add the saml_single_logout_service_url_redirect attribute to an existing SAML client that lacks it. Clients created by an
    earlier version of ensure_keycloak_saml_client() did not have this attribute. Without it, Keycloak shows a "Logout failed"
    page instead of logging the user out. A plain existence check would leave such a client broken, so this function corrects it
    in place.
    """
    # The full client object is fetched, without a --fields filter. A nested field such as attributes is returned empty when
    # a --fields filter selects it.
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get clients -r {} -q clientId={}".format(shlex.quote(realm), shlex.quote(client_id)))
    if r.returncode != 0 or not (r.stdout or "").strip():
        return
    try:
        clients = [c for c in json.loads(r.stdout) if isinstance(c, dict)]
    except (json.JSONDecodeError, TypeError):
        return
    if not clients:
        return
    client = clients[0]
    if (client.get("attributes") or {}).get("saml_single_logout_service_url_redirect") == sls_url:
        return
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "update clients/{} -r {} -s attributes.saml_single_logout_service_url_redirect={}".format(
                   shlex.quote(client.get("id")), shlex.quote(realm), sls_url))
    if r.returncode != 0:
        die("could not set the logout service URL on Keycloak SAML client '{}': {}".format(
            client_id, (r.stderr or r.stdout or "").strip()))
    print("  Set the missing logout service URL on Keycloak SAML client '{}' (was causing "
          "'Logout failed' on every real logout attempt)".format(client_id))


def ensure_keycloak_saml_client(keycloak_host, port, realm, admin_user, admin_password,
                                 client_id, redirect_uri, sls_url=None):
    """
    Idempotently create a SAML client in `realm`. The client_id is the Uyuni service-provider entity id, for example
    "https://sol.mydemo.lab/rhn/manager/sso/metadata". The client has a "uid" SAML attribute mapper, which is
    saml-user-property-mapper and maps the user's username to an attribute named uid. Assertion signing is on, with RSA_SHA1, the
    Key ID key-name format, and no required client signature. The attributes are Keycloak's own names for these SAML settings.

    sls_url is the Uyuni Single Logout Service endpoint, GET /manager/sso/sls. It is separate from the assertion consumer endpoint
    used for login. It is set as saml_single_logout_service_url_redirect. Keycloak fails logout without it. The argument is
    optional, and ensure_sso() always passes it.
    """
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get clients -r {} -q clientId={}".format(shlex.quote(realm), shlex.quote(client_id)))
    if r.returncode == 0 and client_id in (r.stdout or ""):
        print("  Keycloak SAML client '{}' already exists in realm '{}' — leaving it alone".format(
            client_id, realm))
        if sls_url:
            _ensure_keycloak_client_sls_url(keycloak_host, port, admin_user, admin_password, realm,
                                             client_id, sls_url)
    else:
        # kcadm takes a dotted key inside a nested map as a quoted segment within -s, for example
        # -s 'attributes."saml.assertion.signature"=true'. shlex.quote() on the whole -s value produces that form.
        # saml_single_logout_service_url_redirect has no dots, so it is set as a plain dotted path, attributes.<key>=value.
        args = [
            "create", "clients", "-r", realm,
            "-s", "clientId={}".format(client_id),
            "-s", "protocol=saml",
            "-s", "enabled=true",
            "-s", "redirectUris=[{}]".format(json.dumps(redirect_uri)),
            "-s", 'attributes."saml.assertion.signature"=true',
            "-s", 'attributes."saml.signature.algorithm"=RSA_SHA1',
            "-s", 'attributes."saml.signature.keyinfo.xmlSigKeyInfoKeyNameTransformer"=KEY_ID',
            "-s", 'attributes."saml.client.signature"=false',
        ]
        if sls_url:
            args += ["-s", "attributes.saml_single_logout_service_url_redirect={}".format(sls_url)]
        r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
                   " ".join(shlex.quote(a) for a in args))
        if r.returncode != 0:
            die("could not create Keycloak SAML client '{}': {}".format(
                client_id, (r.stderr or r.stdout or "").strip()))
        print("  Created Keycloak SAML client '{}' in realm '{}'".format(client_id, realm))

    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get clients -r {} -q clientId={} --fields id --format csv --noquotes".format(
                   shlex.quote(realm), shlex.quote(client_id)))
    client_uuid = (r.stdout or "").strip().splitlines()[-1] if r.returncode == 0 and r.stdout else None
    if not client_uuid:
        die("could not resolve the internal id of Keycloak SAML client '{}'".format(client_id))

    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get clients/{}/protocol-mappers/models -r {}".format(
                   shlex.quote(client_uuid), shlex.quote(realm)))
    existing_mapper = None
    if r.returncode == 0 and (r.stdout or "").strip():
        try:
            existing_mapper = next(
                (m for m in json.loads(r.stdout) if isinstance(m, dict) and m.get("name") == "uid"), None)
        except (json.JSONDecodeError, TypeError, AttributeError):
            existing_mapper = None
    if existing_mapper is not None:
        # Self-heals a mapper created by an earlier, buggy version of this
        # function that also set config."friendly.name" — see the real
        # "duplicated Attribute Name" bug fixed just below for why that
        # broke every actual login attempt despite the mapper otherwise
        # looking completely fine.
        if existing_mapper.get("config", {}).get("friendly.name"):
            r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
                       "update clients/{}/protocol-mappers/models/{} -r {} "
                       "-s 'config.\"friendly.name\"='".format(
                           shlex.quote(client_uuid), shlex.quote(existing_mapper.get("id")), shlex.quote(realm)))
            if r.returncode != 0:
                die("could not remove the stale friendlyName from Keycloak SAML client '{}''s 'uid' "
                    "mapper: {}".format(client_id, (r.stderr or r.stdout or "").strip()))
            print("  Removed the stale friendlyName from Keycloak SAML client '{}''s 'uid' attribute "
                  "mapper (was causing every real login to fail with a duplicated-Attribute SAML "
                  "error)".format(client_id))
            return
        print("  Keycloak SAML client '{}' already has its 'uid' attribute mapper".format(client_id))
        return
    # friendly.name is not set. Setting it to the same value as attribute.name makes Keycloak emit two Attribute elements with
    # the same Name, and the SAML parser rejects that. SSOController reads attributes by Name only, so FriendlyName is not needed.
    args = [
        "create", "clients/{}/protocol-mappers/models".format(client_uuid), "-r", realm,
        "-s", "name=uid",
        "-s", "protocol=saml",
        "-s", "protocolMapper=saml-user-property-mapper",
        "-s", 'config."user.attribute"=username',
        "-s", 'config."attribute.name"=uid',
    ]
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               " ".join(shlex.quote(a) for a in args))
    if r.returncode != 0:
        die("could not add the 'uid' attribute mapper to Keycloak SAML client '{}': {}".format(
            client_id, (r.stderr or r.stdout or "").strip()))
    print("  Added the 'uid' SAML attribute mapper to Keycloak SAML client '{}'".format(client_id))


def ensure_keycloak_user(keycloak_host, port, realm, admin_user, admin_password, username, password, email,
                          first_name=None, last_name=None):
    """
    Idempotently create one Keycloak user with a password in `realm`. Keycloak 26's default user profile requires firstName and
    lastName. A user without them cannot log in, even with the right password. first_name and last_name are optional and default
    to the username, because only the presence of a non-empty value matters here. emailVerified is set to true, because an
    unverified email address causes the same login failure.

    An existing user that lacks any of these attributes is updated in place, so accounts created by an earlier version of this
    function can log in.
    """
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "get users -r {} -q username={}".format(shlex.quote(realm), shlex.quote(username)))
    existing = None
    if r.returncode == 0 and (r.stdout or "").strip():
        try:
            matches = [u for u in json.loads(r.stdout) if isinstance(u, dict) and u.get("username") == username]
            existing = matches[0] if matches else None
        except (json.JSONDecodeError, TypeError, AttributeError):
            existing = None
    if existing is not None:
        fixes = []
        if not existing.get("firstName"):
            fixes.append("-s firstName={}".format(shlex.quote(first_name or username)))
        if not existing.get("lastName"):
            fixes.append("-s lastName={}".format(shlex.quote(last_name or username)))
        if not existing.get("emailVerified"):
            fixes.append("-s emailVerified=true")
        if not fixes:
            print("  Keycloak user '{}' already exists in realm '{}' — leaving it alone".format(
                username, realm))
            return
        r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
                   "update users/{} -r {} {}".format(existing.get("id"), shlex.quote(realm), " ".join(fixes)))
        if r.returncode != 0:
            die("could not backfill required profile fields on existing Keycloak user '{}': {}".format(
                username, (r.stderr or r.stdout or "").strip()))
        print("  Backfilled required profile fields on existing Keycloak user '{}' in realm '{}' "
              "(needed for real login to work, not cosmetic)".format(username, realm))
        return
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "create users -r {} -s username={} -s enabled=true -s email={} -s emailVerified=true "
               "-s firstName={} -s lastName={}".format(
                   shlex.quote(realm), shlex.quote(username), shlex.quote(email),
                   shlex.quote(first_name or username), shlex.quote(last_name or username)))
    if r.returncode != 0:
        die("could not create Keycloak user '{}': {}".format(username, (r.stderr or r.stdout or "").strip()))
    r = _kcadm(keycloak_host, port, admin_user, admin_password, None,
               "set-password -r {} --username {} --new-password {}".format(
                   shlex.quote(realm), shlex.quote(username), shlex.quote(password)))
    if r.returncode != 0:
        die("could not set a password for Keycloak user '{}': {}".format(
            username, (r.stderr or r.stdout or "").strip()))
    print("  Created Keycloak user '{}' in realm '{}'".format(username, realm))


def _keycloak_idp_cert(keycloak_host, port, realm):
    """
    Fetches the IdP's real signing certificate from Keycloak's own SAML
    metadata endpoint (a standard, unauthenticated GET — no admin
    credentials needed). Required by ensure_sso(): java-saml refuses to
    even render the login page's SSO option without either this
    certificate or a fingerprint configured — see ensure_sso's own
    docstring for the real "idp_cert_or_fingerprint_not_found_and_required"
    bug this fixes.
    """
    r = ssh_run(keycloak_host, "curl -sfk https://localhost:{}/realms/{}/protocol/saml/descriptor".format(
        port, shlex.quote(realm)), check=False, capture=True)
    if r.returncode != 0:
        die("could not fetch the IdP signing certificate from Keycloak realm '{}' on '{}:{}': {}".format(
            realm, keycloak_host, port, (r.stderr or r.stdout or "").strip()))
    m = re.search(r"<ds:X509Certificate>([^<]+)</ds:X509Certificate>", r.stdout or "")
    if not m:
        die("Keycloak realm '{}' SAML descriptor on '{}:{}' had no X509Certificate — cannot "
            "configure SSO without the IdP's signing certificate".format(realm, keycloak_host, port))
    return m.group(1).strip()


def ensure_sso(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrate <prefix>_sso, which deploys Keycloak on a separate host and connects this server to it for SAML 2.0 single
    sign-on. The function does nothing if the field is unset. Fields:

      "keycloak_host"   required. A separate host that the browser can reach, because the SAML redirect goes through the browser.
      "keycloak_port"   optional, default 8080.
      "realm"           optional, default "lab-in-a-box".
      "admin_user" and "admin_password"   Keycloak's own admin account. Optional, default "admin" and "admin".
      "demo_user", "demo_password" and "demo_email"   one extra Keycloak user, beyond the users the function creates
                        automatically. That user must also exist as a Uyuni user with the same username, because SSO maps to an
                        existing account and never creates one.

    The function sets java.sso=true and the java.sso.onelogin.saml2.* keys in /etc/rhn/rhn.conf inside the container, through
    exec_prefix. Then it runs mgradm restart on the host, through a raw SSH command. The restart is a host-level command, not an
    exec_prefix command, because mgradm manages the containers from outside them.

    java-saml needs the IdP's signing certificate to render the login page. Without it, every page load fails, and the classic
    login behind it cannot complete. The certificate is fetched from Keycloak's SAML IdP metadata, by _keycloak_idp_cert(), and
    written as the sixth key.

    Each of the six keys is checked on its own, and a value that differs from the expected one is corrected in place. A key
    that is present but has an outdated value, such as an http:// URL after the switch to HTTPS, is therefore fixed.
    """
    field = "{}_sso".format(prefix)
    if cfg.get(field) is None:
        # The check tests for presence, not truthiness, so that an empty mapping counts as a value.
        return
    sso = cfg[field]
    keycloak_host = sso.get("keycloak_host")
    if not keycloak_host:
        die("{}_sso: 'keycloak_host' is required".format(prefix))
    port = sso.get("keycloak_port", 8080)
    realm = sso.get("realm", "lab-in-a-box")
    admin_user = sso.get("admin_user", "admin")
    admin_password = sso.get("admin_password", "admin")

    ensure_keycloak(keycloak_host, port, realm, admin_user, admin_password)
    ensure_keycloak_realm(keycloak_host, port, realm, admin_user, admin_password)

    sp_entityid = "https://{}/rhn/manager/sso/metadata".format(hostname)
    acs_url = "https://{}/rhn/manager/sso/acs".format(hostname)
    sls_url = "https://{}/rhn/manager/sso/sls".format(hostname)
    ensure_keycloak_saml_client(keycloak_host, port, realm, admin_user, admin_password,
                                 sp_entityid, acs_url, sls_url)

    # Once SSO is on, the login form sends everyone through Keycloak, including the SMLM admin account. Each account that can
    # log in through SSO therefore needs a Keycloak user with a password: the SMLM admin account, and each <prefix>_users entry
    # that has a password. A PAM entry authenticates through the OS, so it has no Keycloak user.
    smlm_admin_user = cfg.get("{}_admin_user".format(prefix)) or "admin"
    smlm_admin_pass = cfg.get("{}_admin_pass".format(prefix)) or "Smlm12345"
    ensure_keycloak_user(keycloak_host, port, realm, admin_user, admin_password,
                          smlm_admin_user, smlm_admin_pass,
                          "{}@mydemo.lab".format(smlm_admin_user))

    for entry in cfg.get("{}_users".format(prefix)) or []:
        if entry.get("pam"):
            continue
        username = entry.get("username")
        password = entry.get("password")
        if not username or not password:
            continue
        ensure_keycloak_user(keycloak_host, port, realm, admin_user, admin_password,
                              username, password, entry.get("email") or
                              "{}@mydemo.lab".format(username),
                              first_name=entry.get("first_name"), last_name=entry.get("last_name"))

    demo_user = sso.get("demo_user")
    if demo_user:
        ensure_keycloak_user(keycloak_host, port, realm, admin_user, admin_password,
                              demo_user, sso.get("demo_password", "SsoDemo12345"),
                              sso.get("demo_email", "{}@mydemo.lab".format(demo_user)))

    idp_cert = _keycloak_idp_cert(keycloak_host, port, realm)
    idp_entityid = "https://{}:{}/realms/{}".format(keycloak_host, port, realm)
    idp_sso_url = "https://{}:{}/realms/{}/protocol/saml".format(keycloak_host, port, realm)
    # Keycloak uses the same endpoint for SSO and SLO. The SLO URL is set explicitly. Otherwise java-saml falls back to a
    # placeholder URL, and logout never reaches Keycloak.
    idp_slo_url = idp_sso_url
    desired = [
        ("java.sso", "java.sso = true"),
        ("java.sso.onelogin.saml2.sp.entityid",
         "java.sso.onelogin.saml2.sp.entityid = {}".format(sp_entityid)),
        ("java.sso.onelogin.saml2.sp.assertion_consumer_service.url",
         "java.sso.onelogin.saml2.sp.assertion_consumer_service.url = {}".format(acs_url)),
        ("java.sso.onelogin.saml2.idp.entityid",
         "java.sso.onelogin.saml2.idp.entityid = {}".format(idp_entityid)),
        ("java.sso.onelogin.saml2.idp.single_sign_on_service.url",
         "java.sso.onelogin.saml2.idp.single_sign_on_service.url = {}".format(idp_sso_url)),
        ("java.sso.onelogin.saml2.idp.single_logout_service.url",
         "java.sso.onelogin.saml2.idp.single_logout_service.url = {}".format(idp_slo_url)),
        ("java.sso.onelogin.saml2.idp.x509cert",
         "java.sso.onelogin.saml2.idp.x509cert = {}".format(idp_cert)),
    ]
    # Each of the six keys is checked for presence and for its value. When the scheme changes, for example from http to https,
    # an existing key with an outdated value is corrected in place.
    r = _run(hostname, exec_prefix, "cat /etc/rhn/rhn.conf", check=False, capture=True)
    if r.returncode != 0 or not (r.stdout or "").strip():
        # Overwriting the whole file below on a failed/empty read would
        # silently destroy every OTHER rhn.conf setting, not just SSO's own
        # — die instead of risking that.
        die("could not read /etc/rhn/rhn.conf on '{}' (needed before rewriting it): {}".format(
            hostname, (r.stderr or r.stdout or "").strip()))
    lines = r.stdout.splitlines()
    changed = False
    for key, desired_line in desired:
        pattern = re.compile(r"^{}\s*=".format(re.escape(key)))
        for i, line in enumerate(lines):
            if pattern.match(line):
                if line.rstrip() != desired_line.rstrip():
                    lines[i] = desired_line
                    changed = True
                break
        else:
            lines.append(desired_line)
            changed = True
    if not changed:
        print("  SSO settings already fully present and correct in /etc/rhn/rhn.conf on '{}' — "
              "leaving them alone (no restart needed)".format(hostname))
        return
    r = _run(hostname, exec_prefix, "cat > /etc/rhn/rhn.conf",
             input_text="\n".join(lines) + "\n", check=False)
    if r.returncode != 0:
        die("could not write SSO settings to /etc/rhn/rhn.conf: {}".format(
            (r.stderr or r.stdout or "").strip()))
    r = ssh_run(hostname, "mgradm restart", check=False, capture=True)
    if r.returncode != 0:
        warn("wrote SSO settings to rhn.conf but 'mgradm restart' failed — SSO won't take effect "
             "until the server is restarted: {}".format((r.stderr or r.stdout or "").strip()))
        return
    print("  Configured SAML 2.0 SSO against Keycloak realm '{}' on '{}:{}' and restarted the server".format(
        realm, keycloak_host, port))


# ─── Virtual-guest provisioning (autoinstallation on a virtualization host) ──
# system.provisionVirtualGuest is a SystemHandler method. hostSid is the numeric id of a registered client that is a
# virtualization host. This is separate from a Virtual Host Manager, which only tracks inventory and does not affect this call.

def provision_virtual_guest(hostname, exec_prefix, host_system, guest_name, kickstart_profile,
                             memory_mb, vcpus, disk_gb):
    """
    Provision a new virtual guest on `host_system` through system.provisionVirtualGuest, which autoinstalls it from
    `kickstart_profile`, a name reference into <prefix>_kickstart_profiles. host_system is a hostname. It is resolved to its numeric
    id with _system_id(), and the system must already be registered and capable of virtualization.

    Provisioning creates a real VM, so it is one-shot work and is not idempotent. The automatic ensure_* flow does not run it.

    The function first ensures that host_system has the virtualization_host entitlement, with system.addEntitlements, so the
    lab definition alone is enough to reproduce the setup. That call ignores an entitlement the system already has.
    """
    host_sid = _system_id(hostname, exec_prefix, host_system)
    r = _api_call(hostname, exec_prefix, "system.addEntitlements", [host_sid, ["virtualization_host"]])
    if r.returncode != 0:
        die("could not enable the Virtualization Host entitlement on '{}': {}".format(
            host_system, (r.stderr or r.stdout or "").strip()))
    r = _api_call(hostname, exec_prefix, "system.provisionVirtualGuest",
                  [host_sid, guest_name, kickstart_profile, memory_mb, vcpus, disk_gb])
    if r.returncode != 0:
        die("could not provision virtual guest '{}' on '{}': {}".format(
            guest_name, host_system, (r.stderr or r.stdout or "").strip()))
    print("  Provisioned virtual guest '{}' on '{}' (kickstart profile: {}, {}MB RAM, "
          "{} vCPU, {}GB disk)".format(guest_name, host_system, kickstart_profile,
                                        memory_mb, vcpus, disk_gb))


def provision_virtual_guests(hostname, exec_prefix, cfg, prefix):
    """
    Orchestrates <prefix>_virtual_guests: a list of {host, name,
    kickstart_profile, memory_mb, vcpus, disk_gb} dicts. NOT part of the
    automatic ensure_* flow — meant to be invoked via the install scripts'
    own explicit-trigger flag, same reasoning as image builds/imports.
    """
    for entry in cfg.get("{}_virtual_guests".format(prefix)) or []:
        host = entry.get("host")
        name = entry.get("name")
        kickstart_profile = entry.get("kickstart_profile")
        if not (host and name and kickstart_profile):
            die("{}_virtual_guests: an entry needs 'host', 'name' and 'kickstart_profile'".format(
                prefix))
        provision_virtual_guest(hostname, exec_prefix, host, name, kickstart_profile,
                                 entry.get("memory_mb", 2048), entry.get("vcpus", 2),
                                 entry.get("disk_gb", 20))
