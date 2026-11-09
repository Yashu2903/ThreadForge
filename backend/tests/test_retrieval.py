"""Hybrid retrieval: the scope is part of the SQL. Five sources in one room, selections that must not
leak into each other, no fallback, RRF, per-source balance, and hostile query strings."""

import asyncio
import re
from pathlib import Path

import pytest

from app.ai import retrieval
from app.ai.retrieval import retrieve
from tests import analysis_files as af
from tests.analysis_helpers import build_world, embed_text_vector
from tests.helpers import execute, rows

DOCS = {
    "ops.md": "# Warehouse operations\n\nThe warehouse processed orders and delivery drivers brought "
    "packaging returns to shipping every day. The pipeline of orders was steady.\n",
    "finance.md": "# Finance review\n\nQuarterly revenue growth beat the budget. The finance team "
    "tracks revenue and budget each quarter.\n",
    "deploy.md": "# Deployment guide\n\nThe deployment pipeline builds a release. A release is "
    "deployed after the build passes. Workers restart during deploy.\n",
    "safety.md": "# Safety report\n\nSafety inspection found no incident. Every incident gets an "
    "inspection and a safety review.\n",
    "infra.md": "# Cluster notes\n\nThe kubernetes cluster runs pods on each node. The kubernetes "
    "deployment pipeline for pods is documented here.\n",
}


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def analyzed(world, files: dict[str, str | bytes]) -> dict[str, dict]:
    out = {}
    for name, text in files.items():
        data = text.encode() if isinstance(text, str) else text
        out[name] = await world.upload(world.owner, name, data)
        assert (await world.analyze(world.owner, out[name])).status_code == 202
    assert set(await world.run_worker(limit=50)) == {"succeeded"}
    return out


async def gens_of(world, sources: dict[str, dict], *names: str) -> list:
    got = []
    for name in names:
        _sid, vid = world.version_of(sources[name])
        got.append(
            (
                await rows(
                    world.sm,
                    "SELECT current_generation_id AS g FROM source_versions WHERE id = :v",
                    v=vid,
                )
            )[0]["g"]
        )
    return got


async def search(world, gens, query, **kw):
    async with world.sm() as db:
        return await retrieve(
            db,
            world.cfg,
            room_id=world.room_id,
            generation_ids=gens,
            query=query,
            query_vector=embed_text_vector(query) if kw.pop("vector", True) else None,
            **kw,
        )


async def names_of(world, result) -> set[str]:
    found = set()
    for p in result.passages:
        found.add(
            (
                await rows(
                    world.sm,
                    "SELECT s.display_name AS n FROM source_versions v JOIN sources s ON s.id = v.source_id WHERE v.id = :v",
                    v=p.source_version_id,
                )
            )[0]["n"]
        )
    return found


async def test_a_selection_of_three_returns_passages_from_exactly_those_three(world):
    src = await analyzed(world, DOCS)
    chosen = await gens_of(world, src, "ops.md", "finance.md", "deploy.md")
    # "kubernetes deployment pipeline" also matches infra.md strongly, which is NOT selected
    result = await search(world, chosen, "deployment pipeline for kubernetes pods")
    found = await names_of(world, result)
    assert found and found <= {"ops.md", "finance.md", "deploy.md"}
    assert "infra.md" not in found
    top = result.passages[0]
    assert (
        top.source_version_id == world.version_of(src["deploy.md"])[1]
        or "deploy" in top.text.lower()
    )


async def test_a_second_selection_shares_nothing_with_the_first(world):
    src = await analyzed(world, DOCS)
    first = await search(
        world,
        await gens_of(world, src, "ops.md", "finance.md", "deploy.md"),
        "pipeline release orders",
    )
    second = await search(
        world, await gens_of(world, src, "safety.md", "infra.md"), "pipeline release orders"
    )
    ids_first = {p.id for p in first.passages}
    ids_second = {p.id for p in second.passages}
    assert ids_first and ids_second and ids_first.isdisjoint(ids_second)
    assert await names_of(world, second) <= {"safety.md", "infra.md"}
    assert {p.generation_id for p in second.passages} <= set(
        await gens_of(world, src, "safety.md", "infra.md")
    )


async def test_nothing_relevant_in_scope_is_an_empty_result_never_a_fallback(world):
    src = await analyzed(world, DOCS)
    only_ops = await gens_of(world, src, "ops.md")
    result = await search(world, only_ops, "kubernetes cluster pods node")  # lives in infra.md only
    assert result.empty and result.passages == [] and result.figures == []
    lexical_only = await search(world, only_ops, "kubernetes cluster", vector=False)
    assert lexical_only.empty


async def test_a_removed_source_is_never_retrievable(world):
    src = await analyzed(world, DOCS)
    gens = await gens_of(world, src, "safety.md", "ops.md")
    assert not (await search(world, gens, "safety inspection incident")).empty
    sid, _vid = world.version_of(src["safety.md"])
    assert (await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")).status_code == 204
    after = await search(world, gens, "safety inspection incident")
    assert gens[0] in after.unavailable
    assert "safety.md" not in await names_of(world, after)
    assert after.empty or all(p.generation_id == gens[1] for p in after.passages)
    assert await rows(world.sm, "SELECT 1 FROM chunks WHERE generation_id = :g", g=gens[0]) == []


async def test_a_generation_of_another_room_is_unavailable_and_returns_nothing(world, sessionmaker):
    from tests.doc_helpers import room_with_doc

    src = await analyzed(world, {"ops.md": DOCS["ops.md"]})
    mine = await gens_of(world, src, "ops.md")
    other_room, other_doc = await room_with_doc(world.owner, sessionmaker)
    saved = world.room_id
    world.room_id = other_room
    secret = await world.upload(
        world.owner, "secret.md", b"# Secret\n\nThe warehouse orders password plan.\n"
    )
    await world.analyze(world.owner, secret, document_id=other_doc["id"])
    await world.run_worker()
    world.room_id = saved
    theirs = (
        await rows(
            sessionmaker,
            "SELECT current_generation_id AS g FROM source_versions WHERE room_id = :r",
            r=other_room,
        )
    )[0]["g"]
    async with sessionmaker() as db:
        result = await retrieve(
            db,
            world.cfg,
            room_id=saved,
            generation_ids=[theirs, *mine],
            query="warehouse orders",
            query_vector=embed_text_vector("warehouse orders"),
        )
    assert theirs in result.unavailable
    assert all(p.generation_id in mine for p in result.passages)
    assert "password" not in " ".join(p.text for p in result.passages)


async def test_a_passage_found_by_both_searches_outranks_one_found_by_one(world):
    src = await analyzed(
        world,
        {
            "mixed.md": "# A\n\nThe deployment pipeline builds a release.\n\n# B\n\nA delicious pipeline of "
            "unrelated gardening words and compost.\n\n# C\n\nRelease notes mention deploy and build.\n"
        },
    )
    result = await search(
        world, await gens_of(world, src, "mixed.md"), "deployment pipeline release"
    )
    best = result.passages[0]
    assert best.lexical_rank is not None and best.vector_rank is not None
    both = best.score
    one = [p for p in result.passages if p.lexical_rank is None or p.vector_rank is None]
    assert all(both > p.score for p in one)
    expected = 1 / (60 + best.lexical_rank) + 1 / (60 + best.vector_rank)
    assert abs(best.score - expected) < 1e-12


async def test_each_selected_source_keeps_a_minimum_share_of_the_evidence_budget(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor,
        sessionmaker,
        tmp_path,
        redis_prefix,
        ai_chunk_target_tokens=40,
        ai_chunk_max_tokens=80,
        ai_chunk_overlap_tokens=5,
        ai_retrieval_evidence_tokens=300,
        ai_retrieval_min_source_share=0.4,
    )
    big = "# Big\n\n" + "\n\n".join(
        f"The release pipeline step {i} deploys the build to workers." for i in range(60)
    )
    small = "# Small\n\nOne short note about the release pipeline.\n"
    src = await analyzed(world, {"big.md": big, "small.md": small})
    gens = await gens_of(world, src, "big.md", "small.md")
    result = await search(world, gens, "release pipeline deploy build")
    names = await names_of(world, result)
    assert names == {"big.md", "small.md"}
    assert sum(p.token_count for p in result.passages) <= 300
    assert result.dropped_for_budget > 0
    # without the guarantee the huge source would crowd the small one out entirely
    crowded = await search(world, gens, "release pipeline deploy build", min_source_share=0.0)
    assert len(result.per_source) >= len(crowded.per_source)


async def test_vectors_of_another_model_are_skipped_but_lexical_search_still_works(world):
    src = await analyzed(world, DOCS)
    gens = await gens_of(world, src, "ops.md")
    await execute(
        world.sm,
        "UPDATE index_generations SET embed_model = 'some-other-model' WHERE id = :g",
        g=gens[0],
    )
    result = await search(world, gens, "warehouse orders delivery")
    assert result.incompatible == gens
    assert result.passages and all(
        p.vector_rank is None and p.lexical_rank for p in result.passages
    )


async def test_vector_hits_beyond_the_distance_cutoff_are_not_relevant(world):
    src = await analyzed(world, DOCS)
    gens = await gens_of(world, src, "finance.md")
    near = await search(world, gens, "revenue growth budget", vector=True)
    assert near.passages
    far = await search(world, gens, "kubernetes pods cluster node", vector=True)
    assert far.empty


@pytest.mark.parametrize(
    "query",
    [
        '"unbalanced quote',
        "a & | ! ( ) :*",
        "'; DROP TABLE chunks; --",
        "x" * 5000,
        "   ",
        "\x00null",
        "ÄÖÜ ß 日本語",
    ],
)
async def test_hostile_or_odd_queries_never_break_retrieval(world, query):
    src = await analyzed(world, {"ops.md": DOCS["ops.md"]})
    gens = await gens_of(world, src, "ops.md")
    result = await search(world, gens, query.replace("\x00", ""))
    assert isinstance(result.passages, list)
    assert await rows(world.sm, "SELECT count(*) AS n FROM chunks") != [{"n": 0}]


async def test_without_a_query_vector_only_the_lexical_list_is_used(world):
    src = await analyzed(world, DOCS)
    result = await search(
        world, await gens_of(world, src, "deploy.md"), "deployment release", vector=False
    )
    assert result.passages and all(p.vector_rank is None for p in result.passages)


async def test_figures_come_back_ranked_and_capped_at_top_k(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, ai_retrieval_figure_top_k=2
    )
    figs = {
        f"d{i}.png": af.diagram_png(labels=("Client", f"Api{i}", "Worker", "Database"))
        for i in range(4)
    }
    docx = af.rich_docx(figure=af.diagram_png(labels=("A", "B", "C", "D")), extra_images=figs)
    src = await analyzed(world, {"fig.docx": docx})
    result = await search(
        world,
        await gens_of(world, src, "fig.docx"),
        "architecture diagram client api worker database",
    )
    assert len([p for p in result.passages if p.kind == "figure"]) == 5
    assert len(result.figures) == 2
    assert all(p.figure_storage_key and p.figure_media_type == "image/png" for p in result.figures)


async def test_every_statement_filters_by_room_and_generation_inside_the_query():
    """Static guard: the scope must be in each SQL text's WHERE clause, so nobody can 'optimize' it
    into a post-filter. (The behavioural tests above prove the effect.)"""
    for statement in (retrieval._LEXICAL, retrieval._VECTOR, retrieval._FETCH):
        sql = re.sub(r"\s+", " ", str(statement))
        assert "c.room_id = :room" in sql or "c.room_id = :room".replace("c.", "") in sql
        assert "ANY(:gens)" in sql
    assert "analysis_state <> 'removed'" in str(retrieval._LEXICAL)
    assert "analysis_state <> 'removed'" in str(retrieval._VECTOR)
    source = await asyncio.to_thread(Path(retrieval.__file__).read_text, encoding="utf-8")
    assert "fetchall()" not in source and "ORDER BY score" not in source  # no global ranking


async def test_the_operator_inspect_command_prints_what_a_query_would_retrieve(
    world, monkeypatch, capsys
):
    from app.ai.analysis import gateway as gw
    from app.commands import ai_retrieve

    src = await analyzed(world, DOCS)
    _sid, vid = world.version_of(src["deploy.md"])
    real = gw.build_embedder
    monkeypatch.setattr(
        gw, "build_embedder", lambda s, k, t=None: real(s, k, world.stub.transports())
    )
    monkeypatch.setattr(ai_retrieve, "load_settings", lambda: world.cfg)
    code = await ai_retrieve.preview(world.room_id, world.owner.id, [vid], "deployment release")
    out = capsys.readouterr().out
    assert code == 0 and "1 index(es)" in out and "deploy" in out.lower()
    ledger = await rows(
        world.sm, "SELECT operation FROM ai_usage_ledger WHERE analysis_job_id IS NULL"
    )
    assert ledger == [{"operation": "embed_text"}]


def test_lexical_queries_are_plain_words_joined_by_or():
    from app.ai.retrieval import lexical_query

    assert lexical_query("What does the architecture diagram show?") == (
        "what or does or the or architecture or diagram or show"
    )
    assert lexical_query('"quoted" -negated (grouped) a & b | c :* OR d') == (
        "quoted or negated or grouped or a or b or c or d"
    )
    assert lexical_query("dup dup DUP") == "dup"
    assert lexical_query("   !!! ") == ""
    assert lexical_query("x " * 500).count(" or ") == 0
    assert len(lexical_query(" ".join(f"w{i}" for i in range(500))).split(" or ")) == 32
