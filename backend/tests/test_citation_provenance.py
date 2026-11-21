"""A citation of a model-written section summary (A3, A7 of the review sheet) must lead back to the original text.

Before: an `S` citation stored the model's summary as its "excerpt" and the UI showed it as a quotation, so a reader could
not tell source text from model text, and the original chunks (stored in `chunk_ids`) could not be opened. Now every
cited item carries its lineage (`origin`: stored chunks with locator, extraction and the hash of their text;
`derivation`: how a summary was made), and `GET .../citations/{n}/origin` resolves it against the stored index, scoped to
the files THIS request pinned.

These are structure and access tests. A passing provenance check says the lineage can be followed, never that the original
supports what the answer says: that is for a reader (and the quality evaluation's labelled semantic checks).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.ai.answer import strategies
from app.ai.answer.answer import CitationTable, EvidenceSet, provenance_incomplete
from tests.ai_helpers import install
from tests.analysis_helpers import add_member, answer, block, build_world
from tests.helpers import execute as sql
from tests.helpers import rows

RELEASE = (
    b"# Release process\n\nEvery release goes through three gates.\n\n## Build\n\n- compile the sources\n"
    b"- run the unit tests\n\n## Deploy\n\nThe deploy step restarts the workers one at a time.\n"
)
OTHER = b"# Secrets\n\nThe vault password rotation happens on the first of the month.\n"


@pytest.fixture
async def world(make_actor, sessionmaker, tmp_path, redis_prefix):
    return await build_world(make_actor, sessionmaker, tmp_path, redis_prefix)


async def ready(world, name="release.md", data=RELEASE):
    source = await world.upload(world.owner, name, data)
    await world.analyze(world.owner, source)
    assert set(await world.run_worker()) == {"succeeded"}
    return source


async def summarize(world, source, instruction="Summarize the release process."):
    # the stand-in cites every section it was given, so every derived citation can be opened
    world.stub.answer_fn = lambda p: answer(*[block(f"About {lb}.", lb) for lb in p.labels])
    r = await world.send(world.owner, instruction, sources=[source], task_hint="summarize")
    assert r.status_code == 201, r.text
    assert (await world.drain())[-1] == "request:published"
    rid = r.json()["id"]
    return rid, (await world.request(world.owner, rid, "/result")).json()


async def origin(world, rid, n, actor=None):
    return await world.request(actor or world.owner, rid, f"/citations/{n}/origin")


# ---- derived summaries lead to their original passages ------------------------------------------------------------


async def test_a_section_summary_citation_is_labelled_derived_and_lists_its_original_chunks(world):
    source = await ready(world)
    result = (await summarize(world, source))[1]
    sections = [c for c in result["citations"] if c["kind"] == "section"]
    assert sections, "the summarize route cites section summaries"
    for c in sections:
        assert c["derived"] is True and c["provenance"] == "complete"
        d = c["derivation"]
        assert d["kind"] == "section_summary" and d["method"] == "map-v1"
        assert d["prompt"] == "answer-v2" and d["model"] and d["of"] == "file"
        assert c["origin"] and all(o["text_sha256"] and o["chunk_id"] for o in c["origin"])
        assert [o["chunk_id"] for o in c["origin"]] == c["chunk_ids"]
        assert all(
            o["source_version_id"] == c["source_version_id"] and o["version_no"] == 1
            for o in c["origin"]
        )
    assert result["answer"]["stats"]["provenance_incomplete"] == 0


async def test_opening_a_summary_citation_returns_the_verbatim_original_text_with_its_locator(
    world,
):
    source = await ready(world)
    rid, result = await summarize(world, source)
    build = next(
        c for c in result["citations"] if (c["locator"].get("heading_path") or [])[-1:] == ["Build"]
    )
    got = (await origin(world, rid, build["n"])).json()
    assert got["derived"] is True and got["provenance"] == "complete" and got["unavailable"] == 0
    texts = " ".join(o["text"] for o in got["origin"])
    assert (
        "compile the sources" in texts and "run the unit tests" in texts
    )  # original words, not the summary's
    first = got["origin"][0]
    assert (
        first["integrity"] == "verified"
        and first["source_name"] == "release.md"
        and first["version_no"] == 1
    )
    assert first["heading_path"][-1] == "Build" and first["extraction"] == "plain"
    assert first["text_sha256"] == hashlib.sha256(first["text"].encode()).hexdigest()
    # the summary text the model wrote is NOT among the origin texts: derived and original are different things
    assert build["excerpt"] not in [o["text"] for o in got["origin"]]


async def test_a_derived_citation_may_stand_on_several_chunks_and_a_chunk_may_be_used_twice(world):
    long = b"# Long\n\n" + b"\n\n".join(
        f"Paragraph {i} says the {i}th depot is on schedule. ".encode() * 12 for i in range(14)
    )
    source = await ready(world, "long.md", long)
    rid, result = await summarize(world, source, "Summarize this file")
    multi = [c for c in result["citations"] if len(c["origin"]) > 1]
    assert multi, "a section made of several chunks cites all of them"
    got = (await origin(world, rid, multi[0]["n"])).json()
    assert len(got["origin"]) == len(multi[0]["origin"]) > 1
    assert [o["ordinal"] for o in got["origin"]] == sorted(o["ordinal"] for o in got["origin"])


async def test_a_retrieved_passage_is_its_own_origin_and_is_not_marked_derived(world):
    source = await ready(world)
    world.stub.answer_fn = lambda p: answer(block("Bob restarts the workers.", *p.labels[:1]))
    r = await world.send(
        world.owner, "Who restarts the workers?", sources=[source], task_hint="ask"
    )
    assert (await world.drain())[-1] == "request:published"
    rid = r.json()["id"]
    result = (await world.request(world.owner, rid, "/result")).json()
    c = result["citations"][0]
    assert c["kind"] == "passage" and c["derived"] is False and c["derivation"] is None
    assert len(c["origin"]) == 1 and c["origin"][0]["chunk_id"] == c["chunk_ids"][0]
    got = (await origin(world, rid, c["n"])).json()
    assert (
        got["derived"] is False
        and got["origin"][0]["text"]
        and got["origin"][0]["integrity"] == "verified"
    )


# ---- access, scope and integrity ----------------------------------------------------------------------------------


async def test_origin_follows_the_source_download_roles(
    make_actor, sessionmaker, tmp_path, redis_prefix
):
    world = await build_world(
        make_actor, sessionmaker, tmp_path, redis_prefix, source_download_roles=["owner", "editor"]
    )
    viewer = await make_actor("Viewer", app_settings=world.cfg)
    install(viewer, world.stub)
    await add_member(world.sm, world.room_id, viewer.id, "viewer")
    source = await ready(world)
    rid, result = await summarize(world, source)
    n = result["citations"][0]["n"]
    assert (await origin(world, rid, n, viewer)).status_code == 403
    assert (await origin(world, rid, n)).status_code == 200
    assert (await origin(world, rid, 99)).status_code == 404


async def test_a_chunk_of_a_file_the_request_did_not_select_is_never_returned(world):
    source = await ready(world)
    other = await ready(world, "secrets.md", OTHER)
    rid, result = await summarize(world, source)
    foreign = (
        await rows(
            world.sm,
            "SELECT c.id, c.text FROM chunks c JOIN source_versions v ON v.current_generation_id = c.generation_id "
            "JOIN sources s ON s.id = v.source_id WHERE s.display_name = 'secrets.md'",
        )
    )[0]
    # tamper with the stored citation: point it at the other file's chunk (a corrupted or forged record)
    cites = result["citations"]
    cites[0]["origin"] = [{**cites[0]["origin"][0], "chunk_id": str(foreign["id"])}]
    await sql(
        world.sm,
        "UPDATE ai_results SET citations = CAST(:c AS jsonb) WHERE request_id = :r",
        c=__import__("json").dumps(cites),
        r=uuid.UUID(rid),
    )
    got = (await origin(world, rid, cites[0]["n"])).json()
    assert got["origin"][0]["integrity"] == "unavailable" and got["origin"][0]["text"] is None
    assert "vault" not in __import__("json").dumps(got)
    assert other  # the other file exists in the room; it is just not part of this request


async def test_changed_stored_text_is_reported_as_changed_not_hidden(world):
    source = await ready(world)
    rid, result = await summarize(world, source)
    c = result["citations"][1]
    await sql(
        world.sm,
        "UPDATE chunks SET text = text || ' (edited later)' WHERE id = CAST(:i AS uuid)",
        i=c["origin"][0]["chunk_id"],
    )
    got = (await origin(world, rid, c["n"])).json()
    assert (
        got["origin"][0]["integrity"] == "changed" and "(edited later)" in got["origin"][0]["text"]
    )


async def test_after_the_source_is_removed_only_a_stub_remains(world):
    source = await ready(world)
    rid, result = await summarize(world, source)
    n = result["citations"][0]["n"]
    sid, _ = world.version_of(source)
    await world.owner.delete(f"/rooms/{world.room_id}/sources/{sid}")
    got = (await origin(world, rid, n)).json()
    assert got["source_removed"] is True and got["origin"] == []
    assert got["derived"] is True  # the fact that it was derived survives; the text does not


# ---- the records themselves ----------------------------------------------------------------------------------------


def test_a_section_item_without_a_derivation_is_incomplete_not_silently_trusted():
    ev = EvidenceSet()
    ev.add("S", kind="section", text="A summary.", origin=[{"chunk_id": "c", "text_sha256": "x"}])
    ev.add(
        "S",
        kind="section",
        text="A summary.",
        derivation={"kind": "section_summary"},
        origin=[{"chunk_id": "c", "text_sha256": "x"}],
    )
    ev.add(
        "S", kind="section", text="A summary.", derivation={"kind": "section_summary"}, origin=[]
    )
    ev.add(
        "S",
        kind="section",
        text="A summary.",
        derivation={"kind": "section_summary"},
        origin=[{"chunk_id": "c"}],
    )
    assert [i.provenance for i in ev.items.values()] == [
        "incomplete",
        "complete",
        "incomplete",
        "incomplete",
    ]
    table = CitationTable(ev)
    for label in ev.items:
        table.resolve([{"ref": label, "quote": None}])
    assert [c["provenance"] for c in table.table] == [
        "incomplete",
        "complete",
        "incomplete",
        "incomplete",
    ]
    assert provenance_incomplete(table.table) == 3


def test_a_summary_can_never_carry_a_quotation_and_a_passage_quote_is_checked_against_the_original():
    ev = EvidenceSet()
    ev.add(
        "S",
        kind="section",
        text="Alice owns Build.",
        derivation={"kind": "section_summary"},
        origin=[],
    )
    ev.add(
        "P",
        kind="passage",
        text="The deploy step   restarts the workers\none at a time.",
        extraction="plain",
        origin=[{"chunk_id": "c", "text_sha256": "x"}],
    )
    table = CitationTable(ev)
    table.resolve(
        [{"ref": "S1", "quote": "Alice owns Build."}]
    )  # dropped: a summary is not quotable
    assert table.stats.dropped_quotes == 1 and table.table[0]["quotes"] == []
    # whitespace is normalized (the documented policy); words, case and order are not
    table.resolve([{"ref": "P1", "quote": "restarts the workers one at a time"}])
    assert table.table[1]["quotes"] == ["restarts the workers one at a time"]
    table.resolve([{"ref": "P1", "quote": "Restarts The Workers"}])
    assert table.stats.invalid_citations == 1  # a different case is a different quotation


class FakeGateway:
    def __init__(self, reply):
        self.reply = reply

    async def structured(self, *args, **kwargs):
        return self.reply


async def test_a_section_the_model_skipped_is_marked_as_the_sections_opening_not_a_summary():
    from app.config import Settings
    from tests.factories import make_settings

    sec = strategies.Section(
        key="part",
        text="The opening words of a section that the model did not summarize.",
        tokens=12,
        chunk_ids=[uuid.uuid4()],
        page_start=None,
        page_end=None,
        extraction="plain",
        version_id=uuid.uuid4(),
        source_name="a.md",
        origin=[{"chunk_id": "c1", "text_sha256": "h"}],
    )
    ev = EvidenceSet()
    settings: Settings = make_settings()
    records, _ = await strategies.summarize_sections(
        FakeGateway({"sections": []}), settings, [sec], ev, max_calls=2
    )
    [item] = ev.items.values()
    assert (
        item.derivation["kind"] == "section_opening" and item.derivation["chars_sent_to_model"] == 0
    )
    assert records[0]["fallback"] is True
