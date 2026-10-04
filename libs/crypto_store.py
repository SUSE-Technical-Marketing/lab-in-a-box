"""
crypto_store.py — passphrase-based encryption for lab-in-a-box's credential
files (/etc/lab_creation/credentials/<name>.yaml — see primary.py's
try_load_cloud_account()/scripts/setup_credentials.py). Small, single-purpose
functions, no file/YAML I/O of its own, so every caller that needs to
encrypt or decrypt a credential value goes through the exact same path.

Cipher design: for a passphrase-encrypted file, the KDF is the security
boundary, and AES-256 alone is considered secure. The hardening is layered
two ways: an intentionally slow, memory-
hard KDF (Argon2id), and TWO independent, differently-designed AEAD ciphers
cascaded rather than one, each keyed by its own subkey with its own domain-
separation label via HKDF-SHA512 — a catastrophic break of either single
algorithm (or of this project's own use of it) still isn't enough on its own:

    passphrase --Argon2id(salt)-->            64-byte master secret
    master secret --HKDF-SHA512(info=aes)-->    32-byte subkey 1
    master secret --HKDF-SHA512(info=chacha)--> 32-byte subkey 2
    plaintext   --AES-256-GCM(subkey 1, nonce 1)-->   intermediate
    intermediate --ChaCha20-Poly1305(subkey 2, nonce 2)--> ciphertext

Decrypt is the exact mirror: ChaCha20-Poly1305 first, then AES-256-GCM.
Each layer is its own AEAD (authenticated) construction, so a wrong
passphrase or a tampered/corrupt envelope is caught as an
InvalidTag -> DecryptionError, not silently "decrypted" into garbage.

Argon2id support needs cryptography>=41. On an older version, derive_master_key()
raises a RuntimeError that names the fix. The Argon2id keyword arguments are
salt, length, iterations, lanes and memory_cost. A TypeError raised by the
Argon2id(...) call means those names changed upstream.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

try:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
except ImportError:
    Argon2id = None

import getpass


class DecryptionError(Exception):
    """Wrong passphrase, or the envelope is corrupt/tampered. Deliberately
    doesn't distinguish the two — same as every other password-based
    encryption tool (age, gpg, ...): telling them apart would leak whether
    a guessed passphrase was "close"."""


# Deliberately slow — Argon2id's cost IS the real security boundary for a
# passphrase-encrypted file (see module docstring), not the cipher. These
# defaults target roughly 0.5-1s per attempt on ordinary hardware; lower
# ARGON2_MEMORY_COST_KIB only if this becomes impractical on a genuinely
# low-power automation VM (each envelope records the params it was made
# with, so changing the module defaults never breaks decrypting an older
# file — see encrypt_cascade()/decrypt_cascade()).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 262144  # 256 MiB
ARGON2_PARALLELISM = 4
ARGON2_SALT_LEN = 16
MASTER_KEY_LEN = 64  # 512 bits, split into two 256-bit AEAD subkeys via HKDF

_AES_INFO = b"lab-in-a-box:credentials:aes-layer"
_CHACHA_INFO = b"lab-in-a-box:credentials:chacha-layer"

_CIPHER_ID = "aes-256-gcm+chacha20-poly1305"
_KDF_ID = "argon2id"


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text)


def derive_master_key(passphrase, salt, time_cost=ARGON2_TIME_COST,
                      memory_cost_kib=ARGON2_MEMORY_COST_KIB, parallelism=ARGON2_PARALLELISM):
    """passphrase (str) + salt (bytes) -> a MASTER_KEY_LEN-byte secret, via Argon2id."""
    if Argon2id is None:
        raise RuntimeError(
            "This Python's 'cryptography' package has no Argon2id KDF (needs cryptography>=41) "
            "— pip install --upgrade cryptography")
    kdf = Argon2id(
        salt=salt, length=MASTER_KEY_LEN,
        iterations=time_cost, lanes=parallelism, memory_cost=memory_cost_kib,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def derive_subkey(master_key, info, salt, length=32):
    """One HKDF-SHA512 subkey from master_key, domain-separated by `info` (bytes) —
    so the AES and ChaCha20 layers below are independently keyed, not just two
    halves of one secret."""
    return HKDF(algorithm=hashes.SHA512(), length=length, salt=salt, info=info).derive(master_key)


def split_keys(master_key, salt):
    """master_key -> (aes_subkey, chacha_subkey), each 32 bytes."""
    return (derive_subkey(master_key, _AES_INFO, salt),
            derive_subkey(master_key, _CHACHA_INFO, salt))


def encrypt_cascade(passphrase, plaintext_bytes):
    """
    Encrypt plaintext_bytes with `passphrase`. Returns an envelope dict —
    JSON/YAML-serializable as-is, no further encoding needed — holding
    everything decrypt_cascade() needs except the passphrase itself. See
    this module's own docstring for the full pipeline.
    """
    salt = os.urandom(ARGON2_SALT_LEN)
    master_key = derive_master_key(passphrase, salt)
    aes_key, chacha_key = split_keys(master_key, salt)

    nonce1 = os.urandom(12)
    intermediate = AESGCM(aes_key).encrypt(nonce1, plaintext_bytes, None)
    nonce2 = os.urandom(12)
    ciphertext = ChaCha20Poly1305(chacha_key).encrypt(nonce2, intermediate, None)

    return {
        "encrypted": True,
        "kdf": _KDF_ID,
        "kdf_time_cost": ARGON2_TIME_COST,
        "kdf_memory_cost_kib": ARGON2_MEMORY_COST_KIB,
        "kdf_parallelism": ARGON2_PARALLELISM,
        "cipher": _CIPHER_ID,
        "salt": _b64(salt),
        "nonce1": _b64(nonce1),
        "nonce2": _b64(nonce2),
        "ciphertext": _b64(ciphertext),
    }


def decrypt_cascade(passphrase, envelope):
    """
    Reverse of encrypt_cascade(). Raises DecryptionError on a wrong
    passphrase, a corrupt/tampered envelope, or an envelope whose kdf/cipher
    isn't one this module knows how to read (e.g. from a newer version).
    """
    if envelope.get("kdf") != _KDF_ID or envelope.get("cipher") != _CIPHER_ID:
        raise DecryptionError("unrecognised kdf/cipher in credential envelope: {}/{} "
                              "(expected {}/{})".format(
                                  envelope.get("kdf"), envelope.get("cipher"), _KDF_ID, _CIPHER_ID))
    try:
        salt = _unb64(envelope["salt"])
        nonce1 = _unb64(envelope["nonce1"])
        nonce2 = _unb64(envelope["nonce2"])
        ciphertext = _unb64(envelope["ciphertext"])
        time_cost = int(envelope.get("kdf_time_cost", ARGON2_TIME_COST))
        memory_cost_kib = int(envelope.get("kdf_memory_cost_kib", ARGON2_MEMORY_COST_KIB))
        parallelism = int(envelope.get("kdf_parallelism", ARGON2_PARALLELISM))
    except (KeyError, ValueError, TypeError) as e:
        raise DecryptionError("malformed credential envelope: {}".format(e))

    master_key = derive_master_key(passphrase, salt, time_cost, memory_cost_kib, parallelism)
    aes_key, chacha_key = split_keys(master_key, salt)
    try:
        intermediate = ChaCha20Poly1305(chacha_key).decrypt(nonce2, ciphertext, None)
        plaintext = AESGCM(aes_key).decrypt(nonce1, intermediate, None)
    except InvalidTag:
        raise DecryptionError("wrong passphrase, or the credential file is corrupt/tampered")
    return plaintext


def prompt_passphrase(prompt="Passphrase: ", confirm=False):
    """getpass-based passphrase prompt (no echo). confirm=True asks twice and
    retries until they match — used by setup_credentials.py when SETTING a
    passphrase; reading one back for decryption never needs confirm=True."""
    while True:
        pw = getpass.getpass(prompt)
        if not confirm:
            return pw
        pw2 = getpass.getpass("Confirm passphrase: ")
        if pw == pw2:
            return pw
        print("Passphrases didn't match — try again.")
