"""Run the file inspector in a sandboxed child process.

    inspect_file(path, kind, settings, scratch_dir)  ->  Inspection   (or raises InspectionRejected)

What the parent does (the child restricts itself further, see inspect_child.py):
  * starts `python -I inspect_child.py` with an EMPTY-ish environment (no DATABASE_URL, no secrets;
    only what Python needs to start) and an empty temporary working directory;
  * hands over the request on stdin, reads at most a few KB from stdout;
  * enforces a wall-clock timeout and kills the whole child on timeout, on too much output, or when
    this coroutine is cancelled (a client that hung up);
  * treats anything but a well-formed answer as a rejection, so a crash or an exploit attempt means
    the file is refused, never accepted.

Limits we have and do not have, plainly (docs/ai.md section 9):
  * POSIX: address-space, CPU, open-file and file-size rlimits are set inside the child.
  * Windows (development machines): NO rlimits; only the timeout, the scrubbed environment and the
    output cap. The child reports `rlimits: false` and we log a warning once.
  * No network namespace: the child cannot make sockets (it disables them), but a compromised native
    library could. Production should run the API container without outbound access to anything but
    what it needs, or run the inspector in a container of its own. That is not set up here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.logging_config import APP_LOGGER

log = logging.getLogger(f"{APP_LOGGER}.sources")
CHILD_SCRIPT = Path(__file__).with_name("inspect_child.py")
MAX_OUTPUT_BYTES = 16 * 1024
MAX_TEXT_BYTES = 25 * 1024 * 1024
_warned_no_rlimits = False


class InspectionRejected(Exception):
    """The file was refused. `code` is stable (stored on the upload, shown to the user)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Inspection:
    kind: str
    page_count: int | None
    text_page_count: int | None
    likely_scanned_pages: int | None
    image_count: int
    text_chars: int
    rlimits: bool


def _child_env() -> dict[str, str]:
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }
    if sys.platform == "win32":  # Python cannot start on Windows without these
        for name in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR"):
            if name in os.environ:
                env[name] = os.environ[name]
    return env


def _int(value: object, *, low: int = 0, high: int = 2**40) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("bad number")
    return value


def _optional(value: object) -> int | None:
    return None if value is None else _int(value)


def _parse(answer: bytes) -> Inspection:
    try:
        data = json.loads(answer)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if data.get("ok") is not True:
            code = data.get("code")
            message = data.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise ValueError("bad rejection")
            if len(code) > 48 or not code.replace("_", "").isalnum():
                raise ValueError("bad code")
            raise InspectionRejected(code, message[:200])
        kind = data["kind"]
        if kind not in ("pdf", "docx", "txt", "md"):
            raise ValueError("bad kind")
        return Inspection(
            kind=kind,
            page_count=_optional(data["page_count"]),
            text_page_count=_optional(data["text_page_count"]),
            likely_scanned_pages=_optional(data["likely_scanned_pages"]),
            image_count=_int(data["image_count"]),
            text_chars=_int(data["text_chars"]),
            rlimits=bool(data.get("rlimits")),
        )
    except InspectionRejected:
        raise
    except (ValueError, KeyError, TypeError):
        raise InspectionRejected("inspection_failed", "The file could not be inspected.") from None


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Everything the child writes until it closes stdout, but never more than cap + 1 bytes."""
    data = b""
    while len(data) <= cap:
        chunk = await stream.read(4096)
        if not chunk:
            break
        data += chunk
    return data


async def _drain(stream: asyncio.StreamReader) -> None:
    """Read what is left in a dead child's pipe (bounded) so the transport can close."""
    with contextlib.suppress(Exception):
        for _ in range(64):
            if not await asyncio.wait_for(stream.read(65536), 2):
                return


async def inspect_file(path: Path, kind: str, settings: Settings, scratch_dir: Path) -> Inspection:
    """`scratch_dir` is a directory inside the storage root (tmp/); a fresh empty working directory
    is made inside it and removed afterwards."""
    global _warned_no_rlimits
    request = {
        "path": str(path),
        "kind": kind,
        "limits": {
            "memory_bytes": settings.source_inspect_memory_mb * 1024 * 1024,
            "cpu_seconds": settings.source_inspect_timeout_seconds + 5,
            "max_pages": settings.source_max_pages,
            "max_text_bytes": min(settings.source_max_upload_bytes, MAX_TEXT_BYTES),
            "docx_max_uncompressed_bytes": settings.source_docx_max_uncompressed_bytes,
            "docx_max_entries": settings.source_docx_max_entries,
        },
    }
    await asyncio.to_thread(scratch_dir.mkdir, parents=True, exist_ok=True)
    workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, dir=scratch_dir, prefix="inspect-"))
    proc: asyncio.subprocess.Process | None = None
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
                _read_capped(proc.stdout, MAX_OUTPUT_BYTES), settings.source_inspect_timeout_seconds
            )
        except TimeoutError:
            raise InspectionRejected(
                "inspection_timeout", "Inspecting the file took too long."
            ) from None
        if len(answer) > MAX_OUTPUT_BYTES or not answer.strip():
            raise InspectionRejected("inspection_failed", "The file could not be inspected.")
        result = _parse(answer.strip().splitlines()[-1])
        if not result.rlimits and not _warned_no_rlimits:
            _warned_no_rlimits = True
            log.warning(
                "action=source_inspector_unlimited detail=no_rlimits_on_this_platform"
            )  # Windows development only
        return result
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        if proc is not None and proc.stdout is not None:
            await _drain(proc.stdout)  # lets asyncio close the pipe transport cleanly
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
