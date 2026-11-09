"""Provider self-check: small real calls that prove each capability works with THIS configuration.

Run by the AI worker at start (and every AI_SELFCHECK_INTERVAL_SECONDS) and by the operator command
`python -m app.commands.ai_selfcheck`. It uses the SERVER keys only (a user's key is never touched by a
background check). Results go to `ai_capability_status`; GET /ai/status shows booleans and codes.

  text_generation    a structured call answers "PONG"
  structured_output  a typed reply (enum + integer + required fields) comes back schema-valid
  image_input        the model reads the colour of a generated solid-colour picture
  text_embedding     one vector with exactly AI_EMBED_DIMENSIONS finite numbers
  image_embedding    a picture scores higher with its matching text than with a mismatched one
                     (red picture vs "a solid red image" / "a solid blue image", and blue likewise)
  ocr_local          the tesseract binary exists and has English data

A failed capability disables only itself, except generation/structured output/text embedding, which
disable AI (see app/ai/status.py). A missing server key is recorded as "not_checked", never as ok.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import shutil
from dataclasses import dataclass
from typing import Any

from PIL import Image
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import errors as perr
from app.ai.calls import MeteredCaller
from app.ai.factory import Transports, build_embedder, build_llm
from app.ai.keys import server_key
from app.ai.pricing import Prices, estimate_parts_tokens
from app.ai.spend import BudgetExhausted, CallContext, Spend
from app.ai.status import CAPABILITIES, NOT_CHECKED
from app.ai.types import ImagePart, TextPart, Usage
from app.config import Settings
from app.logging_config import APP_LOGGER
from app.models import AiCapabilityStatus

log = logging.getLogger(f"{APP_LOGGER}.ai.selfcheck")
_CAPS = AiCapabilityStatus.__table__


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    model: str = ""
    detail: str = ""


def solid_png(color: tuple[int, int, int], size: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_PONG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
_TYPED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "color": {"type": "string", "enum": ["red", "green", "blue"]},
        "count": {"type": "integer"},
    },
    "required": ["color", "count"],
    "additionalProperties": False,
}
_COLOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"color": {"type": "string", "enum": ["red", "green", "blue", "other"]}},
    "required": ["color"],
    "additionalProperties": False,
}


async def run_selfcheck(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    transports: Transports | None = None,
    *,
    store: bool = True,
) -> dict[str, CheckResult]:
    spend = Spend(sessionmaker, settings)
    caller = MeteredCaller(settings, spend)
    prices = Prices.of(settings)
    results: dict[str, CheckResult] = {}

    async def guarded(name: str, check: Any) -> None:
        try:
            results[name] = await check()
        except perr.ProviderError as exc:
            results[name] = CheckResult(False, exc.code, detail=perr.scrub(str(exc)))
        except BudgetExhausted:
            results[name] = CheckResult(False, "budget_exhausted")
        except Exception as exc:  # noqa: BLE001 - a check must never crash the worker
            log.error("selfcheck %s crashed: %s", name, type(exc).__name__)
            results[name] = CheckResult(False, "check_crashed", detail=type(exc).__name__)

    # ---- generation (OpenAI) ----
    llm_key = server_key(settings, "openai")
    if llm_key:
        llm = build_llm(settings, llm_key, transports)
        ctx = CallContext(None, None, None, "openai", settings.ai_llm_model, "server")
        max_out = min(200, settings.ai_llm_max_output_tokens or 200)

        async def ask(system: str, parts: list[Any], schema: dict[str, Any], name: str) -> Any:
            est_in = estimate_parts_tokens(settings, [TextPart(system), *parts])
            worst = Usage(est_in, max_out, sum(isinstance(p, ImagePart) for p in parts))
            result = await caller.call(
                ctx,
                "self_check",
                llm_key,
                lambda: llm.generate_structured(
                    system=system,
                    parts=parts,
                    schema=schema,
                    schema_name=name,
                    max_output_tokens=max_out,
                ),
                usage_of=lambda r: r.usage,
                cost_of=prices.llm_cost,
                worst_case=worst,
            )
            return result.data

        async def text_generation() -> CheckResult:
            data = await ask(
                "You are a connectivity test.",
                [TextPart('Put the word "PONG" in the answer field.')],
                _PONG_SCHEMA,
                "pong",
            )
            ok = isinstance(data, dict) and "pong" in str(data.get("answer", "")).lower()
            return CheckResult(ok, "ok" if ok else "unexpected_answer", llm.model)

        async def structured_output() -> CheckResult:
            data = await ask(
                "You are a connectivity test.",
                [TextPart("Reply with color red and count 3.")],
                _TYPED_SCHEMA,
                "typed",
            )
            ok = (
                isinstance(data, dict)
                and set(data) == {"color", "count"}
                and data["color"] in ("red", "green", "blue")
                and isinstance(data["count"], int)
                and not isinstance(data["count"], bool)
            )
            return CheckResult(bool(ok), "ok" if ok else "schema_invalid", llm.model)

        async def image_input() -> CheckResult:
            data = await ask(
                "You are a connectivity test.",
                [
                    TextPart("What is the colour of this picture?"),
                    ImagePart(solid_png((220, 20, 20)), "image/png"),
                ],
                _COLOR_SCHEMA,
                "color",
            )
            ok = isinstance(data, dict) and data.get("color") == "red"
            return CheckResult(ok, "ok" if ok else "image_not_understood", llm.model)

        try:
            await guarded("text_generation", text_generation)
            await guarded("structured_output", structured_output)
            await guarded("image_input", image_input)
        finally:
            await llm.aclose()

    # ---- embeddings (Google) ----
    emb_key = server_key(settings, "google")
    if emb_key:
        emb = build_embedder(settings, emb_key, transports)
        ctx = CallContext(None, None, None, "google", settings.ai_embed_model, "server")

        async def embed(fn: Any, est_tokens: int, images: int, image_cost: bool) -> Any:
            worst = Usage(est_tokens, 0, images, estimated=True)
            cost = prices.embed_image_cost if image_cost else prices.embed_text_cost
            result = await caller.call(
                ctx,
                "self_check",
                emb_key,
                fn,
                usage_of=lambda r: r.usage,
                cost_of=cost,
                worst_case=worst,
            )
            return result.vectors

        async def text_embedding() -> CheckResult:
            vectors = await embed(lambda: emb.embed_texts(["hello world"]), 64, 0, False)
            ok = len(vectors) == 1 and len(vectors[0]) == settings.ai_embed_dimensions
            return CheckResult(ok, "ok" if ok else "dimension_mismatch", emb.model)

        async def image_embedding() -> CheckResult:
            images = [
                ImagePart(solid_png((220, 20, 20)), "image/png"),
                ImagePart(solid_png((20, 20, 220)), "image/png"),
            ]
            img = await embed(
                lambda: emb.embed_images(images),
                2 * settings.ai_embed_image_tokens_estimate,
                2,
                True,
            )
            txt = await embed(
                lambda: emb.embed_texts(["a solid red image", "a solid blue image"]), 64, 0, False
            )
            ok = cosine(img[0], txt[0]) > cosine(img[0], txt[1]) and cosine(
                img[1], txt[1]
            ) > cosine(img[1], txt[0])
            return CheckResult(ok, "ok" if ok else "pair_ranking_failed", emb.model)

        try:
            await guarded("text_embedding", text_embedding)
            await guarded("image_embedding", image_embedding)
        finally:
            await emb.aclose()

    results["ocr_local"] = await asyncio.to_thread(check_tesseract)

    if store:
        await _store(sessionmaker, settings, results)
    return results


def check_tesseract() -> CheckResult:
    """Installed and has English data. `tesseract --list-langs` is the documented way to list data."""
    import subprocess  # noqa: PLC0415

    binary = shutil.which("tesseract")
    if binary is None:
        return CheckResult(False, "tesseract_missing")
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "--list-langs"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return CheckResult(False, "tesseract_failed")
    languages = {line.strip() for line in (out.stdout + out.stderr).splitlines()}
    if "eng" not in languages:
        return CheckResult(False, "tesseract_no_english")
    return CheckResult(True, "ok", "tesseract")


async def _store(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    results: dict[str, CheckResult],
) -> None:
    models = {
        "text_generation": settings.ai_llm_model,
        "structured_output": settings.ai_llm_model,
        "image_input": settings.ai_llm_model,
        "text_embedding": settings.ai_embed_model,
        "image_embedding": settings.ai_embed_model,
        "ocr_local": "tesseract",
    }
    async with sessionmaker() as db, db.begin():
        await db.execute(delete(_CAPS))
        for name in CAPABILITIES:
            r = results.get(name)
            row = (
                {"ok": False, "code": NOT_CHECKED, "detail": "no server key configured"}
                if r is None
                else {"ok": r.ok, "code": r.code, "detail": r.detail[:300]}
            )
            await db.execute(insert(_CAPS).values(capability=name, model=models[name], **row))
