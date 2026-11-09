"""Analysis of ONE source version: the worker's whole job, start to finish.

    outcome = await run_job(sessionmaker, settings, storage, job, token)

STAGES (the job's `stage`, shown to users as the version's analysis_state):
  extracting  the sandboxed child reads the file: text layer, local OCR (Tesseract) of pages without
              one, figure candidates                                    -> state `extracting`
  ocr         pages local OCR could not read go to the vision model for a transcription-only pass;
              pages still unreadable become recorded GAPS                -> `extracting`
  figures     each non-decorative figure is stored (re-encoded) and described by the vision model
  chunking    units -> passages (chunk-v1), figure passages appended     -> `extracting`
  embedding   passages are embedded in batches and written to a `building` generation  -> `indexing`
  publishing  validate, then switch the version's pointer atomically     -> `indexing` -> `ready`

GUARANTEES
  * before ANY file or model access the requester is checked again with the same gate used at
    admission (role, can_write on the anchor document, switches, consent). Losing access ends the job.
  * a heartbeat renews the lease while a stage runs; if the job is cancelled, loses its claim or its
    source is removed, the stage is interrupted at once.
  * no database transaction is open while a provider call or the child process runs.
  * a failure leaves nothing visible: building rows and figure files are deleted, the previous ready
    generation (if any) stays current.
  * the extractor's scratch directory is always removed.

Log lines carry ids and codes only; file names and content never appear.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import access
from app.ai import errors as perr
from app.ai.analysis import jobs, store, vision
from app.ai.analysis.chunking import DraftChunk, Unit, assign_sections, chunk_units, tokens
from app.ai.analysis.gateway import (
    DESCRIBE_MAX_OUTPUT_TOKENS,
    OCR_MAX_OUTPUT_TOKENS,
    AnalysisAbort,
    AnalysisCancelled,
    AnalysisGateway,
)
from app.ai.status import load_status
from app.config import Settings
from app.errors import AppError
from app.logging_config import APP_LOGGER
from app.services.memberships import get_active_role
from app.sources.extract_sandbox import Extraction, ExtractionFailed, extract_file
from app.storage import Storage

log = logging.getLogger(f"{APP_LOGGER}.ai.analysis")
SKIPPABLE_PROVIDER_CODES = {
    perr.BAD_OUTPUT,
    perr.REFUSED,
    perr.BAD_REQUEST,
    perr.INCOMPLETE,
    perr.INPUT_TOO_LARGE,
}
EMPTY_PAGE_REASONS = {"empty"}


@dataclass
class Report:
    """What the run found out, for warnings, gaps and the job summary."""

    warnings: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    pages_total: int | None = None
    pages_unreadable: int = 0
    figures_found: int = 0
    figures_kept: int = 0
    stored_keys: list[str] = field(default_factory=list)


# ---- units: from the extractor's output to something chunk_units understands ---------------------------


def split_paragraphs(raw: str) -> list[str]:
    """Blank lines separate paragraphs; single line breaks inside one are wrapping, so they become
    spaces (except before list markers, where they are real line breaks)."""
    out = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "").strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        joined = lines[0]
        for ln in lines[1:]:
            joined += ("\n" if re.match(r"^([-*•]|\d{1,3}[.)])\s", ln) else " ") + ln
        out.append(joined)
    return out


def toc_paths(toc: list[dict], pages: int) -> dict[int, tuple[str, ...]]:
    """The outline heading path in force on each page (empty when the PDF has no outline)."""
    entries = sorted(
        (t for t in toc if isinstance(t.get("page"), int)), key=lambda t: (t["page"], t["level"])
    )
    result: dict[int, tuple[str, ...]] = {}
    stack: list[tuple[int, str]] = []
    i = 0
    for page in range(1, pages + 1):
        while i < len(entries) and entries[i]["page"] <= page:
            level, title = entries[i]["level"], entries[i]["title"].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            if title:
                stack.append((level, title))
            i += 1
        result[page] = tuple(t for _lv, t in stack)
    return result


def heading_path_at(blocks: list, index: int) -> tuple[str, ...]:
    stack: list[tuple[int, str]] = []
    for block in blocks[:index]:
        if block.t == "heading":
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text.strip()))
    return tuple(t for _lv, t in stack)


@dataclass(frozen=True)
class PageText:
    text: str
    extraction: str
    conf: float | None = None
    low_agreement: bool = False


def pdf_units(extraction: Extraction, texts: dict[int, PageText]) -> list[Unit]:
    paths = toc_paths(extraction.toc, len(extraction.pages))
    units: list[Unit] = []
    for page in extraction.pages:
        got = texts.get(page.page)
        if got is None:
            continue
        for para in split_paragraphs(got.text):
            units.append(
                Unit(
                    "para",
                    para,
                    page=page.page,
                    extraction=got.extraction,
                    ocr_confidence=got.conf,
                    heading_path=paths.get(page.page, ()),
                    low_agreement=got.low_agreement,
                )
            )
    return units


def block_units(extraction: Extraction) -> list[Unit]:
    default = "docx" if extraction.kind == "docx" else "plain"
    return [
        Unit(
            b.t,
            b.text,
            level=b.level,
            rows=tuple(tuple(r) for r in b.rows),
            extraction=default,
        )
        for b in extraction.blocks
    ]


# ---- the run ------------------------------------------------------------------------------------------------


class _Interrupted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def run_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    job: object,
    token: uuid.UUID,
    *,
    transports: dict[str, httpx2.AsyncBaseTransport] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Run a claimed job to an end. Returns `succeeded`, `cancelled`, `requeued` or `failed:<code>`."""
    job_id, version_id = job.id, job.source_version_id  # type: ignore[attr-defined]
    interrupted: list[str] = []

    async def heartbeat() -> None:
        interval = max(1.0, settings.ai_analysis_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            reason = await jobs.renew(sessionmaker, settings, job_id, token)
            if reason is not None:
                interrupted.append(reason)
                work.cancel()
                return

    work = asyncio.ensure_future(
        _work(sessionmaker, settings, storage, job, token, transports, sleep)
    )
    beat = asyncio.ensure_future(heartbeat())
    try:
        try:
            await work
            outcome = "succeeded"
        except asyncio.CancelledError:
            if not interrupted:
                raise  # the worker process itself is shutting down: leave the lease to expire
            reason = interrupted[0]
            if reason == "lost_claim":
                outcome = "lost_claim"
            else:
                await jobs.finish_cancelled(sessionmaker, job_id, token, version_id)
                outcome = "cancelled"
        except _Interrupted as stop:
            if stop.reason == "lost_claim":
                outcome = "lost_claim"
            else:
                await jobs.finish_cancelled(sessionmaker, job_id, token, version_id)
                outcome = "cancelled"
        except AnalysisCancelled:
            await jobs.finish_cancelled(sessionmaker, job_id, token, version_id)
            outcome = "cancelled"
        except AnalysisAbort as abort:
            if abort.retryable and await jobs.requeue(
                sessionmaker, settings, job_id, token, version_id, abort.code
            ):
                outcome = "requeued"
            else:
                if not abort.retryable:
                    await jobs.finish_failed(sessionmaker, job_id, token, version_id, abort.code)
                outcome = f"failed:{abort.code}"
        except ExtractionFailed as failure:
            await jobs.finish_failed(sessionmaker, job_id, token, version_id, failure.code)
            outcome = f"failed:{failure.code}"
        except store.IndexIncomplete:
            await jobs.finish_failed(sessionmaker, job_id, token, version_id, "index_incomplete")
            outcome = "failed:index_incomplete"
        except AppError as denied:  # the gate: access, consent, switches
            await jobs.finish_failed(sessionmaker, job_id, token, version_id, denied.code)
            outcome = f"failed:{denied.code}"
        except Exception:
            log.exception("action=analysis_crashed job_id=%s", job_id)
            if await jobs.requeue(
                sessionmaker, settings, job_id, token, version_id, "internal_error"
            ):
                outcome = "requeued"
            else:
                outcome = "failed:internal_error"
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beat
    log.info(
        "action=analysis_finished job_id=%s version_id=%s outcome=%s", job_id, version_id, outcome
    )
    return outcome


async def _gate(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, job: object
) -> None:
    """The same admission gate as the request, re-run by the worker BEFORE it touches the file or any
    provider. Raises AppError with the gate's own code."""
    role = await get_active_role(sessionmaker, job.room_id, job.requested_by)  # type: ignore[attr-defined]
    if role is None:
        raise AppError(403, "room_access_lost", "The requester is no longer in this room.")
    async with sessionmaker() as db:
        await access.gate(
            db,
            settings,
            user_id=job.requested_by,  # type: ignore[attr-defined]
            role=role,
            room_id=job.room_id,  # type: ignore[attr-defined]
            document_id=job.anchor_document_id,  # type: ignore[attr-defined]
        )


async def _work(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    job: object,
    token: uuid.UUID,
    transports: dict[str, httpx2.AsyncBaseTransport] | None,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    job_id, version_id = job.id, job.source_version_id  # type: ignore[attr-defined]
    await _gate(sessionmaker, settings, job)

    async def check(stage: str | None = None) -> None:
        reason = await jobs.renew(sessionmaker, settings, job_id, token, stage=stage)
        if reason is not None:
            raise _Interrupted(reason)

    async def is_cancelled() -> bool:
        return await jobs.still_wanted(sessionmaker, job_id, token) is not None

    async with sessionmaker() as db:
        version = (
            await db.execute(
                text(
                    "SELECT id, kind, storage_key, analysis_state, page_count "
                    "FROM source_versions WHERE id = :v"
                ),
                {"v": version_id},
            )
        ).one()
        status = await load_status(db, settings)
    if version.analysis_state == "removed" or version.storage_key is None:
        raise _Interrupted("source_removed")
    vision_ok = status.capability_ok("image_input")
    image_embed_ok = status.capability_ok("image_embedding")

    gateway = AnalysisGateway(
        sessionmaker, settings, job, is_cancelled=is_cancelled, transports=transports, sleep=sleep
    )
    report = Report()
    extraction: Extraction | None = None
    generation_id: uuid.UUID | None = None
    published = False
    try:
        # ---- 1. extract ---------------------------------------------------------------------
        await check("extracting")
        extraction = await extract_file(
            storage.path_for("sources", version.storage_key),
            version.kind,
            settings,
            storage.tmp_dir,
        )
        caps = job.caps  # type: ignore[attr-defined]
        if extraction.pages and len(extraction.pages) > int(caps.get("max_pages", 10**9)):
            raise AnalysisAbort("analysis_cap_exceeded")

        # ---- 2. OCR fallback (vision) ---------------------------------------------------------
        await check("ocr")
        texts = await _read_pages(extraction, gateway, report, vision_ok, check, caps)
        report.pages_total = len(extraction.pages) if extraction.pages else None
        if extraction.pages:
            readable_pages = [p for p in extraction.pages if p.status != "empty"]
            if readable_pages and (
                report.pages_unreadable / len(readable_pages) > settings.ai_unreadable_fail_share
            ):
                raise AnalysisAbort("too_many_unreadable_pages")
            if report.pages_unreadable:
                report.warnings.append(
                    {
                        "code": "unreadable_pages",
                        "pages": [g["page"] for g in report.gaps if g.get("page")],
                        "count": report.pages_unreadable,
                    }
                )

        # ---- 3. figures -----------------------------------------------------------------------
        await check("figures")
        figure_rows = await _read_figures(
            extraction, gateway, storage, settings, report, vision_ok, check, caps
        )

        # ---- 4. chunk -------------------------------------------------------------------------
        await check("chunking")
        units = pdf_units(extraction, texts) if version.kind == "pdf" else block_units(extraction)
        drafts = chunk_units(settings, units)
        if len(drafts) > settings.ai_max_chunks_per_source:
            report.warnings.append(
                {
                    "code": "chunks_capped",
                    "indexed": settings.ai_max_chunks_per_source,
                    "found": len(drafts),
                }
            )
            drafts = drafts[: settings.ai_max_chunks_per_source]
        rows = [store.ChunkRow(d) for d in drafts]
        for fig in figure_rows:
            fig.draft.ordinal = len(rows)
            rows.append(fig)
        for i, row in enumerate(rows):
            row.draft.ordinal = i
        assign_sections([r.draft for r in rows if r.kind == "text"])
        if not rows:
            raise AnalysisAbort("no_extractable_text")

        # ---- 5. embed + write the building generation ------------------------------------------
        await check("embedding")
        mode = settings.ai_figure_embedding_mode
        if mode != "description" and not image_embed_ok:
            report.warnings.append({"code": "image_embedding_unavailable", "used": "description"})
            mode = "description"
        generation_id = await store.start_generation(
            sessionmaker,
            settings,
            storage,
            job,
            fingerprint=job.config_fingerprint,
            figure_mode=mode,  # type: ignore[attr-defined]
        )
        await _embed_and_store(
            sessionmaker, settings, gateway, job, generation_id, rows, mode, check
        )

        # ---- 6. publish -----------------------------------------------------------------------
        await check("publishing")
        summary = {
            "chunks": len(rows),
            "figures": report.figures_kept,
            "pages": report.pages_total,
            "unreadable_pages": report.pages_unreadable,
            "warnings": [w["code"] for w in report.warnings],
        }
        result = await store.publish(
            sessionmaker,
            job,
            token,
            generation_id,
            expected_chunks=len(rows),
            expected_dims=settings.ai_embed_dimensions,
            summary=summary,
            warnings=report.warnings,
            gaps=report.gaps,
            pages_total=report.pages_total,
            pages_unreadable=report.pages_unreadable,
            figures_found=report.figures_found,
            figure_count=report.figures_kept,
        )
        if result is None:
            raise _Interrupted("cancelled")
        published = True
        # the superseded generation stays for AI_SUPERSEDED_GENERATION_GRACE_HOURS: a request that
        # pinned it keeps reading it. cleanup_ai deletes it later, unless something still pins it.
    finally:
        await gateway.aclose()
        if extraction is not None:
            await extraction.cleanup()
        if not published:
            with contextlib.suppress(Exception):
                if generation_id is not None:
                    await store.discard_generations(sessionmaker, storage, [generation_id])
                for key in report.stored_keys:
                    with contextlib.suppress(OSError):
                        storage.delete(store.FIGURE_NAMESPACE, key)


async def _read_pages(
    extraction: Extraction,
    gateway: AnalysisGateway,
    report: Report,
    vision_ok: bool,
    check: Callable[..., Awaitable[None]],
    caps: dict,
) -> dict[int, PageText]:
    """The text of every PDF page: text layer, accepted Tesseract output, or a vision transcription.
    Pages nobody could read become gaps."""
    texts: dict[int, PageText] = {}
    for page in extraction.pages:
        if page.status == "empty":
            continue
        if page.status == "ok":
            texts[page.page] = PageText(page.text, page.extraction, page.ocr_confidence)
            continue
        if page.image is None or not vision_ok:
            reason = (
                "vision_unavailable"
                if page.image and not vision_ok
                else (page.reason or "unreadable")
            )
            report.gaps.append({"page": page.page, "reason": reason})
            report.pages_unreadable += 1
            continue
        await check()
        png = await asyncio.to_thread(extraction.image_path(page.image).read_bytes)
        try:
            data = await gateway.vision_structured(
                "vision_ocr",
                png,
                system=vision.OCR_SYSTEM,
                user=vision.OCR_USER,
                schema=vision.OCR_SCHEMA,
                schema_name=vision.OCR_SCHEMA_NAME,
                max_output_tokens=min(OCR_MAX_OUTPUT_TOKENS, gateway.settings.ai_max_output_tokens),
            )
            got = vision.parse_transcription(data, page.tess_words)
        except AnalysisAbort as abort:
            if abort.code in SKIPPABLE_PROVIDER_CODES:
                report.gaps.append({"page": page.page, "reason": abort.code})
                report.pages_unreadable += 1
                continue
            raise
        except ValueError:
            report.gaps.append({"page": page.page, "reason": "bad_transcription"})
            report.pages_unreadable += 1
            continue
        if got.unreadable:
            report.gaps.append({"page": page.page, "reason": "illegible"})
            report.pages_unreadable += 1
            continue
        texts[page.page] = PageText(got.text, "vision_ocr", None, got.low_agreement)
        if got.low_agreement:
            report.warnings.append({"code": "low_agreement", "page": page.page})
    return texts


async def _read_figures(
    extraction: Extraction,
    gateway: AnalysisGateway,
    storage: Storage,
    settings: Settings,
    report: Report,
    vision_ok: bool,
    check: Callable[..., Awaitable[None]],
    caps: dict,
) -> list[store.ChunkRow]:
    report.figures_found = extraction.figures_found
    if not extraction.figures:
        return []
    if not vision_ok:
        report.warnings.append(
            {
                "code": "figures_not_analyzed",
                "reason": "vision_unavailable",
                "found": extraction.figures_found,
            }
        )
        return []
    limit = int(caps.get("max_figures", settings.ai_max_figures_per_source))
    rows: list[store.ChunkRow] = []
    for candidate in extraction.figures[:limit]:
        await check()
        stored = await asyncio.to_thread(
            store.store_figure, storage, extraction.image_path(candidate.file)
        )
        if stored is None:
            report.warnings.append({"code": "figure_rejected", "page": candidate.page})
            continue
        report.stored_keys.append(stored.storage_key)
        number = len(rows) + 1
        try:
            data = await gateway.vision_structured(
                "vision_describe",
                stored.data,
                system=vision.DESCRIBE_SYSTEM,
                user=vision.DESCRIBE_USER,
                schema=vision.DESCRIBE_SCHEMA,
                schema_name=vision.DESCRIBE_SCHEMA_NAME,
                max_output_tokens=min(
                    DESCRIBE_MAX_OUTPUT_TOKENS, gateway.settings.ai_max_output_tokens
                ),
            )
            described = vision.parse_description(data, number, candidate.page)
        except (AnalysisAbort, ValueError) as problem:
            if isinstance(problem, AnalysisAbort) and problem.code not in SKIPPABLE_PROVIDER_CODES:
                raise
            report.warnings.append({"code": "figure_not_described", "page": candidate.page})
            with contextlib.suppress(OSError):
                storage.delete(store.FIGURE_NAMESPACE, stored.storage_key)
            report.stored_keys.remove(stored.storage_key)
            continue
        path = (
            heading_path_at(extraction.blocks, candidate.after_block)
            if candidate.after_block is not None
            else ()
        )
        draft = DraftChunk(
            text=described.text,
            page_start=candidate.page,
            page_end=candidate.page,
            heading_path=path,
            extraction="vision",
            ocr_confidence=None,
            low_agreement=False,
            token_count=tokens(settings, described.text),
        )
        rows.append(
            store.ChunkRow(
                draft,
                kind="figure",
                figure_number=number,
                figure=stored,
                page=candidate.page,
                locator_extra={"figure_number": number, "figure_kind": described.kind},
            )
        )
    report.figures_kept = len(rows)
    attempted = min(len(extraction.figures), limit)
    if (
        extraction.figures_found > attempted
    ):  # the cap left real figures unanalyzed (skips do not count)
        report.warnings.append(
            {"code": "figures_capped", "analyzed": len(rows), "found": extraction.figures_found}
        )
    if extraction.figures_capped:
        report.warnings.append({"code": "figure_candidates_capped"})
    return rows


def embed_input(row: store.ChunkRow) -> str:
    """What is embedded for a text passage: its heading path (if the text does not start with it)
    and the text. The STORED text stays exactly the passage."""
    path = " > ".join(row.draft.heading_path)
    if path and not row.draft.text.startswith(row.draft.heading_path[-1]):
        return f"{path}\n{row.draft.text}"
    return row.draft.text


async def _embed_and_store(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    gateway: AnalysisGateway,
    job: object,
    generation_id: uuid.UUID,
    rows: list[store.ChunkRow],
    mode: str,
    check: Callable[..., Awaitable[None]],
) -> None:
    dims = settings.ai_embed_dimensions
    text_rows = [r for r in rows if r.kind == "text"]
    figure_rows = [r for r in rows if r.kind == "figure"]
    size = settings.ai_embed_batch_size
    for start in range(0, len(text_rows), size):
        await check()
        batch = text_rows[start : start + size]
        vectors = await gateway.embed_texts([embed_input(r) for r in batch])
        await store.insert_chunks(sessionmaker, job, generation_id, batch, vectors, dims)
    for start in range(0, len(figure_rows), 6):
        await check()
        batch = figure_rows[start : start + 6]
        if mode == "image":
            vectors = await gateway.embed_images([r.figure.data for r in batch if r.figure])
        elif mode == "image_plus_description":
            vectors = await gateway.embed_interleaved(
                [(r.draft.text, r.figure.data) for r in batch if r.figure]
            )
        else:
            vectors = await gateway.embed_texts([r.draft.text for r in batch])
        await store.insert_chunks(sessionmaker, job, generation_id, batch, vectors, dims)


async def process_next(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Storage,
    *,
    transports: dict[str, httpx2.AsyncBaseTransport] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str | None:
    """Claim one job (if any) and run it. None when the queue is empty."""
    claimed = await jobs.claim_next(sessionmaker, settings)
    if claimed is None:
        return None
    job, token = claimed
    return await run_job(
        sessionmaker, settings, storage, job, token, transports=transports, sleep=sleep
    )
