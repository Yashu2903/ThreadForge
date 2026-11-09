"""What AI can do right now: configuration + the global kill switch + the last self-check.

Three independent reasons AI can be off, reported separately so a user sees the real one:
  1. not configured   (AI_ENABLED=false, or settings incomplete) - a deployment decision
  2. switched off     (ai_global_flags.ai_enabled = false)       - the operator's kill switch
  3. capability down  (the last self-check failed generation or embedding)

A single capability failing disables only that capability (no vision = no figure analysis and no
images at answer time); generation or text-embedding failing disables AI as a whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AiCapabilityStatus, AiGlobalFlags

CAPABILITIES = (
    "text_generation",
    "structured_output",
    "image_input",
    "text_embedding",
    "image_embedding",
    "ocr_local",
)
# AI as a whole needs these; the rest only disable their own feature.
REQUIRED = ("text_generation", "structured_output", "text_embedding")
NOT_CHECKED = "not_checked"

_FLAGS = AiGlobalFlags.__table__
_CAPS = AiCapabilityStatus.__table__


@dataclass(frozen=True)
class CapabilityState:
    name: str
    state: str  # ok | failed | not_checked
    code: str
    model: str
    checked_at: datetime | None


@dataclass
class AiStatus:
    configured: bool
    globally_enabled: bool
    available: bool
    code: str  # ok | ai_not_configured | ai_disabled | ai_unavailable
    capabilities: dict[str, CapabilityState] = field(default_factory=dict)

    def capability_ok(self, name: str) -> bool:
        cap = self.capabilities.get(name)
        # "not checked" is not "broken": the check needs a server key, and a deployment that is
        # user-key-only never has one. A real failure at call time surfaces as the call's own error.
        return cap is not None and cap.state in ("ok", "not_checked")


async def set_global_flag(db: AsyncSession, enabled: bool, note: str = "") -> None:
    """Upsert, so it also works if the single row was ever removed."""
    stmt = pg_insert(_FLAGS).values(id=1, ai_enabled=enabled, note=note[:200])
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"ai_enabled": enabled, "note": note[:200], "changed_at": func.now()},
        )
    )


async def load_status(db: AsyncSession, settings: Settings) -> AiStatus:
    flag = (
        await db.execute(select(_FLAGS.c.ai_enabled).where(_FLAGS.c.id == 1))
    ).scalar_one_or_none()
    globally = True if flag is None else bool(flag)
    rows = (await db.execute(select(_CAPS))).all()
    by_name = {r.capability: r for r in rows}
    caps: dict[str, CapabilityState] = {}
    for name in CAPABILITIES:
        row = by_name.get(name)
        if row is None or row.code == NOT_CHECKED:
            caps[name] = CapabilityState(
                name, "not_checked", NOT_CHECKED, "", row.checked_at if row else None
            )
        else:
            caps[name] = CapabilityState(
                name, "ok" if row.ok else "failed", row.code, row.model, row.checked_at
            )
    status = AiStatus(settings.ai_enabled, globally, False, "ok", caps)
    if not settings.ai_enabled:
        status.code = "ai_not_configured"
    elif not globally:
        status.code = "ai_disabled"
    elif any(caps[name].state == "failed" for name in REQUIRED):
        status.code = "ai_unavailable"
    else:
        status.available = True
    return status
