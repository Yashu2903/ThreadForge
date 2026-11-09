"""Provider-neutral types and the two adapter interfaces.

LLMProvider / EmbeddingProvider are Protocols: the OpenAI and Google implementations and the test
stubs (which are real HTTP servers' worth of behaviour behind httpx2.MockTransport) all satisfy them.
Provider calls happen ONLY in worker processes (and the single key-validation call at save time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Provider = Literal["openai", "google"]
KeySource = Literal["server", "user"]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    media_type: str  # image/png, image/jpeg or image/webp


Part = TextPart | ImagePart


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    image_count: int = 0
    estimated: bool = False  # True when the provider did not report usage and we estimated it


@dataclass(frozen=True)
class StructuredResult:
    data: Any  # parsed JSON
    usage: Usage
    model: str


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    usage: Usage
    model: str


@dataclass(frozen=True)
class Health:
    ok: bool
    code: str  # "ok" or a safe error code
    detail: str = ""


@dataclass
class ProviderErrorInfo:
    code: str
    status: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    safe_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    model: str

    async def generate_structured(
        self,
        *,
        system: str,
        parts: list[Part],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> StructuredResult: ...

    def count_tokens(self, parts: list[Part]) -> int: ...

    async def health(self) -> Health: ...

    async def validate_key(self) -> None: ...


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    async def embed_texts(self, texts: list[str]) -> EmbedResult: ...

    async def embed_query(self, text: str) -> EmbedResult: ...

    async def embed_images(self, images: list[ImagePart]) -> EmbedResult: ...

    async def health(self) -> Health: ...

    async def validate_key(self) -> None: ...
