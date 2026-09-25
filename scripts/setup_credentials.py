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

# External-SERVICE credentials (SCC, SUSE Application Collection, …) —
# added 2026-09-18. Same file format/directory/encryption as
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


def _prompt_field(key, required, sensitive):
    """One field prompt — masked (no echo) if sensitive, plain input() otherwise.
    Loops until a required field gets a non-empty answer."""
    suffix = "" if required else " (optional, Enter to skip)"
    prompt = "  {}{}: ".format(key, suffix)
    while True:
        val = crypto_store.prompt_passphrase(prompt) if sensitive else input(prompt).strip()
        if val or not required:
            return val
        print("    {} is required.".format(key))


def _choose_provider():
    providers = sorted(PROVIDER_FIELDS)
    print("Provider type:")
    for i, name in enumerate(providers, 1):
        print("  {}) {}".format(i, name))
    choice = input("Choice: ").strip()
    try:
        return providers[int(choice) - 1]
    except (ValueError, IndexError):
        die("Invalid choice '{}'".format(choice))


def build_account_fields(provider):
    """Prompt for every field PROVIDER_FIELDS[provider] lists. Returns a dict
    of only the fields that were actually filled in (an unanswered optional
    field is simply absent, same as never having set it)."""
    print("\nEnter credentials for '{}':".format(provider))
    fields = {}
    for key, required, sensitive in PROVIDER_FIELDS[provider]:
        val = _prompt_field(key, required, sensitive)
        if val:
            fields[key] = val
    return fields


def write_account_file(out_path, provider, fields, encrypt):
    """The one place that actually assembles + writes a credentials YAML —
    used by both interactive_build() and (indirectly, via crypto_store)
    anything else that ever needs to produce one."""
    import yaml

    payload = {"cloudtype": provider}
    if encrypt:
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


def _choose_kind():
    kinds = sorted(SERVICE_CREDENTIAL_FIELDS)
    print("Service credential kind:")
    for i, name in enumerate(kinds, 1):
        print("  {}) {}".format(i, name))
    choice = input("Choice: ").strip()
    try:
        return kinds[int(choice) - 1]
    except (ValueError, IndexError):
        die("Invalid choice '{}'".format(choice))


def build_service_fields(kind):
    """Like build_account_fields(), for SERVICE_CREDENTIAL_FIELDS[kind]."""
    print("\nEnter credentials for '{}':".format(kind))
    fields = {}
    for key, required, sensitive in SERVICE_CREDENTIAL_FIELDS[kind]:
        val = _prompt_field(key, required, sensitive)
        if val:
            fields[key] = val
    return fields


def write_credential_file(out_path, kind, fields, encrypt):
    """Like write_account_file(), but marks the file with 'credential_kind'
    instead of 'cloudtype' — see SERVICE_CREDENTIAL_FIELDS's own comment."""
    import yaml

    payload = {"credential_kind": kind}
    if encrypt:
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


def interactive_build_service(credentials_dir):
    """Mode C: prompt for credential kind + account name + every field, then
    write <kind>-<account>.yaml (encrypted unless declined)."""
    kind = _choose_kind()

    account_name = input("Account name (the file will be named "
                         "<kind>-<name>.yaml — that whole name is what "
                         "you'll put in, e.g., smlm_scc_account): ").strip()
    if not account_name:
        die("An account name is required")

    stem = "{}-{}".format(kind, account_name)
    out_path = Path(credentials_dir) / "{}.yaml".format(stem)
    if out_path.exists():
        if input("{} already exists — overwrite? [y/N] ".format(out_path)).strip().lower() != "y":
            die("Aborted — not overwriting {}".format(out_path))

    fields = build_service_fields(kind)
    encrypt = input("\nEncrypt this file? [Y/n] ").strip().lower() != "n"

    write_credential_file(out_path, kind, fields, encrypt)
    print("\nWrote {} ({}).".format(out_path, "encrypted" if encrypt else "UNENCRYPTED"))
    print("Reference it in your lab JSON as, e.g.:  \"smlm_scc_account\": \"{}\"  (or leave unset "
          "to auto-discover it, since it's the only '{}' credential file)".format(stem, kind))
    return out_path


def interactive_build(credentials_dir):
    """Mode A: prompt for provider + account name + every field, then write
    <provider>-<account>.yaml (encrypted unless declined)."""
    provider = _choose_provider()

    # The filename is "<provider>-<account_name>.yaml", and it's that WHOLE
    # stem — not just what's typed here — that a lab JSON's cloud_account
    # must reference. Found live 2026-09-11: without saying so explicitly at
    # both ends (the prompt AND the confirmation after writing), it's easy
    # to type "myaccount" here and then, just as naturally, write
    # "cloud_account": "myaccount" in the lab JSON — which doesn't exist.
    account_name = input("Account name (the file will be named "
                         "<provider>-<name>.yaml — that whole name is what "
                         "you'll put in cloud_account): ").strip()
    if not account_name:
        die("An account name is required")

    stem = "{}-{}".format(provider, account_name)
    out_path = Path(credentials_dir) / "{}.yaml".format(stem)
    if out_path.exists():
        if input("{} already exists — overwrite? [y/N] ".format(out_path)).strip().lower() != "y":
            die("Aborted — not overwriting {}".format(out_path))

    fields = build_account_fields(provider)
    encrypt = input("\nEncrypt this file? [Y/n] ").strip().lower() != "n"

    write_account_file(out_path, provider, fields, encrypt)
    print("\nWrote {} ({}).".format(out_path, "encrypted" if encrypt else "UNENCRYPTED"))
    print('Reference it in your lab JSON as:  "cloud_account": "{}"'.format(stem))
    return out_path


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
    to_encrypt = [k for k, v in data.items()
                  if isinstance(v, str) and k not in ("cloudtype", "credential_kind")
                  and _looks_sensitive(k)]

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
    for k in to_encrypt:
        data[k] = crypto_store.encrypt_cascade(passphrase, data[k].encode("utf-8"))

    out_path = p.parent / "{}.encrypted.yaml".format(p.stem)
    out_path.write_text(yaml.safe_dump(data, sort_keys=False))
    out_path.chmod(0o600)
    print("\nWrote {} — review it, then move it into place yourself "
          "(the original is left untouched).".format(out_path))
    return out_path


def main():
    args = sys.argv[1:]

    if args and args[0] in ("--help", "-h"):
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
    # prompt sequence to end early — confirmed live 2026-09-11: without this,
    # either one surfaces as a raw Python traceback instead of a clean abort.
    try:
        print("What kind of credential?")
        print("  1) Cloud provider (used to create/manage VMs — cloud_account)")
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
