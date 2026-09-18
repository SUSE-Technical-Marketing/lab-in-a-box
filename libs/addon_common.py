"""
addon_common.py — shared CLI scaffolding for install_<addon>.py scripts.

Every install_<addon> script (bash and now python) follows the same pattern:
--version/-v, --validate <json>, --help (prints the script's own "JSON
section:" doc comment), and --input-definition/--schema (delegates to
lab_schema, which parses that same comment block — language-agnostic, since
it just reads "#"-prefixed lines, so it works unchanged on these .py ports).
Extracted here once instead of duplicated in all 40 scripts, mirroring how
the bash versions duplicate ~30 lines of identical _vreq/_vns/_vver/_vurl/
_vbool/_vport function bodies in every single script.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import importlib.util
import json
import re
import shutil
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import primary


# ── Field validators ──────────────────────────────────────────────────────────
# Mirror bash's _vreq/_vns/_vver/_vurl/_vbool/_vport exactly (same jq `// ""`
# semantics via .get(key, ""), same regexes, same [ERROR] message text).

class Validator:
    """
    Accumulates [ERROR] lines against a loaded definition dict, mirroring the
    bash _vf/_ve/_vreq/_vns/_vver/_vurl/_vbool/_vport closures.

    errors : list of "[ERROR] ..." strings printed by run_validate().
    """

    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def _get(self, section, field):
        return (self.definition.get(section, {}) or {}).get(field, "") or ""

    def vreq(self, section, field):
        """Mirrors _vreq: <section>.<field> is required (no default)."""
        if not self._get(section, field):
            self.errors.append("[ERROR] {}.{} is required (no default)".format(section, field))

    def vns(self, section):
        """Mirrors _vns: <section>.<section>_ns, if set, must be a valid k8s namespace."""
        v = self._get(section, "{}_ns".format(section))
        if v and not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_ns='{1}': invalid namespace (lowercase, alphanumeric, hyphens only)".format(
                    section, v))

    def vver(self, section):
        """Mirrors _vver: <section>.<section>_version, if set, must look like X.Y…"""
        v = self._get(section, "{}_version".format(section))
        if v and not re.match(r'^[0-9]+\.[0-9]', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_version='{1}': not a valid version (expected X.Y…)".format(section, v))

    def vurl(self, section):
        """Mirrors _vurl: <section>.<section>_repo_url, if set, must start with http(s)://"""
        v = self._get(section, "{}_repo_url".format(section))
        if v and not re.match(r'^https?://', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_repo_url='{1}': must start with https://".format(section, v))

    def vbool(self, section, field):
        """Mirrors _vbool: <section>.<field>, if set, must be 'true' or 'false'."""
        v = self._get(section, field)
        if v and str(v) not in ("true", "false"):
            self.errors.append("[ERROR] {}.{}='{}': must be 'true' or 'false'".format(section, field, v))

    def vport(self, section, field):
        """Mirrors _vport: <section>.<field>, if set, must be a bare integer."""
        v = self._get(section, field)
        if v and not re.match(r'^[0-9]+$', str(v)):
            self.errors.append("[ERROR] {}.{}='{}': must be a port number".format(section, field, v))

    def vreq_or_credential(self, section, field, kind, account_field=None):
        """
        Like vreq(), but also satisfied by the external-service credential
        store (see resolve_credential() below) — added 2026-09-18. Passes if
        EITHER the plaintext <section>.<field> is set, OR <section>.
        <account_field> (default "<kind>_account") names an explicit
        credential file (trusted without decrypting it here — --validate
        never prompts for a passphrase; a bad reference still fails loudly
        at real run time), OR exactly one credential_kind==kind file is
        auto-discoverable. Only errors if none of the three apply.
        """
        if self._get(section, field):
            return
        account_field = account_field or "{}_account".format(kind)
        if self._get(section, account_field):
            return
        _, matches = primary.find_service_credential_for_kind(kind)
        if len(matches) == 1:
            return
        self.errors.append(
            "[ERROR] {0}.{1} is required (no default) — set it directly, set {0}.{2} to a real "
            "'{3}' credential file, or leave both unset with exactly one '{3}' credential file "
            "in the store to auto-discover".format(section, field, account_field, kind))


def require_k8s_name(cfg, field, default):
    """
    Read an addon-config value used as (or interpolated into) a Kubernetes
    namespace/resource name, and validate it at RUNTIME against the same
    lowercase-alphanumeric-plus-hyphens shape Validator.vns() checks.

    Found in code review 2026-09-05: many install_<addon>.py scripts read a
    "*_ns" (or similarly-shaped resource-name) config value and interpolate
    it, unquoted, directly into remote kubectl/shell commands run over
    ssh_run() — and Validator.vns()'s own format check is never actually
    invoked by the real deploy pipeline (setup_lab.py only calls the
    VM-level validate_lab_definition(), never each addon's own --validate).
    A value containing a shell metacharacter would otherwise reach a real
    remote shell unescaped. Exits with a clear error instead of silently
    interpolating something dangerous — mirrors lab_creation.die()'s
    message style, but kept dependency-free here (addon_common has never
    imported lab_creation) rather than adding a new inter-lib coupling.
    """
    v = str(cfg.get(field) or default)
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', v):
        print("[ERROR] {} = '{}' is invalid — must be a valid Kubernetes name "
              "(lowercase alphanumeric and hyphens only)".format(field, v), file=sys.stderr)
        sys.exit(1)
    return v


def run_validate(json_path, check_fn):
    """
    Load json_path (JSON or YAML, auto-detected — see primary.try_load_definition),
    run check_fn(Validator) to populate errors, print them, and return the
    error count as an exit code — mirrors bash's `exit ${_ve}`.
    """
    definition, parse_error = primary.try_load_definition(json_path)
    if parse_error:
        print("[ERROR] {}".format(parse_error))
        return 1
    v = Validator(definition)
    check_fn(v)
    for line in v.errors:
        print(line)
    return len(v.errors)


# ── --help (self-documenting from the script's own header comment) ──────────

def print_help(script_path, usage=None):
    """
    Mirrors bash's --help: prints a usage line, then re-emits this script's
    own "# JSON section: ..." comment block verbatim (same block lab_schema
    parses for --input-definition/--schema).
    """
    name = Path(script_path).name
    print(usage or "Usage: {} <lab.json> [<vm_name>]".format(name))
    print()
    text = Path(script_path).read_text(errors="replace")
    in_block = False
    for line in text.splitlines():
        if not in_block:
            if line.lstrip().startswith("#") and "JSON section:" in line:
                in_block = True
            else:
                continue
        elif not line.lstrip().startswith("#"):
            break
        # The line that triggers in_block is deliberately printed too (falls
        # through here rather than `continue`), matching bash's awk rule
        # ordering exactly (`/pattern/{p=1}` doesn't skip the rest of that
        # line's rules, including the final `p{print}`).
        print(re.sub(r'^#\s?', '', line))


_lab_schema_mod = None


def _lab_schema():
    """
    lab_schema, loaded in-process (found via PATH, same resolution
    subprocess.run(["lab_schema", ...]) used before this) — an explicit
    SourceFileLoader is required since a deployed lab_schema has no .py
    suffix, same reason apps.py needs one for install_<addon> scripts.
    Cached like apps.py's own plugin cache.
    """
    global _lab_schema_mod
    if _lab_schema_mod is None:
        exe = shutil.which("lab_schema")
        if not exe:
            print("[ERROR] lab_schema not found in PATH", file=sys.stderr)
            sys.exit(1)
        loader = SourceFileLoader("lab_schema", exe)
        spec = importlib.util.spec_from_loader("lab_schema", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _lab_schema_mod = mod
    return _lab_schema_mod


def print_schema(script_path, fmt, plugin=None):
    """
    Mirrors bash's --input-definition/--schema: parses script_path's schema
    via lab_schema (in-process, not a subprocess — needed so plugin's
    capabilities can be merged in before printing), attaches plugin's
    capabilities (targets/layers/requires_kubernetes/aux_services — see
    apps.attach_capabilities()) alongside the schema's own fields, then
    emits in the requested format exactly as lab_schema's own CLI would.
    """
    import apps  # deferred: avoids a hard dependency for callers that never hit --schema

    ls = _lab_schema()
    schema = ls.parse_script(script_path)
    apps.attach_capabilities(schema, plugin or {})
    ls._emit(schema, fmt)
    return 0


# ── Top-level dispatch ────────────────────────────────────────────────────────

def handle_common_args(script_path, version, validate_fn=None, usage=None, plugin=None):
    """
    Handle --version/-v, --validate <json>, --help, --input-definition/
    --schema, --capabilities if present as sys.argv[1] (mirrors the identical
    dispatch block at the top of every install_<addon> script). Exits the
    process if one of these matched; otherwise returns None so the caller
    proceeds normally.

    validate_fn : callable(Validator) used for --validate; if None, --validate
                  always exits 0 (mirrors a script whose block is just
                  `exit ${_ve}` with no checks — several addons have exactly
                  this, e.g. install_uyuni, install_suma, install_wordpress).
    plugin      : this script's PLUGIN dict (see apps.py), printed as JSON by
                  --capabilities. Pass explicitly rather than having this
                  module go looking for it on the caller's module — every
                  addon already imports addon_common and calls this at the
                  top of main(), so it has PLUGIN in scope to pass. Scripts
                  that don't pass one (not yet classified) print "{}".
    """
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-v"):
        print("{} {}".format(Path(script_path).name, version))
        sys.exit(0)

    if argv and argv[0] == "--validate":
        if len(argv) < 2:
            print("[ERROR] --validate requires a JSON file path")
            sys.exit(1)
        if validate_fn is None:
            sys.exit(0)
        sys.exit(run_validate(argv[1], validate_fn))

    if argv and argv[0] == "--help":
        print_help(script_path, usage=usage)
        sys.exit(0)

    if argv and argv[0] in ("--input-definition", "--schema"):
        fmt = argv[1] if len(argv) > 1 else "json"
        sys.exit(print_schema(script_path, fmt, plugin=plugin))

    if argv and argv[0] == "--capabilities":
        print(json.dumps(plugin or {}, indent=2))
        sys.exit(0)


# ── External-service credentials ────────────────────────────────────────────

def resolve_credential(cfg, kind, field_map, account_key=None, config=None):
    """
    Resolves a named external-service credential (SCC, SUSE Application
    Collection, …) from /etc/lab_creation/credentials/ the same way
    backends.resolve_cloud_account() already does for cloud providers —
    added 2026-09-18 per explicit user request. An addon's own plaintext
    lab-JSON fields remain fully valid and are the fallback whenever no
    matching credentials file is used; this is purely additive.

    Resolution order:
      1. cfg.get(account_key) (default "<kind>_account") names a real
         credentials file with credential_kind == kind -> use it.
      2. No explicit name, but exactly one such file exists -> auto-use it
         (mirrors resolve_cloud_account()'s own auto-discovery).
      3. No explicit name and more than one matching file exists ->
         genuinely ambiguous, die() rather than guess.
      4. Otherwise (no explicit name, zero matching files) -> every field
         comes straight from cfg via field_map, exactly as before this
         feature existed.

    kind      : credential_kind to look for, e.g. "scc", "appcollection".
    field_map : {canonical_field: cfg_key} — canonical_field is the kind's
                own credential-file field name (e.g. "scc_user"), cfg_key is
                this addon's own lab-JSON field (e.g. "smlm_scc_user") that
                a plaintext fallback (or a mismatched/missing credential
                file's own field) reads. One credential file's canonical
                fields work unchanged across every addon that needs that
                kind, regardless of each addon's own JSON-field prefix.
    account_key : JSON field naming an explicit account (default
                "<kind>_account"). Only meaningful if the caller's own
                schema documents it.

    Returns {canonical_field: value} — every field_map key present, ""
    where genuinely unset everywhere.
    """
    from lab_creation import die

    account_key = account_key or "{}_account".format(kind)
    name = cfg.get(account_key)
    if not name:
        found, matches = primary.find_service_credential_for_kind(kind, config)
        if len(matches) > 1:
            die("multiple '{}' credential files match ({}) — set '{}' explicitly".format(
                kind, ", ".join(matches), account_key))
        name = found

    if name:
        data = primary.load_service_credential(name, config)
        return {canon: (data.get(canon) or cfg.get(cfg_key) or "")
                for canon, cfg_key in field_map.items()}
    return {canon: (cfg.get(cfg_key) or "") for canon, cfg_key in field_map.items()}
