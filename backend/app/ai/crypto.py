"""AES-256-GCM encryption for provider API keys (and any later secret that must be stored).

A *key set* is a list of `key_id:base64(32 random bytes)` pairs. The FIRST encrypts; every listed key
can decrypt, so a key can be rotated by adding a new first entry and keeping the old one until all
rows are re-encrypted. The stored blob is `nonce(12 bytes) || ciphertext+tag`.

AAD ("additional authenticated data") is text that is NOT stored in the blob but must be supplied
again to decrypt. We bind `user_id` and `provider`, so copying one user's encrypted_key into another
user's row (or another provider's) makes decryption fail instead of silently working.

Needs no database and no settings: a worker (the only decryptor) and the API (the only encryptor)
both just load the key set string.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
NONCE_BYTES = 12


class DecryptionError(Exception):
    """Wrong key, tampered blob, or wrong AAD. Never carries the data."""


@dataclass(frozen=True)
class KeySet:
    keys: tuple[tuple[str, bytes], ...]  # (key_id, 32 raw bytes); the first is the active one

    @classmethod
    def parse(cls, text: str) -> KeySet:
        """Raise ValueError (message without key material) on anything malformed."""
        pairs: list[tuple[str, bytes]] = []
        for index, item in enumerate(p.strip() for p in text.split(",") if p.strip()):
            key_id, sep, encoded = item.partition(":")
            if not sep or not _KEY_ID.fullmatch(key_id):
                raise ValueError(f"entry {index + 1} must look like key_id:base64key")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"entry {index + 1} ({key_id}) is not valid base64") from None
            if len(raw) != 32:
                raise ValueError(f"entry {index + 1} ({key_id}) must decode to exactly 32 bytes")
            if any(existing == key_id for existing, _ in pairs):
                raise ValueError(f"key id {key_id!r} appears twice")
            pairs.append((key_id, raw))
        if not pairs:
            raise ValueError("at least one key_id:base64key entry is required")
        return cls(tuple(pairs))

    @property
    def active_id(self) -> str:
        return self.keys[0][0]

    def encrypt(self, plaintext: bytes, aad: bytes) -> tuple[str, bytes]:
        key_id, raw = self.keys[0]
        nonce = os.urandom(NONCE_BYTES)
        return key_id, nonce + AESGCM(raw).encrypt(nonce, plaintext, aad)

    def decrypt(self, key_id: str, blob: bytes, aad: bytes) -> bytes:
        raw = dict(self.keys).get(key_id)
        if raw is None or len(blob) <= NONCE_BYTES:
            raise DecryptionError("unknown key id or malformed blob")
        try:
            return AESGCM(raw).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
        except InvalidTag:
            raise DecryptionError("authentication failed") from None


def key_aad(user_id: uuid.UUID, provider: str) -> bytes:
    """The AAD for a user's provider key: changing either value makes decryption fail."""
    return f"threadforge:ai-key:v1:{user_id}:{provider}".encode()


def generate_key_set_entry(key_id: str = "k1") -> str:
    """For operators and tests: a fresh `key_id:base64key` entry."""
    return f"{key_id}:{base64.b64encode(os.urandom(32)).decode()}"
