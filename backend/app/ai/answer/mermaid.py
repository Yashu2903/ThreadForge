"""Mermaid diagrams in answers: what is allowed, and the parse check.

TWO LAYERS, deliberately:

1. STATIC RULES (this file, mirrored by `validateMermaidSource` in packages/editor-schema, with the same
   test cases): pure text checks that need no Mermaid at all. A diagram must be one of the allowed
   types, at most 20,000 characters and 500 lines, and may contain no directives (`%%{init}%%` can
   change Mermaid's security level), no `click` / `href` / `link` interaction, no HTML tags, no URLs,
   no `javascript:` / `data:` schemes and no HTML entities. These hold even if Node is not installed.
2. THE PARSE CHECK: a real Mermaid parse. DECISION: it runs SERVER-SIDE in a sandboxed Node child
   (`packages/mermaid-check`, Mermaid under jsdom, no network, scrubbed environment, wall-clock
   deadline), because the answer path wants to repair a bad diagram once with the parser's own error
   message, and a diagram that cannot be parsed should be known before it is published. Measured on
   this machine: about 3.4 s cold start for a batch, about 20 ms per diagram after that. The browser
   parses again when it renders (SafeMermaid) and shows the source with a note if it cannot. If Node or
   the script is not available, diagrams are published `unchecked` (the browser decides), never
   silently marked ok.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

MAX_CHARS = 20_000
MAX_LINES = 500
ALLOWED_TYPES = (
    "flowchart",
    "graph",
    "erDiagram",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "gantt",
    "mindmap",
    "timeline",
)
_FIRST = re.compile(r"^(" + "|".join(re.escape(t) for t in ALLOWED_TYPES) + r")(?![\w-])")
_FORBIDDEN_LINE = re.compile(
    r"^\s*(click|href|link|callback|linkStyle\s+\S+\s+href)\b", re.IGNORECASE
)
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("html_not_allowed", re.compile(r"<\s*/?\s*[A-Za-z!]")),
    ("url_not_allowed", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|\bwww\.", re.IGNORECASE)),
    ("scheme_not_allowed", re.compile(r"\b(javascript|data|vbscript)\s*:", re.IGNORECASE)),
    ("entity_not_allowed", re.compile(r"&#?[A-Za-z0-9]+;")),
    ("interaction_not_allowed", re.compile(r"\bhref\b|\bonclick\b|\bon[a-z]+\s*=", re.IGNORECASE)),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Check:
    ok: bool
    code: str = "ok"
    message: str = ""


def check_static(source: str) -> Check:
    if not isinstance(source, str) or not source.strip():
        return Check(False, "empty", "The diagram is empty.")
    if len(source) > MAX_CHARS:
        return Check(False, "too_long", f"A diagram is limited to {MAX_CHARS} characters.")
    if source.count("\n") + 1 > MAX_LINES:
        return Check(False, "too_many_lines", f"A diagram is limited to {MAX_LINES} lines.")
    if _CONTROL.search(source):
        return Check(False, "control_characters", "The diagram contains control characters.")
    lines = source.replace("\r\n", "\n").split("\n")
    body = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("%%")]
    if any(ln.lstrip().startswith("%%{") or "%%{" in ln for ln in lines):
        return Check(
            False, "directive_not_allowed", "Mermaid directives (%%{ ... }%%) are not allowed."
        )
    if not body:
        return Check(False, "empty", "The diagram is empty.")
    if not _FIRST.match(body[0].strip()):
        return Check(
            False,
            "type_not_allowed",
            "The diagram must start with one of: " + ", ".join(ALLOWED_TYPES) + ".",
        )
    for ln in body[1:]:
        if _FORBIDDEN_LINE.match(ln):
            return Check(False, "interaction_not_allowed", "click, href and link are not allowed.")
    for code, pattern in _FORBIDDEN:
        if pattern.search(source):
            return Check(False, code, "The diagram contains something that is not allowed.")
    return Check(True)


def diagram_type(source: str) -> str | None:
    for ln in source.replace("\r\n", "\n").split("\n"):
        if ln.strip() and not ln.lstrip().startswith("%%"):
            m = _FIRST.match(ln.strip())
            return m.group(1) if m else None
    return None


# ---- the Node parse check ------------------------------------------------------------------------------


def default_script() -> Path:
    return Path(__file__).resolve().parents[4] / "packages" / "mermaid-check" / "dist" / "cli.js"


def script_path(settings: Settings) -> Path:
    return (
        Path(settings.ai_mermaid_check_script)
        if settings.ai_mermaid_check_script
        else default_script()
    )


def parse_check_available(settings: Settings) -> bool:
    return bool(shutil.which(settings.ai_node_command)) and script_path(settings).is_file()


def _child_env() -> dict[str, str]:
    env = {"NODE_ENV": "production", "NODE_OPTIONS": "--max-old-space-size=384"}
    for name in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH"):  # node needs to start; no secrets
        if os.environ.get(name) and name != "PATH":
            env[name] = os.environ[name]
    return env


async def parse_check(settings: Settings, sources: list[str]) -> list[Check | None]:
    """Static rules in Python first (cheap, always on), then ONE Node child parses the survivors.
    A result of None means 'could not be parsed here' (Node missing, timeout, crash): the caller
    publishes that diagram `unchecked`."""
    results: list[Check | None] = [None] * len(sources)
    pending: list[tuple[int, str]] = []
    for i, source in enumerate(sources):
        static = check_static(source)
        if not static.ok:
            results[i] = static
        else:
            pending.append((i, source))
    if not pending or not parse_check_available(settings):
        return results
    node = shutil.which(settings.ai_node_command)
    assert node is not None  # noqa: S101 - parse_check_available just proved it
    request = json.dumps({"diagrams": [{"id": i, "source": s} for i, s in pending]}).encode()
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
            proc.communicate(request), settings.ai_mermaid_check_timeout_seconds
        )
        data = json.loads(out.decode("utf-8")[:2_000_000])
        for row in data["results"]:
            idx = int(row["id"])
            if any(idx == p for p, _ in pending):
                ok = bool(row["ok"])
                results[idx] = Check(
                    ok, "ok" if ok else "parse_error", str(row.get("message", ""))[:300]
                )
    except (TimeoutError, OSError, ValueError, KeyError, TypeError):
        return [r if r is not None and not r.ok else None for r in results]
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
    return results


__all__ = [
    "ALLOWED_TYPES",
    "MAX_CHARS",
    "MAX_LINES",
    "Check",
    "check_static",
    "diagram_type",
    "parse_check",
    "parse_check_available",
    "sys",
]
