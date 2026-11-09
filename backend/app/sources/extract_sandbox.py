"""Run the extractor (app/sources/extract_child.py) in a sandboxed child process.

    extraction = await extract_file(path, kind, settings, scratch_dir)   # raises ExtractionFailed
    ...use extraction.pages / .blocks / .figures / extraction.image_path(name)...
    await extraction.cleanup()                                           # always, even on errors

Same containment as the inspector (app/sources/sandbox.py): scrubbed environment, its own empty
working directory, a wall-clock deadline after which the whole child is killed, POSIX resource limits
set by the child itself, no sockets. The child hands its results back as FILES in that working
directory (result.json plus page and figure images), because a document can yield megabytes of text
and dozens of images; the parent reads result.json with a size cap and validates every field before
it trusts it. Anything malformed means the extraction failed, never that the file is half-accepted.

What the child may run: Tesseract, by the ABSOLUTE path resolved here (the child's PATH is empty).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.logging_config import APP_LOGGER
from app.sources.sandbox import MAX_TEXT_BYTES, _child_env, _drain, _read_capped

log = logging.getLogger(f"{APP_LOGGER}.sources")
CHILD_SCRIPT = Path(__file__).with_name("extract_child.py")
MAX_STDOUT_BYTES = 16 * 1024
MAX_RESULT_BYTES = 80 * 1024 * 1024
_IMAGE_NAME = re.compile(r"[pf][0-9]{1,5}\.png")
_warned_no_rlimits = False


class ExtractionFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PageResult:
    page: int
    extraction: str  # text_layer | tesseract | none
    text: str
    ocr_confidence: float | None
    status: str  # ok | empty | failed
    reason: str | None
    image: str | None  # file name of the page image when it must go to the vision fallback
    tess_words: list[str]


@dataclass(frozen=True)
class Block:
    t: str  # heading | para | list | table
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class FigureCandidate:
    file: str
    page: int | None
    after_block: int | None
    origin: str  # page | embedded | docx
    sha256: str
    width: int
    height: int


@dataclass
class Extraction:
    kind: str
    workdir: Path
    pages: list[PageResult]
    blocks: list[Block]
    toc: list[dict]
    figures: list[FigureCandidate]
    figures_found: int
    figures_capped: bool
    chars: int
    ocr_pages: int
    rlimits: bool

    def image_path(self, name: str) -> Path:
        if not _IMAGE_NAME.fullmatch(name):
            raise ExtractionFailed("extraction_failed", "Unexpected file name from the extractor.")
        path = (self.workdir / name).resolve()
        if path.parent != self.workdir.resolve() or not path.is_file():
            raise ExtractionFailed("extraction_failed", "A page or figure image is missing.")
        return path

    async def cleanup(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self.workdir, ignore_errors=True)


def _limits(settings: Settings) -> dict:
    tesseract = shutil.which(settings.ai_tesseract_command)
    return {
        "memory_bytes": settings.ai_extract_memory_mb * 1024 * 1024,
        "cpu_seconds": settings.ai_extract_timeout_seconds + 30,
        "max_pages": settings.source_max_pages,
        "max_text_bytes": min(settings.source_max_upload_bytes, MAX_TEXT_BYTES),
        "docx_max_uncompressed_bytes": settings.source_docx_max_uncompressed_bytes,
        "max_extracted_chars": settings.ai_max_extracted_chars,
        "tesseract": tesseract,
        "ocr_dpi": settings.ai_ocr_dpi,
        "ocr_timeout": settings.ai_ocr_timeout_seconds,
        "ocr_min_confidence": settings.ai_ocr_min_confidence,
        "ocr_min_words": settings.ai_ocr_min_words_per_page,
        "ocr_max_implausible": settings.ai_ocr_max_implausible_share,
        "max_ocr_pages": settings.ai_ocr_max_pages_per_source,
        "vision_max_pages": settings.ai_vision_fallback_max_pages,
        "figure_min_pixels": settings.ai_figure_min_pixels,
        "figure_max_repeats": settings.ai_figure_max_repeats,
        "max_figures": settings.ai_max_figures_per_source,
    }


def _str(value: object, limit: int = 2_000_000) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("bad string")
    return value


def _int(value: object, low: int = 0, high: int = 2**31) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("bad int")
    return value


def _parse(data: object, workdir: Path, rlimits: bool) -> Extraction:
    if not isinstance(data, dict):
        raise ValueError("not an object")
    kind = _str(data["kind"], 8)
    pages = [
        PageResult(
            page=_int(p["page"], 1),
            extraction=_str(p["extraction"], 16),
            text=_str(p["text"]),
            ocr_confidence=None if p.get("ocr_confidence") is None else float(p["ocr_confidence"]),
            status=_str(p["status"], 12),
            reason=None if p.get("reason") is None else _str(p["reason"], 48),
            image=None if p.get("image") is None else _str(p["image"], 24),
            tess_words=[_str(w, 60) for w in (p.get("tess_words") or [])][:400],
        )
        for p in (data.get("pages") or [])
    ]
    blocks = []
    for b in data.get("blocks") or []:
        t = _str(b["t"], 8)
        if t not in ("heading", "para", "list", "table"):
            raise ValueError("bad block")
        rows = [[_str(c, 20000) for c in row] for row in (b.get("rows") or [])]
        blocks.append(Block(t, _str(b.get("text", "")), _int(b.get("level", 0), 0, 9), rows))
    figures = [
        FigureCandidate(
            file=_str(f["file"], 24),
            page=None if f.get("page") is None else _int(f["page"], 1),
            after_block=None if f.get("after_block") is None else _int(f["after_block"], 0),
            origin=_str(f["origin"], 12),
            sha256=_str(f["sha256"], 64),
            width=_int(f["width"], 1, 20000),
            height=_int(f["height"], 1, 20000),
        )
        for f in (data.get("figures") or [])
    ]
    toc = [
        {"title": _str(t["title"], 200), "level": _int(t["level"], 0, 20), "page": t.get("page")}
        for t in (data.get("toc") or [])
    ]
    stats = data.get("stats") or {}
    return Extraction(
        kind=kind,
        workdir=workdir,
        pages=pages,
        blocks=blocks,
        toc=toc,
        figures=figures,
        figures_found=_int(data.get("figures_found", 0)),
        figures_capped=bool(data.get("figures_capped", False)),
        chars=_int(data.get("chars", 0), 0, 2**40),
        ocr_pages=_int(stats.get("ocr_pages", 0)),
        rlimits=rlimits,
    )


async def extract_file(path: Path, kind: str, settings: Settings, scratch_dir: Path) -> Extraction:
    """`scratch_dir` is a directory inside the storage root (tmp/); a fresh working directory is made
    inside it. The caller MUST call `cleanup()` on the result (and on failure it is done here)."""
    global _warned_no_rlimits
    request = {"path": str(path), "kind": kind, "limits": _limits(settings)}
    await asyncio.to_thread(scratch_dir.mkdir, parents=True, exist_ok=True)
    workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, dir=scratch_dir, prefix="inspect-x-"))
    proc: asyncio.subprocess.Process | None = None
    succeeded = False
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(CHILD_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workdir,
            env=_child_env(),
        )
        assert proc.stdin is not None and proc.stdout is not None  # noqa: S101 - just created
        proc.stdin.write(json.dumps(request).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            answer = await asyncio.wait_for(
                _read_capped(proc.stdout, MAX_STDOUT_BYTES), settings.ai_extract_timeout_seconds
            )
        except TimeoutError:
            raise ExtractionFailed(
                "extraction_timeout", "Reading the file took too long."
            ) from None
        if len(answer) > MAX_STDOUT_BYTES or not answer.strip():
            raise ExtractionFailed("extraction_failed", "The file could not be read.")
        try:
            outcome = json.loads(answer.strip().splitlines()[-1])
            if not isinstance(outcome, dict):
                raise ValueError("not an object")
        except ValueError:
            raise ExtractionFailed("extraction_failed", "The file could not be read.") from None
        if outcome.get("ok") is not True:
            code = outcome.get("code")
            message = outcome.get("message")
            if not (
                isinstance(code, str)
                and isinstance(message, str)
                and len(code) <= 48
                and code.replace("_", "").isalnum()
            ):
                raise ExtractionFailed("extraction_failed", "The file could not be read.")
            raise ExtractionFailed(code, message[:200])
        rlimits = bool(outcome.get("rlimits"))
        result_file = workdir / "result.json"
        size = result_file.stat().st_size if result_file.is_file() else 0
        if not 0 < size <= MAX_RESULT_BYTES:
            raise ExtractionFailed("extraction_failed", "The file could not be read.")
        raw = await asyncio.to_thread(result_file.read_bytes)
        try:
            extraction = _parse(json.loads(raw), workdir, rlimits)
        except (ValueError, KeyError, TypeError):
            raise ExtractionFailed("extraction_failed", "The file could not be read.") from None
        if not rlimits and not _warned_no_rlimits:
            _warned_no_rlimits = True
            log.warning("action=source_extractor_unlimited detail=no_rlimits_on_this_platform")
        succeeded = True
        return extraction
    finally:
        # the child must be dead BEFORE its working directory is removed (Windows keeps a
        # running process's cwd locked)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        if proc is not None and proc.stdout is not None:
            await _drain(proc.stdout)
        if not succeeded:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
