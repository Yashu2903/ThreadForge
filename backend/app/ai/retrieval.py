"""Hybrid retrieval over a request's pinned index generations.

    result = await retrieve(db, settings, room_id=..., generation_ids=[...], query=..., query_vector=...)

TERMS
  * lexical search   PostgreSQL full-text search: the generated `tsv` column (english configuration,
                     GIN index), `websearch_to_tsquery`, ranked by `ts_rank_cd`. Finds the words.
  * vector search    exact cosine distance (`<=>`) between the query's embedding and every stored
                     passage vector in scope. Finds the meaning. No approximate (ANN) index: at the
                     sizes involved an exact scan of the filtered rows is cheap and always correct.
  * RRF              reciprocal rank fusion: a passage's score is the sum over the two lists of
                     1 / (k + its rank in that list), k = 60. It needs no score calibration between
                     the two lists, which have different units.

THE ONE RULE THAT MATTERS: THE SCOPE IS PART OF THE QUERY. Every statement below restricts rows with
`room_id = :room AND generation_id = ANY(:generations)` in its WHERE clause, BEFORE ranking and
limiting. Nothing is ranked globally and filtered afterwards; nothing depends on a prompt. A passage
of a source that was not selected, was removed, or lives in another room cannot appear in any list,
however similar it is. If nothing in scope is relevant the result is empty and says so: there is no
fallback to other sources.

Vectors of different models or dimensions never meet: the vector query only looks at generations
whose recorded embedding model and dimension equal the query vector's; the others are reported in
`incompatible` and still take part lexically.

Balanced sources: ranks are computed per generation (window `PARTITION BY generation_id`), so with
several sources every source contributes its own best passages to the fused list, and the packing step
reserves each source a minimum share of the evidence budget.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.pricing import estimate_text_tokens
from app.config import Settings
from app.models.analysis import vector_literal

_WORDS = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_TERMS = 32


def lexical_query(query: str) -> str:
    """The text handed to `websearch_to_tsquery`: the query's plain words joined by `or`.

    `websearch_to_tsquery` ANDs bare words, which suits a search box but not a question: "what does the
    architecture diagram show" would need every word in ONE passage. With `or`, any word can match and
    `ts_rank_cd` ranks passages that match more of them (closer together) higher; PostgreSQL's english
    configuration drops stop words ("what", "does", "the") by itself. Only word characters survive, so
    the query can contain no operators, quotes, parentheses, `-` negations or `:*` prefixes: whatever a
    user (or a document, or a model) puts in the question is just words."""
    seen: dict[str, None] = {}
    for word in _WORDS.findall(query.lower()):
        if word != "or" and len(word) <= 64:
            seen.setdefault(word, None)
        if len(seen) >= MAX_QUERY_TERMS:
            break
    return " or ".join(seen)


_LEXICAL = text(
    """
    SELECT id, generation_id, rn FROM (
        SELECT c.id, c.generation_id,
               row_number() OVER (PARTITION BY c.generation_id
                                  ORDER BY ts_rank_cd(c.tsv, q.query) DESC, c.ordinal) AS rn
          FROM chunks c
          JOIN index_generations g ON g.id = c.generation_id AND g.room_id = c.room_id
          JOIN source_versions v ON v.id = c.source_version_id AND v.room_id = c.room_id,
               websearch_to_tsquery('english', CAST(:q AS text)) AS q(query)
         WHERE c.room_id = :room
           AND c.generation_id = ANY(:gens)
           AND g.state IN ('ready', 'superseded')
           AND v.analysis_state <> 'removed'
           AND c.tsv @@ q.query
    ) ranked WHERE rn <= :k
    """
)
_VECTOR = text(
    """
    SELECT id, generation_id, rn, dist FROM (
        SELECT c.id, c.generation_id, c.embedding <=> CAST(:qv AS vector) AS dist,
               row_number() OVER (PARTITION BY c.generation_id
                                  ORDER BY c.embedding <=> CAST(:qv AS vector), c.ordinal) AS rn
          FROM chunks c
          JOIN index_generations g ON g.id = c.generation_id AND g.room_id = c.room_id
          JOIN source_versions v ON v.id = c.source_version_id AND v.room_id = c.room_id
         WHERE c.room_id = :room
           AND c.generation_id = ANY(:gens)
           AND g.state IN ('ready', 'superseded')
           AND g.embed_model = :model AND g.embed_dims = :dims
           AND c.embedding_dims = :dims
           AND v.analysis_state <> 'removed'
    ) ranked WHERE rn <= :k AND dist <= :maxdist
    """
)
_FETCH = text(
    """
    SELECT c.id, c.generation_id, c.source_version_id, c.ordinal, c.kind, c.text, c.extraction,
           c.ocr_confidence, c.locator, c.flags, c.page_start, c.page_end, c.section_key,
           c.token_count, f.figure_number, f.storage_key, f.media_type
      FROM chunks c LEFT JOIN figures f ON f.chunk_id = c.id
     WHERE c.id = ANY(:ids) AND c.room_id = :room AND c.generation_id = ANY(:gens)
    """
)
_GEN_INFO = text(
    """
    SELECT g.id, g.source_version_id, g.embed_model, g.embed_dims, g.state
      FROM index_generations g
      JOIN source_versions v ON v.id = g.source_version_id AND v.room_id = g.room_id
     WHERE g.room_id = :room AND g.id = ANY(:gens) AND v.analysis_state <> 'removed'
    """
)


@dataclass
class Passage:
    id: uuid.UUID
    generation_id: uuid.UUID
    source_version_id: uuid.UUID
    ordinal: int
    kind: str
    text: str
    extraction: str
    ocr_confidence: float | None
    locator: dict
    flags: dict
    page_start: int | None
    page_end: int | None
    section_key: str | None
    token_count: int
    figure_number: int | None
    figure_storage_key: str | None
    figure_media_type: str | None
    score: float = 0.0
    lexical_rank: int | None = None
    vector_rank: int | None = None
    vector_distance: float | None = None


@dataclass
class RetrievalResult:
    passages: list[Passage] = field(default_factory=list)  # the evidence, best first, within budget
    figures: list[Passage] = field(default_factory=list)  # top-K figure passages with images
    considered: int = 0  # distinct candidates before packing
    dropped_for_budget: int = 0
    per_source: dict[uuid.UUID, int] = field(default_factory=dict)  # selected per source version
    searched_generations: list[uuid.UUID] = field(default_factory=list)
    incompatible: list[uuid.UUID] = field(default_factory=list)  # lexical only (other model/dims)
    unavailable: list[uuid.UUID] = field(default_factory=list)  # pinned but gone or removed

    @property
    def empty(self) -> bool:
        return not self.passages


async def retrieve(
    db: AsyncSession,
    settings: Settings,
    *,
    room_id: uuid.UUID,
    generation_ids: list[uuid.UUID],
    query: str,
    query_vector: list[float] | None,
    evidence_tokens: int | None = None,
    figure_top_k: int | None = None,
    min_source_share: float | None = None,
) -> RetrievalResult:
    gens = list(dict.fromkeys(generation_ids))
    result = RetrievalResult()
    if not gens or not query.strip():
        return result
    info = {r.id: r for r in (await db.execute(_GEN_INFO, {"room": room_id, "gens": gens})).all()}
    result.unavailable = [g for g in gens if g not in info]
    usable = [g for g in gens if g in info]
    result.searched_generations = usable
    if not usable:
        return result
    budget = evidence_tokens or settings.ai_retrieval_evidence_tokens
    top_k_figures = settings.ai_retrieval_figure_top_k if figure_top_k is None else figure_top_k
    share = settings.ai_retrieval_min_source_share if min_source_share is None else min_source_share

    terms = lexical_query(query)
    lex = []
    if terms:
        lex = (
            await db.execute(
                _LEXICAL,
                {"room": room_id, "gens": usable, "q": terms, "k": settings.ai_retrieval_lexical_k},
            )
        ).all()
    vec = []
    if query_vector is not None:
        dims = len(query_vector)
        comparable = [
            g
            for g in usable
            if info[g].embed_model == settings.ai_embed_model and info[g].embed_dims == dims
        ]
        result.incompatible = [g for g in usable if g not in comparable]
        if comparable:
            vec = (
                await db.execute(
                    _VECTOR,
                    {
                        "room": room_id,
                        "gens": comparable,
                        "qv": vector_literal(query_vector),
                        "model": settings.ai_embed_model,
                        "dims": dims,
                        "k": settings.ai_retrieval_vector_k,
                        "maxdist": settings.ai_retrieval_max_distance,
                    },
                )
            ).all()

    k = settings.ai_retrieval_rrf_k
    scores: dict[uuid.UUID, float] = {}
    lexical_rank: dict[uuid.UUID, int] = {}
    vector_rank: dict[uuid.UUID, int] = {}
    distance: dict[uuid.UUID, float] = {}
    for row in lex:
        scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (k + row.rn)
        lexical_rank[row.id] = row.rn
    for row in vec:
        scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (k + row.rn)
        vector_rank[row.id] = row.rn
        distance[row.id] = float(row.dist)
    result.considered = len(scores)
    if not scores:
        return result

    fetched = {
        r.id: r
        for r in (
            await db.execute(_FETCH, {"ids": list(scores), "room": room_id, "gens": usable})
        ).all()
    }
    candidates: list[Passage] = []
    for pid, score in scores.items():
        r = fetched.get(pid)
        if r is None:  # cannot happen (same filters); never trust, just skip
            continue
        candidates.append(
            Passage(
                id=r.id,
                generation_id=r.generation_id,
                source_version_id=r.source_version_id,
                ordinal=r.ordinal,
                kind=r.kind,
                text=r.text,
                extraction=r.extraction,
                ocr_confidence=r.ocr_confidence,
                locator=r.locator,
                flags=r.flags,
                page_start=r.page_start,
                page_end=r.page_end,
                section_key=r.section_key,
                token_count=r.token_count,
                figure_number=r.figure_number,
                figure_storage_key=r.storage_key,
                figure_media_type=r.media_type,
                score=score,
                lexical_rank=lexical_rank.get(pid),
                vector_rank=vector_rank.get(pid),
                vector_distance=distance.get(pid),
            )
        )
    candidates.sort(key=lambda p: (-p.score, p.generation_id.int, p.ordinal))
    selected = _pack(candidates, budget, share, usable, settings)
    result.passages = selected
    result.dropped_for_budget = len(candidates) - len(selected)
    for p in selected:
        result.per_source[p.source_version_id] = result.per_source.get(p.source_version_id, 0) + 1
    result.figures = [p for p in selected if p.kind == "figure" and p.figure_storage_key][
        :top_k_figures
    ]
    return result


def _pack(
    candidates: list[Passage],
    budget: int,
    share: float,
    generations: list[uuid.UUID],
    settings: Settings,
) -> list[Passage]:
    """Fill the evidence budget. First pass: each generation with candidates gets up to `share` of the
    budget from ITS OWN best passages (only when several sources are searched). Second pass: the rest
    of the budget in global fused order. The output is in fused order."""
    chosen: dict[uuid.UUID, Passage] = {}
    used = 0
    if len(generations) > 1 and share > 0:
        floor = int(budget * share)
        for gen in generations:
            spent = 0
            for p in candidates:
                if p.generation_id != gen or p.id in chosen:
                    continue
                if spent + p.token_count > floor:
                    continue
                chosen[p.id] = p
                spent += p.token_count
                used += p.token_count
    for p in candidates:
        if p.id in chosen:
            continue
        if used + p.token_count > budget:
            continue
        chosen[p.id] = p
        used += p.token_count
    return [p for p in candidates if p.id in chosen]


def evidence_tokens_of(settings: Settings, passages: list[Passage]) -> int:
    return sum(p.token_count for p in passages) + estimate_text_tokens(settings, "")
