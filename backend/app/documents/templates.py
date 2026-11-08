"""Server-defined starter documents.

Each template is only headings plus empty paragraphs: structure to fill in, no invented facts.
Ids are fresh UUIDs per document (ids must be unique within a document, and two documents made
from one template should not share node ids). The result is run through the same validator as an
editor save before it is stored.
"""

from __future__ import annotations

from typing import Any

from app.documents.schema import Limits, ValidatedDocument, new_id, validate_document

CREATABLE_TEMPLATES = (
    "blank",
    "project_brief",
    "technical_proposal",
    "meeting_notes",
    "research_summary",
)
ALL_TEMPLATE_KEYS = (*CREATABLE_TEMPLATES, "imported")

SECTIONS: dict[str, tuple[str, ...]] = {
    "project_brief": ("Objective", "Scope", "Requirements", "Next steps"),
    "technical_proposal": (
        "Problem",
        "Proposed approach",
        "Architecture",
        "Tradeoffs",
        "Implementation plan",
    ),
    "meeting_notes": ("Agenda", "Discussion", "Decisions", "Action items"),
    "research_summary": (
        "Research question",
        "Findings",
        "Evidence",
        "Limitations",
        "Conclusions",
    ),
}


def _empty_paragraph() -> dict[str, Any]:
    return {"type": "paragraph", "id": new_id()}


def build_template(key: str) -> dict[str, Any]:
    """The raw (not yet validated) tree for a template key."""
    if key == "blank":
        blocks = [_empty_paragraph()]
    elif key in SECTIONS:
        blocks = []
        for title in SECTIONS[key]:
            blocks.append(
                {
                    "type": "heading",
                    "id": new_id(),
                    "level": 1,
                    "children": [{"type": "text", "text": title}],
                }
            )
            blocks.append(_empty_paragraph())
    else:
        raise ValueError(f"unknown template {key!r}")
    return {"schema_version": 1, "blocks": blocks}


def template_document(key: str, limits: Limits) -> ValidatedDocument:
    return validate_document(build_template(key), limits)
