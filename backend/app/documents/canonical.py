"""Deterministic serialization, so equal documents always hash equal.

PostgreSQL JSONB does not preserve key order, and JSON allows several spellings of one number, so
"is this the same content?" can never be a comparison of stored text. Instead:

    canonical_json(tree) = keys sorted, no whitespace, UTF-8 (not \\u-escaped), 2.0 written as 2
    content_hash(tree)   = SHA-256 of canonical_json(tree) as UTF-8

The TypeScript client serializes with the same rules (client/src/document-schema.ts).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def content_hash(value: Any) -> bytes:
    """The raw 32-byte SHA-256 stored in documents.content_hash."""
    return hashlib.sha256(canonical_bytes(value)).digest()
