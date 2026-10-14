#!/usr/bin/env python3.11
# Part of lab-in-a-box — creates/encrypts cloud-provider and external-service
# credential files
# Author/s: Raul Mahiques
# License: GPLv3

"""
setup_credentials.py — create or encrypt a cloud-provider OR external-service
(SCC, SUSE Application Collection, …) credentials file under
/etc/lab_creation/credentials/ (configurable via lab_creation.cfg's
CREDENTIALS_PATH). See libs/primary.py's try_load_cloud_account()/
try_load_service_credential() for how these are read back, and
libs/crypto_store.py for the actual cipher. Plaintext-in-lab-JSON remains
fully valid for either kind of credential — this store is an optional
alternative, never a requirement.

Usage:
    setup_credentials.py
        Interactive: pick cloud provider or external service, fill in its
        fields, write a new <provider-or-kind>-<account>.yaml, encrypted by
        default.

    setup_credentials.py --encrypt-existing <file>
        Encrypt the sensitive fields (password/secret/token-shaped keys) of
        an already-written plaintext credentials file. Never overwrites the
        input — writes "<file-without-ext>.encrypted.yaml" alongside it.
"""

__version__ = "1"

# Version of the credentials-file schema (PROVIDER_FIELDS and SERVICE_CREDENTIAL_FIELDS), written to every file as
# schema_version; see schemas/compatibility.json.
CREDENTIALS_SCHEMA_VERSION = "1.0"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import crypto_store  # noqa: E402
from lab_creation import die  # noqa: E402

_HELP_TEXT = __doc__

# One row per provider: (KEY, required, sensitive). "sensitive" fields are
# entered with no terminal echo (crypto_store.prompt_passphrase) and are what
# --encrypt-existing looks for by name (see SENSITIVE_HINTS below) — this
# table is the authoritative list for the interactive builder; sizing-table
# overrides (*_INSTANCE_TYPES/*_SERVER_TYPES/*_PLANS) are deliberately left
# out here — those aren't credentials, set them in lab_creation.cfg directly
# if wanted (see README's Compute backends table).
PROVIDER_FIELDS = {
    "hetzner": [
        ("HETZNER_TOKEN", True, True),
        ("HETZNER_LOCATION", False, False),
    ],
    "aws": [
        ("AWS_REGION", True, False),
        ("AWS_PROFILE", False, False),
        ("AWS_ACCESS_KEY_ID", False, False),
        ("AWS_SECRET_ACCESS_KEY", False, True),
        ("AWS_SESSION_TOKEN", False, True),
        ("AWS_SUBNET_ID", False, False),
        ("AWS_SECURITY_GROUP_ID", False, False),
        ("AWS_KEY_NAME", False, False),
    ],
    "gcp": [
        ("GCP_PROJECT", True, False),
        ("GCP_ZONE", True, False),
        ("GCP_SERVICE_ACCOUNT_KEY", False, False),  # a file path, not a secret value itself
        ("GCP_IMAGE_PROJECT", False, False),
        ("GCP_NETWORK", False, False),
        ("GCP_SUBNET", False, False),
    ],
    "alibaba": [
        ("ALIBABA_ACCESS_KEY_ID", True, False),
        ("ALIBABA_ACCESS_KEY_SECRET", True, True),
        ("ALIBABA_REGION", True, False),
        ("ALIBABA_SECURITY_GROUP_ID", True, False),
        ("ALIBABA_VSWITCH_ID", True, False),
    ],
    "scaleway": [
        ("SCALEWAY_SECRET_KEY", True, True),
        ("SCALEWAY_PROJECT_ID", True, False),
        ("SCALEWAY_ZONE", True, False),
    ],
    "upcloud": [
        ("UPCLOUD_USERNAME", True, False),
        ("UPCLOUD_PASSWORD", True, True),
        ("UPCLOUD_ZONE", True, False),
    ],
    "ovhcloud": [
        ("OVH_APPLICATION_KEY", True, True),
        ("OVH_APPLICATION_SECRET", True, True),
        ("OVH_CONSUMER_KEY", True, True),
        ("OVH_SERVICE_NAME", True, False),
        ("OVH_REGION", True, False),
        ("OVH_ENDPOINT", False, False),
    ],
    "exoscale": [
        ("EXOSCALE_API_KEY", True, True),
        ("EXOSCALE_API_SECRET", True, True),
        ("EXOSCALE_ZONE", True, False),
    ],
}

# External-SERVICE credentials (SCC, SUSE Application Collection, …).
# Same file format/directory/encryption as
# PROVIDER_FIELDS above, but marked with a "credential_kind" field instead
# of "cloudtype" (see libs/primary.py's find_service_credential_for_kind()/
# libs/addon_common.py's resolve_credential()) so the two concepts never
# collide: a file is either a cloud account or a service credential, never
# both. One credential file per kind is reusable across every addon that
# needs it (e.g. a single "scc" file for install_smlm.py's smlm_scc_user
# AND any other addon that later wants SCC credentials too), regardless of
# each addon's own JSON-field prefix — see each field's own canonical name.
SERVICE_CREDENTIAL_FIELDS = {
    "scc": [
        ("scc_user", True, False),
        ("scc_password", True, True),
        ("scc_regcode", False, True),
    ],
    "appcollection": [
        ("appcollection_user", True, False),
        ("appcollection_password", True, True),
    ],
    "hermes": [
        ("hermes_llm_api_key", True, True),
        ("hermes_telegram_token", False, True),
        ("hermes_dashboard_password", False, True),
    ],
    "ds389": [
        ("ds389_dm_password", False, True),
    ],
    "vhm_aws": [
        ("vhm_aws_access_key_id", True, False),
        ("vhm_aws_secret_access_key", True, True),
    ],
}

# --encrypt-existing's own heuristic for "this plaintext value looks like a
# secret" — an allowlist table like PROVIDER_FIELDS isn't available there
# (the input file might not even name a known provider), so this falls back
# to matching on the key's own name.
SENSITIVE_HINTS = ("SECRET", "PASSWORD", "_TOKEN", "APPLICATION_KEY",
                   "APPLICATION_SECRET", "CONSUMER_KEY", "ACCESS_KEY_SECRET")


def _looks_sensitive(key):
    up = key.upper()
    return any(h in up for h in SENSITIVE_HINTS)


def _prompt_field(key: str, required: bool, sensitive: bool) -> str:
    """One field prompt — masked (no echo) if sensitive, plain input() otherwise.
    Loops until a required field gets a non-empty answer."""
    suffix = "" if required else " (optional, Enter to skip)"
    prompt = "  {}{}: ".format(key, suffix)
    while True:
        val = crypto_store.prompt_passphrase(prompt) if sensitive else input(prompt).strip()
        if val or not required:
            return val
        print("    {} is required.".format(key))


def _choose(title: str, names: list) -> str:
    """Print `names` numbered under `title` and return the one the user picks; die on an invalid choice."""
    print(title)
    for i, name in enumerate(names, 1):
        print("  {}) {}".format(i, name))
    choice = input("Choice: ").strip()
    try:
        return names[int(choice) - 1]
    except (ValueError, IndexError):
        die("Invalid choice '{}'".format(choice))


def _prompt_fields(name: str, rows: list) -> dict:
    """Prompt for every (key, required, sensitive) row of `name`; only the fields filled in."""
    print("\nEnter credentials for '{}':".format(name))
    fields = {}
    for key, required, sensitive in rows:
        val = _prompt_field(key, required, sensitive)
        if val:
            fields[key] = val
    return fields


def build_account_fields(provider: str) -> dict:
    """Prompt for every field PROVIDER_FIELDS[provider] lists; only the fields filled in."""
    return _prompt_fields(provider, PROVIDER_FIELDS[provider])


def build_service_fields(kind: str) -> dict:
    """Prompt for every field SERVICE_CREDENTIAL_FIELDS[kind] lists; only the fields filled in."""
    return _prompt_fields(kind, SERVICE_CREDENTIAL_FIELDS[kind])


def _write_file(out_path: Path, marker: dict, fields: dict, encrypt: bool, passphrase: str = None) -> Path:
    """
    Write a credentials YAML at `out_path` (mode 0600): `marker` (the cloudtype or credential_kind) and the schema
    version, then `fields`, whole-file encrypted when `encrypt` (the passphrase is prompted for when not given).
    """
    import yaml

    payload = dict(marker, schema_version=CREDENTIALS_SCHEMA_VERSION)
    if encrypt:
        if passphrase is None:
            passphrase = crypto_store.prompt_passphrase("Set a passphrase for this file: ", confirm=True)
        plaintext = yaml.safe_dump(fields, sort_keys=False).encode("utf-8")
        payload.update(crypto_store.encrypt_cascade(passphrase, plaintext))
    else:
        payload["unencrypted"] = True
        payload.update(fields)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    out_path.chmod(0o600)
    return out_path


def write_account_file(out_path: Path, provider: str, fields: dict, encrypt: bool, passphrase: str = None) -> Path:
    """Write a cloud-account credentials file for `provider`; see _write_file()."""
    return _write_file(out_path, {"cloudtype": provider}, fields, encrypt, passphrase)


def write_credential_file(out_path: Path, kind: str, fields: dict, encrypt: bool, passphrase: str = None) -> Path:
    """Write an external-service credentials file of `kind`; see _write_file()."""
    return _write_file(out_path, {"credential_kind": kind}, fields, encrypt, passphrase)


def _interactive(credentials_dir: str, title: str, noun: str, table: dict, write, reference: str) -> Path:
    """
    Prompt for an entry of `table` (picked under `title`), an account name and every field, then write
    <entry>-<account>.yaml in `credentials_dir` with `write` (encrypted unless declined). `noun` names the entry in the
    file-name hint; `reference` is the hint printed after writing, formatted with the file's stem and the entry.
    """
    name = _choose(title, sorted(table))
    account_name = input("Account name (the file will be named <{}>-<name>.yaml — that whole name is what "
                         "you'll reference in the lab JSON): ".format(noun)).strip()
    if not account_name:
        die("An account name is required")
    stem = "{}-{}".format(name, account_name)
    out_path = Path(credentials_dir) / "{}.yaml".format(stem)
    if out_path.exists() and input("{} already exists — overwrite? [y/N] ".format(out_path)).strip().lower() != "y":
        die("Aborted — not overwriting {}".format(out_path))
    fields = _prompt_fields(name, table[name])
    encrypt = input("\nEncrypt this file? [Y/n] ").strip().lower() != "n"
    write(out_path, name, fields, encrypt)
    print("\nWrote {} ({}).".format(out_path, "encrypted" if encrypt else "UNENCRYPTED"))
    print(reference.format(stem=stem, name=name))
    return out_path


def interactive_build(credentials_dir: str) -> Path:
    """Mode A: a cloud-provider credentials file, <provider>-<account>.yaml, referenced as cloud_account."""
    return _interactive(credentials_dir, "Provider type:", "provider", PROVIDER_FIELDS, write_account_file,
                        'Reference it in your lab JSON as:  "cloud_account": "{stem}"')


def interactive_build_service(credentials_dir: str) -> Path:
    """Mode C: an external-service credentials file, <kind>-<account>.yaml, referenced e.g. as smlm_scc_account."""
    return _interactive(credentials_dir, "Service credential kind:", "kind", SERVICE_CREDENTIAL_FIELDS, write_credential_file,
                        "Reference it in your lab JSON as, e.g.:  \"smlm_scc_account\": \"{stem}\"  (or leave unset "
                        "to auto-discover it when it is the only '{name}' credential file)")


def encrypt_existing(path):
    """Mode B: encrypt the sensitive-looking fields of an already-written
    plaintext credentials file, in place value-by-value, leaving everything
    else readable. Never overwrites the input file — writes
    "<name-without-ext>.encrypted.yaml" alongside it (same
    never-touch-the-original discipline as primary.save_definition())."""
    import yaml

    p = Path(path)
    if not p.exists():
        die("{} not found".format(p))
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        die("{} is not valid YAML: {}".format(p, e))
    if not isinstance(data, dict):
        die("{} must be a mapping of key: value".format(p))
    if data.get("encrypted") is True:
        die("{} is already whole-file encrypted".format(p))

    already = [k for k, v in data.items() if isinstance(v, dict) and v.get("encrypted") is True]
    to_encrypt = sensitive_plaintext_fields(data)

    if not to_encrypt:
        print("No plaintext sensitive-looking fields found in {} "
              "(looked for names containing: {}).".format(p, ", ".join(SENSITIVE_HINTS)))
        if already:
            print("Already encrypted: {}".format(", ".join(already)))
        return None

    print("Will encrypt these fields in {}:".format(p))
    for k in to_encrypt:
        print("  - {}".format(k))
    if input("Proceed? [y/N] ").strip().lower() != "y":
        die("Aborted")

    passphrase = crypto_store.prompt_passphrase("Set a passphrase for these fields: ", confirm=True)
    out_path = write_encrypted_copy(p, data, to_encrypt, passphrase)
    print("\nWrote {} — review it, then move it into place yourself "
          "(the original is left untouched).".format(out_path))
    return out_path


def sensitive_plaintext_fields(data):
    """The keys of credentials mapping `data` whose plaintext values look like secrets."""
    return [k for k, v in data.items()
            if isinstance(v, str) and k not in ("cloudtype", "credential_kind") and _looks_sensitive(k)]


def write_encrypted_copy(path, data, keys, passphrase):
    """Write "<stem>.encrypted.yaml" next to credentials file `path`: mapping `data`
    with the values of `keys` encrypted with `passphrase`. Returns the new path."""
    import yaml

    for k in keys:
        data[k] = crypto_store.encrypt_cascade(passphrase, data[k].encode("utf-8"))
    out_path = path.parent / "{}.encrypted.yaml".format(path.stem)
    out_path.write_text(yaml.safe_dump(data, sort_keys=False))
    out_path.chmod(0o600)
    return out_path


def main():
    args = sys.argv[1:]

    if primary.wants_help(args):
        print(_HELP_TEXT)
        sys.exit(0)
    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if args and args[0] == "--encrypt-existing":
        if len(args) < 2:
            die("Usage: setup_credentials.py --encrypt-existing <file>")
        try:
            encrypt_existing(args[1])
        except (EOFError, KeyboardInterrupt):
            die("\nAborted (no more input).")
        return

    if args:
        die("Unknown argument '{}' — see --help".format(args[0]))

    config = primary.load_config()
    credentials_dir = primary.credentials_dirs(config)[0]
    # EOFError (stdin ran out / piped input too short / Ctrl-D) and
    # KeyboardInterrupt (Ctrl-C) are real, expected ways for an interactive
    # prompt sequence to end early.
    # Both are caught, so the prompt ends in a clean abort instead of a raw Python traceback.
    try:
        print("What kind of credential?")
        print("  1) Cloud provider (creates and manages VMs, as cloud_account)")
        print("  2) External service (SCC, SUSE Application Collection, … — e.g. smlm_scc_account)")
        choice = input("Choice: ").strip()
        if choice == "2":
            interactive_build_service(credentials_dir)
        else:
            interactive_build(credentials_dir)
    except (EOFError, KeyboardInterrupt):
        die("\nAborted (no more input).")


if __name__ == "__main__":
    main()
