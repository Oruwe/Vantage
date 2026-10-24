from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path


_SERVICE = "vantage"
_KEYCHAIN_USER = "data_key"
_FIELD_PREFIX = "v1:"


def _key_file_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "Vantage" / "keys" / "data.key"
    return Path.home() / ".vantage" / "keys" / "data.key"


def get_or_create_dek() -> bytes:
    """Return the 32-byte data encryption key, creating it on first call.

    Stored in the OS keychain; falls back to a file under the Vantage state dir.
    """
    try:
        import keyring
        stored = keyring.get_password(_SERVICE, _KEYCHAIN_USER)
        if stored:
            return bytes.fromhex(stored)
    except Exception:
        pass

    key_file = _key_file_path()
    if key_file.exists():
        return bytes.fromhex(key_file.read_text(encoding="utf-8").strip())

    key = secrets.token_bytes(32)
    key_hex = key.hex()

    try:
        import keyring
        keyring.set_password(_SERVICE, _KEYCHAIN_USER, key_hex)
        return key
    except Exception:
        pass

    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key_hex, encoding="utf-8")
    return key


def encrypt_field(plaintext: str, key: bytes | None = None) -> str:
    """AES-GCM encrypt a string field. Returns a ``v1:<b64>`` string."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key is None:
        key = get_or_create_dek()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    encoded = base64.b64encode(nonce + ct).decode("ascii")
    return _FIELD_PREFIX + encoded


def decrypt_field(ciphertext: str, key: bytes | None = None) -> str:
    """AES-GCM decrypt a ``v1:<b64>`` field. Returns plaintext.

    If the value does not start with the prefix it is treated as legacy
    plaintext and returned unchanged (transparent migration path).
    """
    if not ciphertext.startswith(_FIELD_PREFIX):
        return ciphertext
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key is None:
        key = get_or_create_dek()
    raw = base64.b64decode(ciphertext[len(_FIELD_PREFIX):])
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
