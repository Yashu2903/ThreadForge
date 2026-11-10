"""Shared setup for the analysis tests: a provider stub that answers vision and embedding calls the way
the documented APIs do, and a small 'world' (room, owner, document, consent) with upload and analyze
helpers. The stubs sit behind httpx2.MockTransport, so the REAL adapters run; only the network is
faked. They stand in for documented provider behaviour; they prove nothing about the real providers."""

from __future__ import annotations

import base64
import json
import re
import zlib
from typing import Any

from app.ai.notice import NOTICE_VERSION
from app.storage import Storage
from tests import analysis_files as af
from tests.ai_helpers import DIMS, Stub, ai_settings, install
from tests.doc_helpers import room_with_doc
from tests.helpers import TRUSTED_ORIGIN, add_member, execute, rows  # noqa: F401

TOPICS: list[set[str]] = [
    {"warehouse", "orders", "delivery", "drivers", "packaging", "returns", "shipping"},
    {"finance", "revenue", "growth", "budget", "quarter", "quarterly"},
    {"deployment", "pipeline", "release", "build", "deploy", "workers"},
    {"safety", "inspection", "incidents", "incident"},
    {"client", "api", "worker", "database", "architecture", "diagram"},
    {"kubernetes", "cluster", "node", "pods"},
    {"invoice", "payment", "billing", "tax"},
]
WORD = re.compile(r"[a-z]+")


def embed_text_vector(text: str, dims: int = DIMS) -> list[float]:
    """A deterministic bag-of-topics vector: each topic keyword adds to its own dimension, every other
    word adds a little to a hashed dimension. Similar topics land near each other."""
    vec = [0.0] * dims
    for word in WORD.findall(text.lower()):
        for slot, words in enumerate(TOPICS):
            if word in words:
                vec[slot] += 1.0
                break
        else:
            vec[7 % dims] += 0.02 * (1 + zlib.crc32(word.encode()) % 3)
    if not any(vec):
        vec[7 % dims] = 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


LABEL_LINE = re.compile(r"^\[([PSDC][0-9]+)\] ?(.*)$")
SCOPE_LINE = re.compile(r"^\[(B[0-9]+)\] \(([^)]*)\) ?(.*)$")
RANGE_LINE = re.compile(r"^\[(B[0-9]+)\] start=(\d+) end=(\d+)")
ANCHOR_LINE = re.compile(r"^after \[(B[0-9]+)\]$")


class Prompt:
    """What an answer/summary/classify call was given, parsed back out of the request body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.system = body.get("instructions", "")
        content = body["input"][0]["content"]
        self.text = "\n".join(p["text"] for p in content if p["type"] == "input_text")
        self.images = [p for p in content if p["type"] == "input_image"]
        self.items: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        # phase 2: the blocks the person made changeable, their declared ranges and the insertion points
        self.scope: dict[str, tuple[str, str]] = {}
        self.ranges: dict[str, tuple[int, int]] = {}
        self.anchors: list[str] = []
        lines = self.text.split("\n")
        for line in lines:
            m = SCOPE_LINE.match(line)
            if m:
                self.scope.setdefault(m.group(1), (m.group(2), m.group(3)))
            m = RANGE_LINE.match(line)
            if m:
                self.ranges[m.group(1)] = (int(m.group(2)), int(m.group(3)))
            m = ANCHOR_LINE.match(line)
            if m:
                self.anchors.append(m.group(1))
            elif line.startswith("start (before the first block)"):
                self.anchors.append("start")
        for i, line in enumerate(lines):
            m = LABEL_LINE.match(line)
            if m and i + 1 < len(lines):
                self.items.setdefault(m.group(1), lines[i + 1])  # the first mention is the evidence
                self.headers.setdefault(m.group(1), line)

    @classmethod
    def of(cls, body: dict[str, Any]) -> Prompt:
        return cls(body)

    @property
    def labels(self) -> list[str]:
        return [lb for lb in self.items if lb[0] in "PSD"]


def block(text: str, *refs: str, quote: str | None = None) -> dict[str, Any]:
    cites = [{"ref": r, "quote": quote if i == 0 else None} for i, r in enumerate(refs)]
    return {
        "type": "paragraph",
        "text": text,
        "items": None,
        "title": None,
        "mermaid": None,
        "citations": cites,
    }


def answer(*blocks: dict[str, Any], gaps: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"blocks": list(blocks), "evidence_gaps": list(gaps)}


def diagram(source: str, *refs: str, title: str = "Flow") -> dict[str, Any]:
    return {
        "type": "diagram",
        "text": None,
        "items": None,
        "title": title,
        "mermaid": source,
        "citations": [{"ref": r, "quote": None} for r in refs],
    }


def default_answer(prompt: Prompt) -> dict[str, Any]:
    labels = prompt.labels
    cites = labels[:1]
    return (
        answer(block("The evidence covers the request.", *cites))
        if cites
        else answer(block("No evidence."))
    )


def run_(text: str, *marks: str) -> dict[str, Any]:
    return {"text": text, "marks": [{"type": m, "href": None} for m in marks]}


def cite(*refs: str, quote: str | None = None) -> list[dict[str, Any]]:
    return [{"ref": r, "quote": quote if i == 0 else None} for i, r in enumerate(refs)]


def new_block(kind: str, **fields: Any) -> dict[str, Any]:
    """A block in the model's output format (every field present, unused ones null)."""
    base = {
        "type": kind,
        "level": None,
        "inline": None,
        "paragraphs": None,
        "items": None,
        "rows": None,
        "header_row": None,
        "mermaid": None,
        "citations": [],
    }
    base.update(fields)
    return base


def para(text: str, *refs: str) -> dict[str, Any]:
    return new_block("paragraph", inline=[run_(text)], citations=cite(*refs))


def op(kind: str, *, reason: str = "because", refs: tuple[str, ...] = (), **fields: Any) -> dict:
    base = {
        "op": kind,
        "block": None,
        "start": None,
        "end": None,
        "anchor": None,
        "block_refs": None,
        "new_inline": None,
        "blocks": None,
        "reason": reason,
        "citations": cite(*refs),
    }
    base.update(fields)
    return base


def edit_reply(*ops: dict[str, Any], summary: str = "Proposed changes.") -> dict[str, Any]:
    return {"summary": summary, "operations": list(ops), "evidence_gaps": []}


def draft_reply(title: str, *blocks: dict[str, Any], sources: bool | None = True) -> dict[str, Any]:
    return {
        "title": title,
        "summary": "A draft.",
        "blocks": list(blocks),
        "include_sources_section": sources,
        "evidence_gaps": [],
    }


def default_edit(prompt: Prompt) -> dict[str, Any]:
    refs = tuple(prompt.labels[:1])
    for label, (kind, _text) in prompt.scope.items():
        if kind in ("paragraph", "heading 1", "heading 2", "heading 3"):
            return edit_reply(
                op("replace_block_text", block=label, new_inline=[run_("Rewritten.")], refs=refs)
            )
    if prompt.anchors:
        return edit_reply(
            op(
                "insert_blocks_after",
                anchor=prompt.anchors[0],
                blocks=[para("A new paragraph.", *refs)],
                refs=refs,
            )
        )
    return edit_reply()


def default_draft(prompt: Prompt) -> dict[str, Any]:
    refs = tuple(prompt.labels[:1])
    return draft_reply(
        "Draft from the sources",
        new_block("heading", level=1, inline=[run_("Summary")]),
        para("The sources describe the work.", *refs),
    )


class AnalysisStub(Stub):
    """Vision replies and topic-aware embeddings. Counters let tests assert exactly what was called."""

    def __init__(self) -> None:
        super().__init__()
        self.transcript: Any = None  # str, list of str (consumed), or callable(body) -> str
        self.description: dict | None = None
        self.describe_failures = 0  # next N figure descriptions return unusable JSON
        # `@AI` requests (checkpoint 4)
        self.classify_as: Any = "qa"  # a task kind, or None for an unusable reply
        self.answer_script: list[
            Any
        ] = []  # consumed one per answer call: dict, str (raw) or callable
        self.answer_fn: Any = (
            None  # callable(prompt: Prompt) -> dict | str, used when the script is empty
        )
        # phase 2: edit proposals and drafts (same shape as the answer script)
        self.edit_script: list[Any] = []
        self.edit_fn: Any = None
        self.draft_script: list[Any] = []
        self.draft_fn: Any = None

    # ---- OpenAI ----
    def _llm_text(self, body: dict[str, Any]) -> str:
        fmt = (body.get("text") or {}).get("format") or {}
        name = fmt.get("name", "")
        if name == "page_transcription":
            value = self.transcript
            if callable(value):
                value = value(body)
            elif isinstance(value, list):
                value = value.pop(0)
            if value is None:
                value = " ".join(af.CLEAN_SCAN_LINES)
            return json.dumps({"text": value})
        if name == "task_kind":
            return json.dumps({"task": self.classify_as} if self.classify_as else {"oops": 1})
        if name == "section_summaries":
            prompt = Prompt.of(body)
            return json.dumps(
                {
                    "sections": [
                        {"ref": ref, "summary": f"Summary of {ref}: {text[:50]}"}
                        for ref, text in prompt.items.items()
                    ]
                }
            )
        if name == "cited_answer":
            prompt = Prompt.of(body)
            if self.answer_script:
                value = self.answer_script.pop(0)
            elif self.answer_fn is not None:
                value = self.answer_fn(prompt)
            else:
                value = default_answer(prompt)
            if callable(value):
                value = value(prompt)
            return value if isinstance(value, str) else json.dumps(value)
        if name in ("document_edit", "document_draft"):
            prompt = Prompt.of(body)
            edit = name == "document_edit"
            script = self.edit_script if edit else self.draft_script
            fn = self.edit_fn if edit else self.draft_fn
            if script:
                value = script.pop(0)
            elif fn is not None:
                value = fn(prompt)
            else:
                value = default_edit(prompt) if edit else default_draft(prompt)
            if callable(value):
                value = value(prompt)
            return value if isinstance(value, str) else json.dumps(value)
        if name == "figure_description":
            if self.describe_failures > 0:
                self.describe_failures -= 1
                return json.dumps({"figure_kind": "weird"})
            return json.dumps(
                self.description
                or {
                    "figure_kind": "diagram",
                    "description": "Architecture diagram: a client calls the API which feeds a worker and a database.",
                    "visible_text": "Client API Worker Database",
                    "elements": ["Client", "API", "Worker", "Database"],
                    "connections": [
                        {"from": "Client", "to": "API", "label": "request"},
                        {"from": "API", "to": "Worker", "label": ""},
                    ],
                }
            )
        return super()._llm_text(body)

    # ---- Google ----
    def _vector(self, part: dict[str, Any]) -> list[float]:
        if self.wrong_dimension:
            return [0.1] * (DIMS + 1)
        text = part.get("text")
        if text is not None:
            return embed_text_vector(text)
        raw = base64.b64decode(part["inline_data"]["data"])
        return embed_text_vector("diagram architecture client api" if len(raw) % 2 else "diagram")

    def vision_calls(self, name: str) -> int:
        n = 0
        for r in self.calls("/v1/responses"):
            fmt = ((r["json"] or {}).get("text") or {}).get("format") or {}
            if fmt.get("name") == name:
                n += 1
        return n

    def provider_call_count(self) -> int:
        return len([r for r in self.requests if r["method"] == "POST"])


class World:
    """One room (owner + a document + consent), a storage directory, helpers for the HTTP flow."""

    def __init__(self, owner, room_id, document, sm, store, cfg, stub) -> None:
        self.owner, self.room_id, self.document = owner, room_id, document
        self.sm, self.store, self.cfg, self.stub = sm, store, cfg, stub

    async def upload(self, actor, name: str, data: bytes) -> dict:
        """The whole device upload through the real routes; returns the finalized source JSON."""
        import uuid

        r = await actor.post(
            f"/rooms/{self.room_id}/source-uploads",
            json={"client_request_id": str(uuid.uuid4()), "filename": name, "byte_size": len(data)},
        )
        assert r.status_code in (200, 201), r.text
        uid = r.json()["id"]
        r = await actor.put(
            f"/rooms/{self.room_id}/source-uploads/{uid}/content",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 200, r.text
        r = await actor.post(f"/rooms/{self.room_id}/source-uploads/{uid}/finalize")
        assert r.status_code == 201, r.text
        return r.json()

    @staticmethod
    def version_of(source: dict) -> tuple[str, str]:
        return source["id"], source["current_version"]["id"]

    async def analyze(self, actor, source: dict, document_id: str | None = None):
        sid, vid = self.version_of(source)
        return await actor.post(
            f"/rooms/{self.room_id}/sources/{sid}/versions/{vid}/analyze",
            json={"document_id": document_id or self.document["id"]},
        )

    async def status(self, actor, source: dict):
        sid, vid = self.version_of(source)
        r = await actor.get(f"/rooms/{self.room_id}/sources/{sid}/versions/{vid}/analysis")
        assert r.status_code == 200, r.text
        return r.json()

    async def passages(self, actor, source: dict, **params):
        sid, vid = self.version_of(source)
        return await actor.get(
            f"/rooms/{self.room_id}/sources/{sid}/versions/{vid}/passages", params=params
        )

    # ---- `@AI` requests (checkpoint 4) ----
    def request_body(
        self, instruction: str = "How many orders did the warehouse process?", **overrides
    ) -> dict:
        import uuid as _uuid

        body = {
            "client_request_id": str(_uuid.uuid4()),
            "instruction": instruction,
            "task_hint": "auto",
            "document_id": self.document["id"],
        }
        body.update(overrides)
        return body

    async def send(
        self,
        actor,
        instruction: str = "How many orders did the warehouse process?",
        *,
        sources=(),
        **overrides,
    ):
        """POST /ai-requests with the given sources (source JSONs from upload())."""
        body = self.request_body(instruction, **overrides)
        if sources and "source_version_ids" not in overrides:
            body["source_version_ids"] = [self.version_of(s)[1] for s in sources]
        return await actor.post(f"/rooms/{self.room_id}/ai-requests", json=body)

    async def request(self, actor, request_id: str, suffix: str = ""):
        return await actor.get(f"/rooms/{self.room_id}/ai-requests/{request_id}{suffix}")

    async def drain(self, *, rounds: int = 40, collab=None) -> list[str]:
        """Do what the AI worker does until nothing is left to do: analysis jobs, waiting requests,
        queued requests. Returns the outcomes in order."""
        from app.ai.analysis.pipeline import process_next
        from app.ai.answer.execute import process_next_request
        from app.services import ai_requests

        out: list[str] = []
        for _ in range(rounds):
            progressed = False
            changed = await ai_requests.advance_waiting(self.sm, self.cfg)
            changed += await ai_requests.expire_stale(self.sm, self.cfg)
            progressed = progressed or bool(changed)
            res = await process_next(
                self.sm, self.cfg, self.store, transports=self.stub.transports()
            )
            if res is not None:
                out.append("analysis:" + res)
                progressed = True
            res = await process_next_request(
                self.sm,
                self.cfg,
                self.store,
                transports=self.stub.transports(),
                collab_transport=collab,
            )
            if res is not None:
                out.append("request:" + res)
                progressed = True
            if not progressed:
                break
        return out

    async def run_worker(self, *, limit: int = 20) -> list[str]:
        """Drain the queue the way the AI worker would, with the stub as the network."""
        from app.ai.analysis.pipeline import process_next

        outcomes = []
        for _ in range(limit):
            outcome = await process_next(
                self.sm, self.cfg, self.store, transports=self.stub.transports()
            )
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    async def state(self, source: dict) -> dict:
        _sid, vid = self.version_of(source)
        got = await rows(
            self.sm,
            "SELECT analysis_state, error_code, current_generation_id FROM source_versions WHERE id = :v",
            v=vid,
        )
        return got[0]


async def build_world(make_actor, sessionmaker, tmp_path, redis_prefix, **overrides) -> World:
    storage_dir = tmp_path / "storage"
    cfg = ai_settings(redis_channel_prefix=redis_prefix, storage_path=str(storage_dir), **overrides)
    stub = AnalysisStub()
    owner = await make_actor("Owner", app_settings=cfg)
    install(owner, stub)
    room_id, document = await room_with_doc(owner, sessionmaker)
    r = await owner.post("/ai/consent", json={"notice_version": NOTICE_VERSION})
    assert r.status_code == 204, r.text
    return World(owner, room_id, document, sessionmaker, Storage(storage_dir), cfg, stub)


async def give_consent(actor) -> None:
    r = await actor.post("/ai/consent", json={"notice_version": NOTICE_VERSION})
    assert r.status_code == 204, r.text


__all__ = ["AnalysisStub", "World", "add_member", "build_world", "execute", "give_consent"]
