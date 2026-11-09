"""Helpers for the `@AI` request tests: a fake collaboration service (the private read route), document
blocks with real shared-package-style hashes, and small utilities."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx2

from app.ai.answer.capture import DocBlock, selection_hash


class FakeCollab:
    """Stands in for POST /internal/ai/read of the collaboration service. It answers in the shape the
    real service documents (collab/src/ai-hooks.ts): content, content_hash, yjs_update_counter, blocks."""

    def __init__(
        self, document_id: str, secret: str, items: list[tuple[str, int, str]] | None = None
    ) -> None:
        self.document_id = document_id
        self.secret = secret
        self.counter = 7
        self.status = 200
        self.calls: list[dict[str, Any]] = []
        self.items = items or [
            ("heading", 1, "Launch plan"),
            ("paragraph", 0, "The launch is scheduled for Friday at noon."),
            ("paragraph", 0, "The budget is forty thousand dollars."),
            ("heading", 2, "Risks"),
            ("paragraph", 0, "Vendor delays could slip the schedule."),
        ]
        self.assets: dict[str, str] = {}  # block index -> asset id (an image inside that block)
        self.rebuild()

    def rebuild(self) -> None:
        self.blocks: list[DocBlock] = []
        content: list[dict[str, Any]] = []
        for i, (kind, level, text) in enumerate(self.items):
            bid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.document_id}/{i}"))
            node: dict[str, Any] = {
                "type": kind,
                "attrs": {"id": bid},
                "content": [{"type": "text", "text": text}],
            }
            if kind == "heading":
                node["attrs"]["level"] = level
            if str(i) in self.assets:
                node["content"].append(
                    {"type": "assetImage", "attrs": {"assetId": self.assets[str(i)]}}
                )
            content.append(node)
            digest = hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()
            self.blocks.append(
                DocBlock(bid, kind, level if kind == "heading" else 0, text, digest, None)
            )
        self.content = {"type": "doc", "content": content}
        self.content_hash = hashlib.sha256(
            json.dumps(self.content, sort_keys=True).encode()
        ).hexdigest()

    def edit(self, index: int, text: str) -> None:
        kind, level, _ = self.items[index]
        self.items[index] = (kind, level, text)
        self.counter += 1
        self.rebuild()

    def block_ids(self, *indexes: int) -> list[str]:
        return [self.blocks[i].id for i in indexes]

    def hash_for(self, *indexes: int) -> str:
        return selection_hash([self.blocks[i] for i in sorted(indexes)])

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            self.calls.append(
                {"path": request.url.path, "headers": dict(request.headers), "json": body}
            )
            if request.headers.get("x-internal-secret") != self.secret:
                return httpx2.Response(401, json={"error": "unauthorized"})
            if self.status != 200:
                return httpx2.Response(self.status, json={"error": "denied"})
            return httpx2.Response(
                200,
                json={
                    "document_id": self.document_id,
                    "yjs_update_counter": self.counter,
                    "content_hash": self.content_hash,
                    "content": self.content,
                    "blocks": [
                        {
                            "id": b.id,
                            "type": b.type,
                            "hash": b.hash,
                            "parent_id": b.parent_id,
                            "text": b.text,
                        }
                        for b in self.blocks
                    ],
                },
            )

        return httpx2.MockTransport(handle)


def answer_calls(stub) -> list[dict[str, Any]]:
    return [
        c
        for c in stub.calls("/v1/responses")
        if ((c["json"].get("text") or {}).get("format") or {}).get("name") == "cited_answer"
    ]


def prompt_text(call: dict[str, Any]) -> str:
    return "\n".join(
        p["text"] for p in call["json"]["input"][0]["content"] if p["type"] == "input_text"
    )
