"""
primary.py — Lab definition loading, config parsing, and validation.

Python equivalent of primary_functions.bash.

Typical usage:
    from primary import load_definition, load_config, load_defaults, validate_definition
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import os
import re
import sys
from pathlib import Path


# ── Definition loading ────────────────────────────────────────────────────────

class LabDefinition(dict):
    """
    A loaded lab definition. Behaves as a plain dict for every existing
    read access (.get()/["..."]/.items()/json.dumps()/isinstance(x, dict) —
    every one of the ~40 addons and every library function that reads a
    definition keeps working completely unchanged), but also carries where
    it came from and in what format, as plain instance attributes:

        .source_path  — the file path it was loaded from
        .fmt          — "json" or "yaml", whichever was actually used

    These are attributes, not dict keys, so they never show up in
    .items()/.keys()/iteration or get serialized by json.dumps()/
    yaml.safe_dump() — the dict's own content is always exactly what was
    on disk, nothing extra riding along in it.

    The point: anything holding a `definition` already has everything it
    needs to save a change back later (see save_definition() below) without
    a separate path/format argument threaded through every function in the
    call chain — logic that mutates a value (e.g. backends.py's MAC-
    conflict resolution) shouldn't need to know or care where the file
    lives or what format it's in; that's this object's job, not theirs.
    """

    def __init__(self, data, source_path, fmt):
        super().__init__(data)
        self.source_path = source_path
        self.fmt = fmt


def load_definition(path):
    """
    Load a lab definition from a JSON or YAML file. Dies (SystemExit) on any
    parse failure — for the graceful, non-dying equivalent used by preflight/
    --validate paths that need to fold a parse failure into their own issue
    list, see try_load_definition() below (this function is a thin wrapper
    around it).

    Returns a LabDefinition (see above). YAML input requires pyyaml
    (pip install pyyaml). Falls back to YAML parsing if the file is not
    valid JSON.
    """
    definition, error = try_load_definition(path)
    if error:
        _die(error)
    return definition


def try_load_definition(path):
    """
    Format-detecting lab-definition parse that never dies: returns
    (definition, error) where exactly one of the two is None/empty.
    `definition`, when present, is a LabDefinition (see above) — it already
    knows its own source path and format, so nothing downstream needs to
    re-derive or re-pass either.

    Detection mirrors load_definition(): a .yaml/.yml extension parses as
    YAML directly; anything else is tried as JSON first, falling back to
    YAML if that fails (so an extensionless or oddly-named file still works
    either way). YAML parsing (including the fallback) requires pyyaml.
    """
    p = Path(path)
    if not p.exists():
        return None, "Lab definition file '{}' not found".format(path)

    try:
        text = p.read_text()
    except OSError as e:
        return None, "could not read '{}': {}".format(path, e)

    is_yaml_ext = p.suffix.lower() in (".yaml", ".yml")

    json_error = None
    if not is_yaml_ext:
        try:
            return LabDefinition(json.loads(text), path, "json"), None
        except json.JSONDecodeError as e:
            json_error = e

    try:
        import yaml
    except ImportError:
        return None, (
            "PyYAML is required to parse '{}' as YAML.\n"
            "Install it with:  pip install pyyaml".format(path)
        )

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        if json_error is not None:
            return None, (
                "'{}' is not valid JSON ({}) or YAML ({})".format(path, json_error, e)
            )
        return None, "YAML syntax error in '{}': {}".format(path, e)

    if not isinstance(data, dict):
        return None, "'{}' does not contain a mapping at the top level".format(path)

    return LabDefinition(data, path, "yaml"), None


def save_definition(definition):
    """
    Persist a change made to an in-memory `definition` (a LabDefinition, or
    any dict-like object carrying .source_path/.fmt attributes in the same
    shape). Returns the path actually written.

    Deliberately does NOT overwrite .source_path itself: this project's
    lab definitions are sometimes hand-edited (comments, specific
    formatting), and a plain json.dumps()/yaml.safe_dump() round-trip would
    silently discard all of that. Instead, writes to
    "<source_path>.system_modified.<fmt>" — a clearly-named sibling file the
    operator can review and merge back manually. The original file is never
    touched.

    This is the ONLY place a lab-definition-mutating change gets written to
    disk — a caller that needs to persist something (e.g. backends.py's
    MAC-conflict resolution, generating a new MAC) mutates the in-memory
    `definition` directly and calls this; there is no re-reading of the
    source file anywhere in that path, ever — the in-memory `definition` IS
    the current, authoritative state.

    Dies (SystemExit) if pyyaml is required and missing, same as
    load_definition().
    """
    path = definition.source_path
    fmt = definition.fmt
    output_path = "{}.system_modified.{}".format(path, fmt)

    if fmt == "yaml":
        try:
            import yaml
        except ImportError:
            _die(
                "PyYAML is required to write '{}' as YAML.\n"
                "Install it with:  pip install pyyaml".format(output_path)
            )
        Path(output_path).write_text(yaml.safe_dump(dict(definition), sort_keys=False))
    else:
        Path(output_path).write_text(json.dumps(dict(definition), indent=2))

    return output_path


# ── Config / defaults loading ─────────────────────────────────────────────────

_DEFAULT_CFG_PATHS      = ["/etc/lab_creation.cfg", "lab_creation.cfg"]
_DEFAULT_DEFAULTS_PATHS = ["/etc/lab_creation.defaults", "lab_creation.defaults"]


def load_config(paths=None):
    """
    Load lab_creation.cfg (node-specific settings: REMOTE_HOST, ROOT_SSH_KEY, VIRT_SRV, …).

    Searches paths in order; uses system + local defaults when paths is None.
    Returns a dict of the parsed key-value pairs.
    Raises SystemExit if no config file is found.
    """
    return _load_shell_vars_file(paths or _DEFAULT_CFG_PATHS, "lab_creation.cfg")


def load_defaults(paths=None):
    """
    Load lab_creation.defaults (system-wide defaults: _lib_path, VM_IMG_LOC, delay_min, …).

    Parses only simple KEY=value assignments; skips bash arrays and expressions.
    Returns a dict.
    """
    return _load_shell_vars_file(paths or _DEFAULT_DEFAULTS_PATHS, "lab_creation.defaults")


def load_shell_vars(path):
    """
    Parse an arbitrary simple-shell-variable config file at an exact path
    (unlike load_config/load_defaults, which search a list of default
    locations for a specific filename). Used for config files outside the
    lab_creation.cfg/.defaults pair — e.g. setup_demo_server/lab.cfg.
    Returns a dict. Raises SystemExit if the file doesn't exist.
    """
    p = Path(path)
    if not p.exists():
        _die("Configuration file '{}' not found".format(path))
    return _parse_shell_vars(p.read_text())


_DEFAULT_CREDENTIALS_DIRS = ["/etc/lab_creation/credentials", "lab_creation-credentials"]
_CLOUD_ACCOUNT_EXTS = [".yaml", ".yml", ".json", ".cfg"]

# Passphrases live here ONLY — process memory, this run's lifetime, never
# written anywhere. Keyed by the resolved account file's absolute path so a
# multi-node setup_lab.py run prompts for a given account's passphrase once,
# not once per node. clear_passphrase_cache() is a test hook; normal code
# never needs to call it.
_PASSPHRASE_CACHE = {}


def clear_passphrase_cache():
    _PASSPHRASE_CACHE.clear()


def credentials_dirs(config=None):
    """Search path for credential/cloud-account files: config["CREDENTIALS_PATH"]
    (a lab_creation.cfg key, added 2026-09-11) if set, else the built-in
    default + local dev fallback."""
    custom = (config or {}).get("CREDENTIALS_PATH")
    if custom:
        return [custom]
    return list(_DEFAULT_CREDENTIALS_DIRS)


def cloud_account_path(name, config=None):
    """First existing file for cloud account `name` — <dir>/<name>.<ext> across
    credentials_dirs(config) x _CLOUD_ACCOUNT_EXTS, or None."""
    for d in credentials_dirs(config):
        for ext in _CLOUD_ACCOUNT_EXTS:
            p = Path(d) / "{}{}".format(name, ext)
            if p.exists():
                return p
    return None


def list_cloud_accounts(config=None):
    """
    Every parseable credentials file across credentials_dirs(config), as
    (name, cloudtype) pairs. `cloudtype` is ALWAYS a plaintext top-level
    field even in an otherwise fully-encrypted file (see
    try_load_cloud_account()'s own docstring on the two encrypted shapes) —
    so this never decrypts anything and never prompts for a passphrase.

    Added 2026-09-12 to support automatic account discovery (see
    find_cloud_account_for_cloudtype() below) — before this, a cloud
    backend with no explicit "cloud_account" set silently fell back to
    plaintext lab_creation.cfg keys even when an encrypted credentials file
    for that same provider existed, which is what the encrypted-store
    feature was actually meant to replace.

    A file that fails to parse, isn't a mapping, or has no cloudtype field
    is skipped silently — inventorying what's usable, not validating every
    file in the directory; load_cloud_account() still gives the real error
    if a specific broken one is ever actually selected.
    """
    accounts = []
    seen = set()
    for d in credentials_dirs(config):
        dirp = Path(d)
        if not dirp.is_dir():
            continue
        for ext in _CLOUD_ACCOUNT_EXTS:
            for p in sorted(dirp.glob("*" + ext)):
                name = p.name[:-len(ext)]
                if name in seen:
                    continue
                try:
                    text = p.read_text()
                    if ext == ".json":
                        data = json.loads(text)
                    elif ext in (".yaml", ".yml"):
                        import yaml
                        data = yaml.safe_load(text)
                    else:
                        data = _parse_shell_vars(text)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                cloudtype = ""
                for k in data:
                    if k.lower() in ("cloudtype", "cloud_type"):
                        cloudtype = str(data[k] or "").strip()
                        break
                if cloudtype:
                    accounts.append((name, cloudtype))
                    seen.add(name)
    return accounts


def find_cloud_account_for_cloudtype(cloudtype, config=None):
    """
    Auto-discovery for backends.resolve_cloud_account()/effective_backend_name():
    when a cloud-backend node has no explicit "cloud_account" set, look for
    a credentials file matching `cloudtype` (e.g. "aws") instead of silently
    using plaintext lab_creation.cfg keys.

    Returns (name_or_None, all_matching_names):
      - exactly one match  -> (that name, [that name]): use it automatically.
      - no matches         -> (None, []): caller falls back to today's
        lab_creation.cfg behaviour — nothing changes for setups that never
        adopted the encrypted store.
      - more than one match -> (None, [name, ...]): genuinely ambiguous —
        the caller should die() with a clear message rather than guess
        which one was intended.
    """
    matches = sorted(name for name, ct in list_cloud_accounts(config) if ct == cloudtype)
    return (matches[0] if len(matches) == 1 else None), matches


def list_service_credentials(config=None):
    """
    Like list_cloud_accounts(), but for non-cloud external-service credentials
    (SCC, SUSE Application Collection, …) — added 2026-09-18 per explicit user
    request that /etc/lab_creation/credentials/ cover more than just cloud
    providers, while plaintext-in-lab-JSON remains fully valid either way.

    Every parseable file across credentials_dirs(config), as (name,
    credential_kind) pairs, reading a top-level 'credential_kind' (or
    'kind') field — deliberately a DIFFERENT marker key from cloud_account's
    own 'cloudtype', so the two concepts share the same directory/file
    format/encryption mechanism without ever colliding: a file is either a
    cloud account (has cloudtype) or a service credential (has
    credential_kind), never both. Same "skip anything unparseable or
    marker-less" contract as list_cloud_accounts() — inventorying what's
    usable, not validating every file in the directory.
    """
    creds = []
    seen = set()
    for d in credentials_dirs(config):
        dirp = Path(d)
        if not dirp.is_dir():
            continue
        for ext in _CLOUD_ACCOUNT_EXTS:
            for p in sorted(dirp.glob("*" + ext)):
                name = p.name[:-len(ext)]
                if name in seen:
                    continue
                try:
                    text = p.read_text()
                    if ext == ".json":
                        data = json.loads(text)
                    elif ext in (".yaml", ".yml"):
                        import yaml
                        data = yaml.safe_load(text)
                    else:
                        data = _parse_shell_vars(text)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = ""
                for k in data:
                    if k.lower() in ("credential_kind", "kind"):
                        kind = str(data[k] or "").strip()
                        break
                if kind:
                    creds.append((name, kind))
                    seen.add(name)
    return creds


def find_service_credential_for_kind(kind, config=None):
    """
    Auto-discovery for addon_common.resolve_credential(): when no explicit
    "<kind>_account" is set, look for a credentials file with this
    credential_kind instead of silently falling back to plaintext lab-JSON
    fields. Same (name_or_None, all_matching_names) contract as
    find_cloud_account_for_cloudtype() — exactly one match is used
    automatically, none falls back to plaintext, more than one is
    genuinely ambiguous and the caller should die() rather than guess.
    """
    matches = sorted(name for name, k in list_service_credentials(config) if k == kind)
    return (matches[0] if len(matches) == 1 else None), matches


def _decrypt_value(envelope, cache_key, label, passphrase_prompt, max_attempts=3):
    """
    Decrypt one crypto_store envelope dict, trying a cached passphrase for
    `cache_key` first, then prompting (via passphrase_prompt, defaulting to
    crypto_store.prompt_passphrase) up to max_attempts times. A passphrase
    that works is cached under cache_key for the rest of this process; a
    cached one that DOESN'T fit this particular envelope is evicted, not
    trusted blindly (lets one account's passphrase differ from another's
    even though both happen to be requested in the same run).

    Returns (plaintext_bytes, error) — exactly one is None. Never raises.
    """
    import crypto_store  # lazy: only needed once an ACTUALLY-encrypted value is hit
    prompt_fn = passphrase_prompt or crypto_store.prompt_passphrase

    if cache_key in _PASSPHRASE_CACHE:
        try:
            return crypto_store.decrypt_cascade(_PASSPHRASE_CACHE[cache_key], envelope), None
        except crypto_store.DecryptionError:
            del _PASSPHRASE_CACHE[cache_key]

    last_err = None
    for attempt in range(max_attempts):
        pw = prompt_fn("Passphrase for {}: ".format(label))
        try:
            plaintext = crypto_store.decrypt_cascade(pw, envelope)
        except crypto_store.DecryptionError as e:
            last_err = e
            if attempt < max_attempts - 1:
                print("Wrong passphrase for {} — {} attempt(s) left.".format(
                    label, max_attempts - attempt - 1))
            continue
        _PASSPHRASE_CACHE[cache_key] = pw
        return plaintext, None
    return None, "{}: {}".format(label, last_err)


def try_load_cloud_account(name, config=None, passphrase_prompt=None):
    """
    Non-dying load of a per-account credentials/cloud-account file: returns
    (data, error) where exactly one is None. `data`, when present, is a flat
    dict with the provider normalised to the key "CLOUDTYPE". Used by the
    preflight, which folds any error into its own issue list rather than
    aborting.

    Multiple cloud accounts, the same way KVM_HOSTS gives multiple hypervisors
    (see backends.resolve_cloud_account()). Looks under credentials_dirs(config)
    — default /etc/lab_creation/credentials, configurable via lab_creation.cfg's
    CREDENTIALS_PATH — for <name>.{yaml,yml,json,cfg}. The file carries a
    `cloudtype` (aws/gcp/hetzner/…) plus the same connection keys that
    provider's backend already reads from lab_creation.cfg (AWS_REGION, etc.).

    Encryption (added 2026-09-11 — see scripts/setup_credentials.py, which is
    the normal way to create these files, and libs/crypto_store.py for the
    actual cipher): encrypted by default. Two shapes are recognised, both
    written by setup_credentials.py:
      - Whole-file: a top-level "encrypted: true" with the crypto_store
        envelope fields alongside it; the decrypted plaintext is itself a
        YAML mapping of the real fields.
      - Field-level: only some values are themselves envelope dicts
        ({"encrypted": true, ...}) — the rest of the file stays plain text
        (e.g. AWS_REGION/AWS_PROFILE readable, AWS_SECRET_ACCESS_KEY boxed).
    A file can opt out entirely with a top-level "unencrypted: true" — no
    passphrase is ever prompted for one (the loader checks for "encrypted"/
    per-field envelopes, not "unencrypted", so an absent flag and an explicit
    unencrypted: true behave identically: only actually-encrypted content
    ever triggers a prompt).
    """
    p = cloud_account_path(name, config)
    if p is None:
        return None, ("cloud account '{}' not found — looked for <name>.{{{}}} in: {}".format(
            name, ",".join(e.lstrip(".") for e in _CLOUD_ACCOUNT_EXTS),
            ", ".join(credentials_dirs(config))))

    text = p.read_text()
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        elif p.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = _parse_shell_vars(text)
    except ImportError:
        return None, "cloud account '{}' ({}) needs PyYAML to parse — pip install pyyaml".format(name, p)
    except Exception as e:  # json.JSONDecodeError, yaml.YAMLError, …
        return None, "cloud account '{}' ({}) failed to parse as {}: {}".format(
            name, p, p.suffix.lstrip(".") or "config", e)

    if not isinstance(data, dict):
        return None, "cloud account '{}' ({}) must be a mapping of key: value".format(name, p)

    # Normalise the provider key: accept cloudtype / CLOUDTYPE / cloud_type.
    cloudtype = ""
    for k in list(data.keys()):
        if k.lower() in ("cloudtype", "cloud_type"):
            cloudtype = str(data.pop(k) or "").strip()
    if not cloudtype:
        return None, "cloud account '{}' ({}) has no 'cloudtype' — set it to aws, gcp, hetzner, …".format(name, p)

    data.pop("unencrypted", None)  # informational only — see docstring

    if data.get("encrypted") is True:
        plaintext, err = _decrypt_value(data, str(p), "cloud account '{}'".format(name), passphrase_prompt)
        if err:
            return None, err
        import yaml
        try:
            inner = yaml.safe_load(plaintext.decode("utf-8")) or {}
        except yaml.YAMLError as e:
            return None, "cloud account '{}' ({}) decrypted, but the plaintext isn't valid YAML: {}".format(
                name, p, e)
        if not isinstance(inner, dict):
            return None, "cloud account '{}' ({}) decrypted payload must be a mapping".format(name, p)
        data = inner
    else:
        # Field-level (mode B): any value that is itself an envelope dict
        # gets decrypted in place; everything else (region, profile name, …)
        # stays exactly as written.
        for key in list(data.keys()):
            val = data[key]
            if isinstance(val, dict) and val.get("encrypted") is True:
                plaintext, err = _decrypt_value(
                    val, "{}::{}".format(p, key), "{} ({})".format(key, name), passphrase_prompt)
                if err:
                    return None, err
                data[key] = plaintext.decode("utf-8")

    data["CLOUDTYPE"] = cloudtype
    return data, None


def load_cloud_account(name, config=None):
    """Dying wrapper over try_load_cloud_account() — used by get_backend()/
    effective_backend_name(), where a bad account reference (including a
    wrong passphrase after retries) is fatal."""
    data, error = try_load_cloud_account(name, config=config)
    if error:
        _die(error)
    return data


def try_load_service_credential(name, config=None, passphrase_prompt=None):
    """
    Non-dying load of a named external-service credential file (SCC,
    Application Collection, …) — the credential_kind-marked counterpart of
    try_load_cloud_account() above; see that function's own docstring for
    the two encrypted shapes this also recognises (whole-file vs
    field-level), the CREDENTIALS_PATH lookup, and the unencrypted:true
    opt-out — all identical here, just keyed off 'credential_kind'/'kind'
    instead of 'cloudtype'/'cloud_type'. Returns (data, error) where data,
    when present, is a flat dict with the kind normalised under the key
    "CREDENTIAL_KIND" and every other field exactly as the file (or its
    decrypted payload) defines it — no further translation: a kind's own
    canonical field names (e.g. "scc_user"/"scc_password" for kind "scc")
    are whatever the caller's field_map (see addon_common.resolve_credential())
    expects them to be.
    """
    p = cloud_account_path(name, config)
    if p is None:
        return None, ("credential '{}' not found — looked for <name>.{{{}}} in: {}".format(
            name, ",".join(e.lstrip(".") for e in _CLOUD_ACCOUNT_EXTS),
            ", ".join(credentials_dirs(config))))

    text = p.read_text()
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        elif p.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = _parse_shell_vars(text)
    except ImportError:
        return None, "credential '{}' ({}) needs PyYAML to parse — pip install pyyaml".format(name, p)
    except Exception as e:
        return None, "credential '{}' ({}) failed to parse as {}: {}".format(
            name, p, p.suffix.lstrip(".") or "config", e)

    if not isinstance(data, dict):
        return None, "credential '{}' ({}) must be a mapping of key: value".format(name, p)

    kind = ""
    for k in list(data.keys()):
        if k.lower() in ("credential_kind", "kind"):
            kind = str(data.pop(k) or "").strip()
    if not kind:
        return None, "credential '{}' ({}) has no 'credential_kind' — set it to scc, appcollection, …".format(
            name, p)

    data.pop("unencrypted", None)

    if data.get("encrypted") is True:
        plaintext, err = _decrypt_value(data, str(p), "credential '{}'".format(name), passphrase_prompt)
        if err:
            return None, err
        import yaml
        try:
            inner = yaml.safe_load(plaintext.decode("utf-8")) or {}
        except yaml.YAMLError as e:
            return None, "credential '{}' ({}) decrypted, but the plaintext isn't valid YAML: {}".format(
                name, p, e)
        if not isinstance(inner, dict):
            return None, "credential '{}' ({}) decrypted payload must be a mapping".format(name, p)
        data = inner
    else:
        for key in list(data.keys()):
            val = data[key]
            if isinstance(val, dict) and val.get("encrypted") is True:
                plaintext, err = _decrypt_value(
                    val, "{}::{}".format(p, key), "{} ({})".format(key, name), passphrase_prompt)
                if err:
                    return None, err
                data[key] = plaintext.decode("utf-8")

    data["CREDENTIAL_KIND"] = kind
    return data, None


def load_service_credential(name, config=None):
    """Dying wrapper over try_load_service_credential() — used by
    addon_common.resolve_credential(), where a bad account reference
    (including a wrong passphrase after retries) is fatal."""
    data, error = try_load_service_credential(name, config=config)
    if error:
        _die(error)
    return data


def _load_shell_vars_file(search_paths, name):
    for candidate in search_paths:
        p = Path(candidate)
        if p.exists():
            return _parse_shell_vars(p.read_text())
    _die("Configuration file '{}' not found in: {}".format(name, ", ".join(search_paths)))


_VAR_REF_RE = re.compile(r'\$\{(\w+)\}|\$(\w+)')


def _parse_shell_vars(text):
    """
    Parse simple KEY=value or KEY="value" assignments from a bash config file.
    Skips comments, declare statements, arrays, and command substitutions.

    Expands ${VAR}/$VAR references to previously-parsed keys in the same file
    (sequential, like bash `source` — e.g. the real lab_creation.cfg.example
    ships `VIRT_SRV="qemu+ssh://root@${REMOTE_HOST}/system?..."`, which relies
    on REMOTE_HOST already having been assigned earlier in the same file).
    Falls back to the process environment for anything not defined earlier in
    the file, matching a sourced script's actual variable scope. An
    unresolvable reference is left as-is rather than raising.

    Returns a dict.
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if any(line.startswith(kw) for kw in ("declare", "export declare", "typeset")):
            continue
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw.startswith("(") or "$(" in raw or "`" in raw:
            continue
        if raw and raw[0] in ('"', "'"):
            # Quoted value: the value ends at the matching closing quote —
            # anything after that (e.g. a trailing " # comment") is not part
            # of it. A naive "first char == last char" check breaks on lines
            # like `KEY='value' # comment`, since the line's last character
            # is then the comment's, not the closing quote.
            quote = raw[0]
            end = raw.find(quote, 1)
            raw = raw[1:end] if end != -1 else raw[1:]
        else:
            # Unquoted: bash treats " #" (space then hash) as the start of a
            # trailing comment.
            comment_at = raw.find(" #")
            if comment_at != -1:
                raw = raw[:comment_at].rstrip()

        def _expand(m):
            name = m.group(1) or m.group(2)
            if name in result:
                return result[name]
            return os.environ.get(name, m.group(0))

        raw = _VAR_REF_RE.sub(_expand, raw)
        result[key] = raw
    return result


# ── Validation ────────────────────────────────────────────────────────────────

def validate_definition(definition, path):
    """
    Check a loaded lab definition for obvious structural errors.
    Raises SystemExit on fatal problems; prints warnings for non-fatal issues.
    """
    if not definition.get("nodes"):
        _die("'{}' has no 'nodes' section".format(path))

    if "cluster" in definition and "kclusters" not in definition:
        _warn(
            "'{}' uses the legacy single-cluster 'cluster' format. "
            "K8s setup requires the 'kclusters' + per-node 'kcluster' format.".format(path)
        )

    kclusters = definition.get("kclusters", {})
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        ref = node_cfg.get("kcluster", "")
        if ref and ref not in kclusters:
            _die(
                "Node '{}' references kcluster '{}' "
                "which is not defined in 'kclusters'".format(vm_name, ref)
            )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _die(msg):
    print("\033[1;91mERROR:\033[0m {}".format(msg), file=sys.stderr)
    raise SystemExit(1)


def _warn(msg):
    print("\033[1;33mWARNING:\033[0m {}".format(msg), file=sys.stderr)
