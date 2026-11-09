"""Helpers for the AI tests: AI-enabled settings and HTTP-level provider stubs.

The stubs sit behind httpx2.MockTransport, so the REAL adapters build the REAL requests (URL, headers,
JSON body) and parse the REAL kind of response; only the network is replaced. They are stand-ins for
the providers' documented behaviour, not proof that the real providers behave that way (the
real-provider checks are listed separately in docs/ai.md).
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Callable
from typing import Any

import httpx2
from PIL import Image

from app.ai.crypto import generate_key_set_entry
from app.config import Settings
from tests.factories import make_settings

SERVER_OPENAI_KEY = "sk-server-openai-0123456789abcdef"
SERVER_GOOGLE_KEY = "AIzaServerGoogleKey0123456789abcdef"
USER_OPENAI_KEY = "sk-user-openai-ZZ9988776655abcd"
USER_GOOGLE_KEY = "AIzaUserGoogleKeyZZ9988776655abcd"
DIMS = 8


def ai_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ai_enabled": True,
        "ai_llm_model": "test-llm",
        "ai_llm_context_tokens": 100_000,
        "ai_llm_max_output_tokens": 4_000,
        "ai_embed_model": "test-embed",
        "ai_embed_dimensions": DIMS,
        "ai_embed_max_input_tokens": 8_000,
        "ai_key_encryption_keys": generate_key_set_entry("t1"),
        "ai_openai_api_key": SERVER_OPENAI_KEY,
        "ai_google_api_key": SERVER_GOOGLE_KEY,
        "ai_max_input_tokens": 50_000,
        "ai_max_output_tokens": 2_000,
        "ai_llm_input_micros_per_mtok": 1_000_000,  # USD 1 per 1M input tokens
        "ai_llm_output_micros_per_mtok": 2_000_000,
        "ai_embed_text_micros_per_mtok": 200_000,
        "ai_embed_image_micros_per_mtok": 450_000,
        "ai_prices_checked_on": "2026-11-08",
        "ai_retry_base_delay_seconds": 0.001,
        "ai_retry_max_delay_seconds": 0.01,
    }
    values.update(overrides)
    return make_settings(**values)


def png_color(data: bytes) -> str:
    """Dominant colour name of a (stub-generated) solid picture."""
    r, g, b = Image.open(io.BytesIO(data)).convert("RGB").getpixel((1, 1))
    if r > 150 and g < 100 and b < 100:
        return "red"
    if b > 150 and r < 100 and g < 100:
        return "blue"
    if g > 150 and r < 100 and b < 100:
        return "green"
    return "other"


class Stub:
    """Both providers in one object. Every request is recorded in `requests` (method, path, headers,
    json) so tests can assert what was actually sent."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.valid_keys: set[str] = {
            SERVER_OPENAI_KEY,
            SERVER_GOOGLE_KEY,
            USER_OPENAI_KEY,
            USER_GOOGLE_KEY,
        }
        # one-shot scripted failures, consumed in order: (status, headers, body)
        self.llm_failures: list[tuple[int, dict[str, str], Any]] = []
        self.embed_failures: list[tuple[int, dict[str, str], Any]] = []
        self.llm_answer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.llm_usage: dict[str, int] | None = {"input_tokens": 120, "output_tokens": 30}
        self.llm_output_override: Any = None  # replace the text output with this raw string
        self.refuse = False
        self.incomplete = False
        self.break_pair_ranking = False
        self.wrong_dimension = False

    # ---- transports ----

    def transports(self) -> dict[str, httpx2.AsyncBaseTransport]:
        return {
            "openai": httpx2.MockTransport(self._openai),
            "google": httpx2.MockTransport(self._google),
        }

    def calls(self, path_part: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if path_part in r["path"]]

    def _record(self, request: httpx2.Request) -> dict[str, Any]:
        body = json.loads(request.content) if request.content else None
        entry = {
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
            "json": body,
        }
        self.requests.append(entry)
        return entry

    # ---- OpenAI ----

    def _openai(self, request: httpx2.Request) -> httpx2.Response:
        entry = self._record(request)
        key = entry["headers"].get("authorization", "").removeprefix("Bearer ")
        if key not in self.valid_keys:
            return httpx2.Response(
                401, json={"error": {"message": f"Incorrect API key provided: {key}"}}
            )
        if self.llm_failures:
            status, headers, body = self.llm_failures.pop(0)
            return httpx2.Response(status, headers=headers, json=body)
        if request.method == "GET":
            return httpx2.Response(200, json={"id": "test-llm", "object": "model"})
        body = entry["json"]
        if self.refuse:
            return httpx2.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}
                    ],
                },
            )
        if self.incomplete:
            return httpx2.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            )
        text = self._llm_text(body)
        payload: dict[str, Any] = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }
        if self.llm_usage is not None:
            payload["usage"] = self.llm_usage
        return httpx2.Response(200, json=payload)

    def _llm_text(self, body: dict[str, Any]) -> str:
        if self.llm_output_override is not None:
            value = self.llm_output_override
            if isinstance(value, list):  # a script: consumed one per call
                return value.pop(0)
            return value
        if self.llm_answer is not None:
            return json.dumps(self.llm_answer(body))
        fmt = (body.get("text") or {}).get("format") or {}
        name = fmt.get("name", "")
        content = body["input"][0]["content"]
        if name == "pong":
            return json.dumps({"answer": "PONG"})
        if name == "typed":
            return json.dumps({"color": "red", "count": 3})
        if name == "color":
            for part in content:
                if part["type"] == "input_image":
                    raw = base64.b64decode(part["image_url"].split(",", 1)[1])
                    return json.dumps({"color": png_color(raw)})
        return json.dumps({"answer": "ok"})

    # ---- Google ----

    def _google(self, request: httpx2.Request) -> httpx2.Response:
        entry = self._record(request)
        key = entry["headers"].get("x-goog-api-key", "")
        if key not in self.valid_keys:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": "API key not valid.",
                        "status": "INVALID_ARGUMENT",
                        "details": [{"reason": "API_KEY_INVALID"}],
                    }
                },
            )
        if self.embed_failures:
            status, headers, body = self.embed_failures.pop(0)
            return httpx2.Response(status, headers=headers, json=body)
        if request.method == "GET":
            return httpx2.Response(200, json={"name": "models/test-embed"})
        embeddings = []
        for req in entry["json"]["requests"]:
            embeddings.append({"values": self._vector(req["content"]["parts"][0])})
        return httpx2.Response(200, json={"embeddings": embeddings})

    def _vector(self, part: dict[str, Any]) -> list[float]:
        dims = DIMS + (1 if self.wrong_dimension else 0)
        vec = [0.0] * dims
        text = part.get("text")
        if text is not None:
            lowered = text.lower()
            slot = 0 if "red" in lowered else 1 if "blue" in lowered else 2
        else:
            raw = base64.b64decode(part["inline_data"]["data"])
            slot = {"red": 0, "blue": 1}.get(png_color(raw), 2)
            if self.break_pair_ranking:
                slot = 1 - slot if slot < 2 else slot
        vec[slot] = 1.0
        vec[3] = 0.05  # keep vectors non-degenerate
        return vec


def install(actor: Any, stub: Stub) -> None:
    """Point an actor's app at the stub transports (the app reads app.state.ai_transports)."""
    actor.client._transport.app.state.ai_transports = stub.transports()


def key_in(text: str) -> bool:
    """True if any of the test keys (or anything key-shaped from them) appears in the text."""
    return any(
        k in text for k in (SERVER_OPENAI_KEY, SERVER_GOOGLE_KEY, USER_OPENAI_KEY, USER_GOOGLE_KEY)
    ) or bool(re.search(r"sk-(user|server)-|AIza(User|Server)", text))
