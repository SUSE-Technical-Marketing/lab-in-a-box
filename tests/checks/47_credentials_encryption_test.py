#!/usr/bin/env python3
# Integration tests for the encrypted-credentials feature:
# primary.py's try_load_cloud_account() decrypt/cache/opt-out handling, and
# scripts/setup_credentials.py's two modes. Uses the REAL crypto_store
# (cryptography package) — nothing about the cipher itself is mocked, only
# the interactive prompts (input()/getpass). Run from
# 47_credentials_encryption.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import yaml  # noqa: E402
import primary  # noqa: E402
import crypto_store  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _write(dirpath, name, obj):
    p = Path(dirpath) / "{}.yaml".format(name)
    p.write_text(yaml.safe_dump(obj, sort_keys=False))
    return p


# ── credentials_dirs() / CREDENTIALS_PATH override ──────────────────────────
check("credentials_dirs(None) falls back to the built-in default",
      primary.credentials_dirs(None)[0] == "/etc/lab_creation/credentials")
check("credentials_dirs({'CREDENTIALS_PATH': ...}) honours the override",
      primary.credentials_dirs({"CREDENTIALS_PATH": "/srv/creds"}) == ["/srv/creds"])


# ── whole-file encryption (setup_credentials.py mode A shape) ───────────────
with tempfile.TemporaryDirectory() as d:
    primary.clear_passphrase_cache()
    fields = {"AWS_REGION": "eu-central-1", "AWS_SECRET_ACCESS_KEY": "super-secret-value"}
    envelope = crypto_store.encrypt_cascade("hunter2", yaml.safe_dump(fields, sort_keys=False).encode("utf-8"))
    _write(d, "aws-sandbox", dict({"cloudtype": "aws"}, **envelope))
    cfg = {"CREDENTIALS_PATH": d}

    prompts = []

    def _right_pw(prompt):
        prompts.append(prompt)
        return "hunter2"

    data, err = primary.try_load_cloud_account("aws-sandbox", config=cfg, passphrase_prompt=_right_pw)
    check("whole-file encrypted account: loads with no error given the right passphrase",
          err is None and data is not None)
    check("whole-file encrypted account: the decrypted fields come through",
          data.get("AWS_REGION") == "eu-central-1" and data.get("AWS_SECRET_ACCESS_KEY") == "super-secret-value")
    check("whole-file encrypted account: CLOUDTYPE is normalised and present",
          data.get("CLOUDTYPE") == "aws")
    check("whole-file encrypted account: prompted exactly once", len(prompts) == 1)

    # second load of the SAME account within this process must NOT re-prompt
    # (in-process passphrase cache, keyed by the resolved file path)
    data2, err2 = primary.try_load_cloud_account("aws-sandbox", config=cfg, passphrase_prompt=_right_pw)
    check("whole-file encrypted account: a second load in the same run reuses the cached passphrase",
          err2 is None and len(prompts) == 1)

    primary.clear_passphrase_cache()

    # wrong passphrase -> error after retries, never raises
    died_cleanly = True
    try:
        data3, err3 = primary.try_load_cloud_account(
            "aws-sandbox", config=cfg, passphrase_prompt=lambda prompt: "totally-wrong")
    except Exception:
        died_cleanly = False
    check("whole-file encrypted account: a wrong passphrase returns (None, error), never raises",
          died_cleanly and data3 is None and err3 and "aws-sandbox" in err3)


# ── unencrypted: true opt-out — no prompt at all ────────────────────────────
with tempfile.TemporaryDirectory() as d:
    primary.clear_passphrase_cache()
    _write(d, "hetzner-plain", {
        "cloudtype": "hetzner", "unencrypted": True, "HETZNER_TOKEN": "plain-token",
    })
    called = []
    data, err = primary.try_load_cloud_account(
        "hetzner-plain", config={"CREDENTIALS_PATH": d},
        passphrase_prompt=lambda prompt: called.append(prompt) or "should-never-be-used")
    check("unencrypted: true account: loads cleanly", err is None and data.get("HETZNER_TOKEN") == "plain-token")
    check("unencrypted: true account: never prompts for a passphrase", called == [])


# ── field-level encryption (setup_credentials.py mode B shape) ─────────────
with tempfile.TemporaryDirectory() as d:
    primary.clear_passphrase_cache()
    secret_envelope = crypto_store.encrypt_cascade("fieldpw", b"boxed-secret-value")
    _write(d, "gcp-prod", {
        "cloudtype": "gcp", "GCP_PROJECT": "lab-prod", "GCP_ZONE": "europe-west1-b",
        "GCP_SERVICE_ACCOUNT_KEY_SECRET": secret_envelope,
    })
    prompts = []
    data, err = primary.try_load_cloud_account(
        "gcp-prod", config={"CREDENTIALS_PATH": d},
        passphrase_prompt=lambda prompt: prompts.append(prompt) or "fieldpw")
    check("field-level encrypted account: loads cleanly", err is None)
    check("field-level encrypted account: the plaintext fields pass through untouched",
          data.get("GCP_PROJECT") == "lab-prod" and data.get("GCP_ZONE") == "europe-west1-b")
    check("field-level encrypted account: the boxed field is decrypted to its real value",
          data.get("GCP_SERVICE_ACCOUNT_KEY_SECRET") == "boxed-secret-value")
    check("field-level encrypted account: prompted exactly once (one encrypted field)", len(prompts) == 1)


# ── setup_credentials.py: _looks_sensitive() heuristic ──────────────────────
import setup_credentials as sc  # noqa: E402

check("_looks_sensitive: AWS_SECRET_ACCESS_KEY matches", sc._looks_sensitive("AWS_SECRET_ACCESS_KEY"))
check("_looks_sensitive: UPCLOUD_PASSWORD matches", sc._looks_sensitive("UPCLOUD_PASSWORD"))
check("_looks_sensitive: HETZNER_TOKEN matches (via _TOKEN)", sc._looks_sensitive("AWS_SESSION_TOKEN"))
check("_looks_sensitive: AWS_REGION does not match", not sc._looks_sensitive("AWS_REGION"))
check("_looks_sensitive: AWS_KEY_NAME does not match (a name reference, not a secret)",
      not sc._looks_sensitive("AWS_KEY_NAME"))


# ── setup_credentials.py: write_account_file() + encrypt_existing() round trip ──
with tempfile.TemporaryDirectory() as d:
    primary.clear_passphrase_cache()
    sc.crypto_store.prompt_passphrase = lambda *a, **kw: "buildpw"
    out = sc.write_account_file(Path(d) / "aws-ci.yaml", "aws",
                                {"AWS_REGION": "us-east-1", "AWS_SECRET_ACCESS_KEY": "s3kr3t"}, encrypt=True)
    written = yaml.safe_load(out.read_text())
    check("write_account_file(): the file it writes is whole-file encrypted",
          written.get("encrypted") is True and written.get("cloudtype") == "aws")
    reloaded, err = primary.try_load_cloud_account(
        "aws-ci", config={"CREDENTIALS_PATH": d}, passphrase_prompt=lambda p: "buildpw")
    check("write_account_file(): round-trips through try_load_cloud_account() correctly",
          err is None and reloaded.get("AWS_SECRET_ACCESS_KEY") == "s3kr3t")

with tempfile.TemporaryDirectory() as d:
    plain_path = Path(d) / "aws-legacy.yaml"
    plain_path.write_text(yaml.safe_dump({
        "cloudtype": "aws", "AWS_REGION": "eu-west-1", "AWS_SECRET_ACCESS_KEY": "plain-secret",
    }))
    sc.crypto_store.prompt_passphrase = lambda *a, **kw: "retrofitpw"
    import builtins
    _orig_input = builtins.input
    builtins.input = lambda *a, **kw: "y"
    try:
        out2 = sc.encrypt_existing(str(plain_path))
    finally:
        builtins.input = _orig_input
    check("encrypt_existing(): writes a NEW file, never overwrites the original",
          out2 is not None and out2 != plain_path and plain_path.read_text().find("plain-secret") != -1)
    after = yaml.safe_load(out2.read_text())
    check("encrypt_existing(): the sensitive field is now boxed",
          isinstance(after.get("AWS_SECRET_ACCESS_KEY"), dict)
          and after["AWS_SECRET_ACCESS_KEY"].get("encrypted") is True)
    check("encrypt_existing(): non-sensitive fields stay readable",
          after.get("AWS_REGION") == "eu-west-1")


# ── real subprocess: stdin running out mid-prompt must abort cleanly ────────
# EOFError/KeyboardInterrupt from input()/getpass()
# weren't caught anywhere, so piped stdin running out mid-sequence (a real,
# ordinary way for scripted or fat-fingered input to end) surfaced as a raw
# Python traceback instead of a clean "Aborted" message.
import subprocess  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    short_path = Path(d) / "short.yaml"
    short_path.write_text(yaml.safe_dump({"cloudtype": "aws", "AWS_SECRET_ACCESS_KEY": "x"}))
    # "y" to proceed, then stdin ends before any passphrase is ever given
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "setup_credentials.py"),
         "--encrypt-existing", str(short_path)],
        input="y\n", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check("setup_credentials.py: stdin running out mid-prompt exits non-zero, not a hang or a crash",
          proc.returncode != 0)
    check("setup_credentials.py: stdin running out mid-prompt prints a clean 'Aborted', no raw traceback",
          "Aborted" in proc.stdout and "Traceback (most recent call last)" not in proc.stdout)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all credentials_encryption checks passed")
