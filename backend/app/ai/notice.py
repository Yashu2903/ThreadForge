"""The consent notice: exactly what is sent where. Bump NOTICE_VERSION whenever the meaning changes;
everyone then has to accept again (a request without consent for the CURRENT version fails with
consent_required). The text is served by GET /ai/consent so the UI never carries its own copy.
"""

from __future__ import annotations

NOTICE_VERSION = 1


def notice_points(*, embed_provider: str, llm_provider: str) -> list[str]:
    return [
        f"When a file you attach is analyzed for the first time, its text and images are sent to the "
        f"embedding provider ({embed_provider}) so they can be searched.",
        f"To answer, the passages retrieved from your files, any images selected for the answer, the "
        f"document text you include, and any chat messages you include are sent to the generation "
        f"provider ({llm_provider}).",
        "Scanned pages that local text recognition cannot read are sent to the generation "
        "provider's vision model to be transcribed.",
        "Requests use either your own API key or the server's key. You will be told which one before "
        "you send.",
        "Other members of the room can already read the files you attach; AI answers are visible to "
        "everyone in the room.",
        "Answers can be wrong. A citation shows which passage the answer used, not that the passage "
        "proves the claim.",
    ]
