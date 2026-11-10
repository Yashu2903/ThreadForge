"""The proposal check: build the candidate document of a proposal with the shared editor schema.

The validation that matters ("is the document that would result from these operations a valid document?")
is the shared TypeScript package's job; Python cannot run it. So the worker starts ONE sandboxed Node child
(`packages/proposal-check`, the same code the collaboration service runs at apply time), writes a JSON
request to its stdin and reads one JSON reply: the same pattern, limits and scrubbed environment as the
Mermaid parse check. No database, no network and no files are touched by the child.

If Node or the script is missing the proposal cannot be validated, so the request FAILS
(`proposal_check_unavailable`, retryable): a proposal is never published unchecked. (A diagram's Mermaid
PARSE check, which an answer may publish `unchecked`, is different: the browser draws it safely either way.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ai.answer.mermaid import _child_env
from app.config import Settings


class CheckUnavailable(Exception):
    """Node or the script is missing, timed out or crashed: the proposal cannot be validated."""


@dataclass
class CheckOutcome:
    ok: bool
    errors: list[dict[str, Any]] = field(default_factory=list)  # {op, code, message, path?}
    doc: dict[str, Any] | None = None  # a draft's whole document
    changes: list[dict[str, Any]] = field(default_factory=list)
    inserted_chars: int = 0
    hashes: dict[str, str] = field(default_factory=dict)


def default_script() -> Path:
    return Path(__file__).resolve().parents[4] / "packages" / "proposal-check" / "dist" / "cli.js"


def script_path(settings: Settings) -> Path:
    return (
        Path(settings.ai_proposal_check_script)
        if settings.ai_proposal_check_script
        else default_script()
    )


def available(settings: Settings) -> bool:
    return bool(shutil.which(settings.ai_node_command)) and script_path(settings).is_file()


async def run_check(settings: Settings, request: dict[str, Any]) -> CheckOutcome:
    node = shutil.which(settings.ai_node_command)
    if node is None or not script_path(settings).is_file():
        raise CheckUnavailable("missing")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            str(script_path(settings)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_env(),
            cwd=str(script_path(settings).parent),
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(request).encode("utf-8")),
            settings.ai_proposal_check_timeout_seconds,
        )
        data = json.loads(out.decode("utf-8")[:8_000_000])
    except (TimeoutError, OSError, ValueError) as error:
        raise CheckUnavailable(type(error).__name__) from None
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
    if not isinstance(data, dict) or "ok" not in data:
        raise CheckUnavailable("bad_reply")
    if not data["ok"]:
        errors = data.get("errors")
        if any(e.get("code") == "check_crashed" for e in errors or [] if isinstance(e, dict)):
            raise CheckUnavailable("crashed")
        return CheckOutcome(False, [e for e in (errors or []) if isinstance(e, dict)][:10])
    return CheckOutcome(
        True,
        doc=data.get("doc"),
        changes=list(data.get("changes") or []),
        inserted_chars=int(data.get("inserted_chars") or 0),
        hashes=dict(data.get("hashes") or {}),
    )
