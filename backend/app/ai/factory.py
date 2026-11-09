"""Build provider adapters. `transports` lets tests put an HTTP-level stub (httpx2.MockTransport)
behind the REAL adapters, so the real request/response code runs and only the network is faked."""

from __future__ import annotations

import httpx2

from app.ai.providers.google_embed import GoogleEmbedding
from app.ai.providers.openai_llm import OpenAILLM
from app.config import Settings

Transports = dict[str, httpx2.AsyncBaseTransport]

PROVIDER_FOR = {"generation": "openai", "embedding": "google"}


def build_llm(settings: Settings, api_key: str, transports: Transports | None = None) -> OpenAILLM:
    return OpenAILLM(settings, api_key, (transports or {}).get("openai"))


def build_embedder(
    settings: Settings, api_key: str, transports: Transports | None = None
) -> GoogleEmbedding:
    return GoogleEmbedding(settings, api_key, (transports or {}).get("google"))
