"""Reading the open document for an `@AI` request: capture, linearize, verify the selection.

THE SERVER CANNOT SEE EDITS THAT HAVE NOT REACHED IT. Documents are edited live in browsers and
synchronized to the collaboration service; what this module reads is the service's validated state at
the moment of the call. The composer refuses "Use open document" while the editor reports unsynced
changes, and every result states the captured update counter, so a person can tell which version of
the document an answer saw.

HOW: the worker calls the collaboration service's private route `POST /internal/ai/read`
(secret-protected, the same route the Phase 2 hooks use). The service stores any pending edits first,
validates the content with the shared schema, and answers with the content, its hash, the exact
`yjs_update_counter` of the stored state it describes, and every block with an id: its type, parent,
text and its own content hash. The call is made for the REQUESTER: the service re-runs the same
`can_write` the editor uses, so a demoted or removed user gets 403 here too. The deprecated JSON column
and old snapshots are never used; if no reliable capture is possible the request fails with a clear
code, it is not answered from something stale.

CAPTURED ONCE per attempt and bound to `document_id`: a later change of the room's active document
never redirects a running request.

SELECTION CHECK. The client sends the ids of the blocks it highlighted and a hash it computed from them
at send time: sha256 of `"\\n".join(f"{id}:{block_hash}")` over the selected blocks in document order,
where `block_hash` is the shared package's `blockHash`. If any block is gone or the hash differs, the
text changed after the person selected it: the request fails with `selection_changed` and asks them to
select again, instead of answering about text they no longer see.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx2

from app.config import Settings

DOC_CAPTURE_ERRORS = {
    403: "ai_write_access_required",
    404: "document_not_found",
    409: "document_unavailable",
    422: "capture_failed",
    429: "capture_rate_limited",
}


class CaptureFailed(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DocBlock:
    id: str
    type: str
    level: int  # heading level, 0 otherwise
    text: str
    hash: str
    parent_id: str | None


@dataclass
class Captured:
    document_id: uuid.UUID
    counter: int
    content_hash: str
    blocks: list[DocBlock]  # every block with an id, in document order
    content: dict[str, Any] = field(repr=False, default_factory=dict)

    def leaves(self) -> list[DocBlock]:
        parents = {b.parent_id for b in self.blocks if b.parent_id}
        return [b for b in self.blocks if b.id not in parents and b.text.strip()]


async def capture_document(
    settings: Settings,
    requester_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Captured:
    secret = settings.internal_api_secret.get_secret_value()
    if not secret:
        raise CaptureFailed("capture_unavailable")
    try:
        async with httpx2.AsyncClient(
            base_url=settings.collab_service_url,
            timeout=settings.ai_collab_timeout_seconds,
            transport=transport,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/internal/ai/read",
                headers={"X-Internal-Secret": secret},
                json={"document_id": str(document_id), "requester_id": str(requester_id)},
            )
    except (httpx2.TimeoutException, httpx2.TransportError):
        raise CaptureFailed("capture_unavailable", retryable=True) from None
    if response.status_code != 200:
        if response.status_code in DOC_CAPTURE_ERRORS:
            raise CaptureFailed(DOC_CAPTURE_ERRORS[response.status_code])
        raise CaptureFailed("capture_unavailable", retryable=response.status_code >= 500)
    try:
        data = response.json()
        content = data["content"]
        levels = heading_levels(content)
        blocks = [
            DocBlock(
                id=str(b["id"]),
                type=str(b["type"]),
                level=levels.get(str(b["id"]), 0),
                text=str(b["text"]),
                hash=str(b["hash"]),
                parent_id=str(b["parent_id"]) if b.get("parent_id") else None,
            )
            for b in data["blocks"]
        ]
        counter = int(data["yjs_update_counter"])
        content_hash = str(data["content_hash"])
    except (ValueError, KeyError, TypeError):
        raise CaptureFailed("capture_failed") from None
    if data.get("document_id") != str(document_id):
        raise CaptureFailed("capture_failed")  # bound to THIS document, whatever came back
    return Captured(document_id, counter, content_hash, blocks, content)


def heading_levels(content: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}

    def walk(nodes: Any) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            attrs = node.get("attrs") or {}
            if node.get("type") == "heading" and isinstance(attrs.get("id"), str):
                level = attrs.get("level")
                out[attrs["id"]] = level if isinstance(level, int) and 1 <= level <= 6 else 1
            walk(node.get("content"))

    walk(content.get("content"))
    return out


def selection_hash(blocks: list[DocBlock]) -> str:
    """The hash the client must have computed (see the module docstring)."""
    return hashlib.sha256("\n".join(f"{b.id}:{b.hash}" for b in blocks).encode("utf-8")).hexdigest()


def resolve_selection(
    captured: Captured, selected_ids: list[str], expected_hash: str | None
) -> list[DocBlock]:
    """The selected blocks in DOCUMENT order, or CaptureFailed('selection_changed')."""
    by_id = {b.id: b for b in captured.blocks}
    if any(i not in by_id for i in selected_ids):
        raise CaptureFailed("selection_changed")
    wanted = set(selected_ids)
    chosen = [b for b in captured.blocks if b.id in wanted]
    # a container selected together with its own children would count the text twice
    ids = {b.id for b in chosen}
    chosen = [b for b in chosen if b.parent_id not in ids]
    if (
        expected_hash is None
        or selection_hash(sorted_like(chosen, selected_ids, captured)) != expected_hash
    ):
        raise CaptureFailed("selection_changed")
    return chosen


def sorted_like(
    chosen: list[DocBlock], selected_ids: list[str], captured: Captured
) -> list[DocBlock]:
    """The hash is over ALL ids the client selected, in document order (not the de-duplicated set)."""
    order = {b.id: i for i, b in enumerate(captured.blocks)}
    by_id = {b.id: b for b in captured.blocks}
    return sorted((by_id[i] for i in dict.fromkeys(selected_ids)), key=lambda b: order[b.id])


def asset_ids_in(content: dict[str, Any], scope_block_ids: set[str] | None) -> list[str]:
    """Ids of the images (AssetImage nodes) inside the whole document, or inside the given blocks."""
    found: list[str] = []

    def walk(nodes: Any, inside: bool) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            attrs = node.get("attrs") or {}
            here = inside or (scope_block_ids is not None and attrs.get("id") in scope_block_ids)
            is_image = node.get("type") == "assetImage" and isinstance(attrs.get("assetId"), str)
            if is_image and (scope_block_ids is None or here):
                found.append(attrs["assetId"])
            walk(node.get("content"), here)

    walk(content.get("content"), scope_block_ids is None)
    return list(dict.fromkeys(found))
