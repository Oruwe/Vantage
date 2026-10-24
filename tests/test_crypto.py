import secrets

from ghost_daemon.crypto import encrypt_field, decrypt_field, _FIELD_PREFIX


def test_roundtrip():
    key = secrets.token_bytes(32)
    plaintext = "test-bearer-token-xyz-12345"
    ciphertext = encrypt_field(plaintext, key=key)
    assert decrypt_field(ciphertext, key=key) == plaintext


def test_ciphertext_has_prefix():
    key = secrets.token_bytes(32)
    ct = encrypt_field("any string", key=key)
    assert ct.startswith(_FIELD_PREFIX)
    assert ct.startswith("v1:")


def test_nonces_are_randomized():
    key = secrets.token_bytes(32)
    c1 = encrypt_field("hello world", key=key)
    c2 = encrypt_field("hello world", key=key)
    assert c1 != c2


def test_legacy_plaintext_passthrough():
    # Values without the v1: prefix are returned as-is (transparent migration)
    plaintext = "old-token-no-prefix"
    assert decrypt_field(plaintext) == plaintext


def test_empty_string_roundtrip():
    key = secrets.token_bytes(32)
    ct = encrypt_field("", key=key)
    assert decrypt_field(ct, key=key) == ""


def test_unicode_roundtrip():
    key = secrets.token_bytes(32)
    plaintext = "token-with-unicode-café-日本語"
    assert decrypt_field(encrypt_field(plaintext, key=key), key=key) == plaintext


def test_ciphertext_not_plaintext():
    key = secrets.token_bytes(32)
    plaintext = "super-secret-value"
    ct = encrypt_field(plaintext, key=key)
    assert plaintext not in ct
