"""Google Gemini embedding adapter.

Verified against the Gemini API docs on 2026-11-08 (model id "gemini-embedding-2"):
  * POST /v1beta/models/{model}:embedContent and :batchEmbedContents, header x-goog-api-key
  * body {"model": "models/<id>", "content": {"parts": [{"text": ...}]}}, optional output_dimensionality
  * text up to 8192 tokens, PNG/JPEG images; separate Content objects give separate embeddings
  * default 3072 dimensions, allowed 128-3072; shortened vectors are normalized by the model
  * this model takes NO task_type: the task goes into the text, e.g. "task: search result | query: "
    for queries and "title: none | text: " for documents (both prefixes are settings)
  * the docs show no usage metadata, so usage is ESTIMATED (flagged in the ledger)
NOT verified (parsed defensively; the self-check is the arbiter): the exact REST response keys
("embedding.values" for one input, "embeddings[].values" for a batch), the inline image part spelling,
how an invalid API key is reported (Gemini often answers 400 API_KEY_INVALID), and GET
/v1beta/models/{model} as the cheap key check.
"""

from __future__ import annotations

import base64
import math
from typing import Any

import httpx2

from app.ai import errors
from app.ai.http import ProviderHttp
from app.ai.pricing import estimate_text_tokens
from app.ai.types import EmbedResult, Health, ImagePart, Usage
from app.config import Settings


class GoogleEmbedding:
    def __init__(
        self,
        settings: Settings,
        api_key: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self.model = settings.ai_embed_model
        self.dimensions = settings.ai_embed_dimensions
        self._key = api_key
        self._http = ProviderHttp(
            settings.ai_google_base_url, api_key, settings.ai_call_timeout_seconds, transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    async def validate_key(self) -> None:
        try:
            await self._http.request("GET", f"/v1beta/models/{self.model}", self._headers())
        except errors.ProviderError as exc:
            if exc.status == 400 and (
                "API_KEY_INVALID" in str(exc) or "API key not valid" in str(exc)
            ):
                raise errors.ProviderError(
                    errors.KEY_REJECTED, "key rejected", status=400
                ) from None
            if exc.status == 404:
                raise errors.ProviderError(
                    "model_unavailable", "model not available", status=404
                ) from None
            raise

    async def health(self) -> Health:
        try:
            await self.validate_key()
        except errors.ProviderError as exc:
            return Health(False, exc.code, str(exc))
        return Health(True, "ok")

    # ---- embedding ---------------------------------------------------------------------------

    async def embed_texts(self, texts: list[str]) -> EmbedResult:
        """Documents. Batched to AI_EMBED_BATCH_SIZE per request; one vector per input, in order."""
        prefix = self._settings.ai_embed_document_prefix
        contents = [{"parts": [{"text": prefix + t}]} for t in texts]
        tokens = sum(estimate_text_tokens(self._settings, prefix + t) for t in texts)
        return await self._embed(contents, Usage(tokens, 0, 0, estimated=True))

    async def embed_query(self, text: str) -> EmbedResult:
        prefixed = self._settings.ai_embed_query_prefix + text
        contents = [{"parts": [{"text": prefixed}]}]
        usage = Usage(estimate_text_tokens(self._settings, prefixed), 0, 0, estimated=True)
        return await self._embed(contents, usage)

    async def embed_images(self, images: list[ImagePart]) -> EmbedResult:
        contents = [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": image.media_type,
                            "data": base64.b64encode(image.data).decode(),
                        }
                    }
                ]
            }
            for image in images
        ]
        count = len(images)
        usage = Usage(
            count * self._settings.ai_embed_image_tokens_estimate, 0, count, estimated=True
        )
        return await self._embed(contents, usage)

    async def _embed(self, contents: list[dict[str, Any]], usage: Usage) -> EmbedResult:
        vectors: list[list[float]] = []
        size = self._settings.ai_embed_batch_size
        for start in range(0, len(contents), size):
            vectors += await self._batch(contents[start : start + size])
        return EmbedResult(vectors, usage, self.model)

    async def _batch(self, contents: list[dict[str, Any]]) -> list[list[float]]:
        model_ref = f"models/{self.model}"
        requests = [
            {"model": model_ref, "content": c, "output_dimensionality": self.dimensions}
            for c in contents
        ]
        body = await self._http.request(
            "POST",
            f"/v1beta/models/{self.model}:batchEmbedContents",
            self._headers(),
            {"requests": requests},
        )
        rows = body.get("embeddings")
        if not isinstance(rows, list) or len(rows) != len(contents):
            raise errors.ProviderError(errors.BAD_OUTPUT, "wrong number of embeddings returned")
        out: list[list[float]] = []
        for row in rows:
            values = row.get("values") if isinstance(row, dict) else None
            out.append(self._checked(values))
        return out

    def _checked(self, values: object) -> list[float]:
        if (
            not isinstance(values, list)
            or len(values) != self.dimensions
            or not all(isinstance(v, int | float) and math.isfinite(v) for v in values)
        ):
            raise errors.ProviderError(errors.BAD_OUTPUT, "embedding dimension mismatch")
        vector = [float(v) for v in values]
        if self._settings.ai_embed_normalize:
            norm = math.sqrt(sum(v * v for v in vector))
            if norm == 0:
                raise errors.ProviderError(errors.BAD_OUTPUT, "zero-length embedding")
            vector = [v / norm for v in vector]
        return vector
