"""Chunking policy chunk-v1, tested as the pure function it is: sizes, structure, tables, overlap,
pages, hostile shapes."""

import pytest

from app.ai.analysis.chunking import Unit, chunk_units, split_sentences, tokens
from tests.factories import make_settings


@pytest.fixture
def cfg():
    return make_settings(
        ai_chunk_target_tokens=120, ai_chunk_max_tokens=260, ai_chunk_overlap_tokens=15
    )


def prose(words: int, tag: str = "w") -> str:
    """Sentences of ten words each, so sentence splitting has somewhere to cut."""
    sentences = []
    for i in range(0, words, 10):
        sentences.append(" ".join(f"{tag}{i + j}" for j in range(10)).capitalize() + ".")
    return " ".join(sentences)


def test_chunks_respect_the_hard_maximum_and_aim_for_the_target(cfg):
    units = [Unit("para", prose(60)) for _ in range(30)]
    chunks = chunk_units(cfg, units)
    assert len(chunks) > 5
    assert all(c.token_count <= cfg.ai_chunk_max_tokens for c in chunks)
    full = [c for c in chunks[:-1]]
    assert sum(c.token_count for c in full) / len(full) > cfg.ai_chunk_target_tokens * 0.5
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_no_content_is_lost_or_duplicated_beyond_the_overlap(cfg):
    units = [Unit("para", f"Paragraph {i} " + prose(30, f"p{i}x")) for i in range(12)]
    chunks = chunk_units(cfg, units)
    joined = " ".join(c.text for c in chunks)
    for i in range(12):
        assert f"p{i}x0 " in joined or f"Paragraph {i}" in joined


def test_a_heading_always_starts_a_new_chunk_and_sets_the_path(cfg):
    units = [
        Unit("heading", "Overview", level=1),
        Unit("para", "Intro text about the system."),
        Unit("heading", "Design", level=2),
        Unit("para", "The design uses a queue."),
        Unit("heading", "Operations", level=1),
        Unit("para", "Runbook text."),
    ]
    chunks = chunk_units(cfg, units)
    assert [c.heading_path for c in chunks] == [
        ("Overview",),
        ("Overview", "Design"),
        ("Operations",),
    ]
    assert chunks[0].text.startswith("Overview\n")
    assert chunks[1].section_key == "Overview > Design"


def test_overlap_carries_the_tail_within_a_section_but_never_across_headings(cfg):
    long_text = prose(400)
    chunks = chunk_units(
        cfg,
        [
            Unit("heading", "A", level=1),
            Unit("para", long_text),
            Unit("heading", "B", level=1),
            Unit("para", "Fresh start here."),
        ],
    )
    in_a = [c for c in chunks if c.heading_path == ("A",)]
    assert len(in_a) >= 2
    tail = in_a[0].text.split()[-4:]
    assert " ".join(tail) in in_a[1].text  # the next chunk begins with the previous tail
    b = next(c for c in chunks if c.heading_path == ("B",))
    assert "w39" not in b.text and b.text == "B\nFresh start here."


def test_a_table_is_readable_and_repeats_its_header_in_every_chunk(cfg):
    rows = tuple((f"Row{i:03d}", f"Owner{i:03d}", "Status text for this row") for i in range(80))
    header = ("Name", "Owner", "Status")
    chunks = chunk_units(cfg, [Unit("table", rows=(header, *rows))])
    assert len(chunks) > 2
    for c in chunks:
        assert c.text.splitlines()[0] == "Name | Owner | Status"
    body = "\n".join(c.text for c in chunks)
    assert "Row000 | Owner000 | Status text for this row" in body
    assert "Row079 | Owner079" in body
    assert all(c.token_count <= cfg.ai_chunk_max_tokens for c in chunks)


def test_a_table_never_shares_a_chunk_with_surrounding_prose(cfg):
    units = [
        Unit("para", "Before the table."),
        Unit("table", rows=(("A", "B"), ("1", "2"))),
        Unit("para", "After the table."),
    ]
    chunks = chunk_units(cfg, units)
    assert [c.text for c in chunks] == ["Before the table.", "A | B\n1 | 2", "After the table."]


def test_a_single_sentence_longer_than_the_maximum_is_cut_at_word_boundaries(cfg):
    monster = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_units(cfg, [Unit("para", monster)])
    assert len(chunks) > 3
    assert all(c.token_count <= cfg.ai_chunk_max_tokens for c in chunks)
    assert all(not c.text.endswith("wor") for c in chunks)  # no word is cut in half


def test_a_page_boundary_closes_a_chunk_once_it_is_half_full(cfg):
    page = lambda n, text: Unit("para", text, page=n, extraction="text_layer")  # noqa: E731
    big = prose(70)  # about half the target and more
    chunks = chunk_units(cfg, [page(1, big), page(2, big), page(3, "Tiny page.")])
    assert chunks[0].page_start == 1 and chunks[0].page_end == 1
    assert chunks[1].page_start == 2
    # a tiny page does not get a chunk of its own when the previous one is small
    tiny = chunk_units(cfg, [page(1, "Short."), page(2, "Also short.")])
    assert len(tiny) == 1 and (tiny[0].page_start, tiny[0].page_end) == (1, 2)


def test_extraction_is_the_least_trustworthy_source_and_confidence_the_lowest(cfg):
    units = [
        Unit("para", "From the text layer.", page=1, extraction="text_layer"),
        Unit("para", "From local OCR.", page=1, extraction="tesseract", ocr_confidence=71.5),
        Unit("para", "Also OCR.", page=1, extraction="tesseract", ocr_confidence=88.0),
    ]
    (chunk,) = chunk_units(cfg, units)
    assert chunk.extraction == "tesseract" and chunk.ocr_confidence == 71.5
    (vision,) = chunk_units(
        cfg,
        [
            Unit("para", "x y z", page=1, extraction="vision_ocr", low_agreement=True),
            Unit("para", "text", page=1, extraction="text_layer"),
        ],
    )
    assert vision.extraction == "vision_ocr" and vision.low_agreement is True


def test_pdf_pages_take_their_heading_path_from_the_outline(cfg):
    units = [
        Unit("para", "Chapter one text.", page=1, heading_path=("Chapter 1",)),
        Unit("para", "Chapter two text.", page=2, heading_path=("Chapter 2", "Section 2.1")),
    ]
    chunks = chunk_units(cfg, units)
    assert [c.heading_path for c in chunks] == [("Chapter 1",), ("Chapter 2", "Section 2.1")]


def test_section_keys_fall_back_to_page_blocks_then_parts(cfg):
    pdf = chunk_units(cfg, [Unit("para", "Page twelve.", page=12)])
    assert pdf[0].section_key == "pages 11-20"
    text = chunk_units(cfg, [Unit("para", "No structure at all.")])
    assert text[0].section_key == "part 1"


def test_empty_and_blank_units_produce_nothing(cfg):
    assert chunk_units(cfg, []) == []
    assert (
        chunk_units(cfg, [Unit("para", "   \n "), Unit("table", rows=()), Unit("list", "")]) == []
    )


def test_lists_keep_their_markers(cfg):
    (chunk,) = chunk_units(cfg, [Unit("list", "first"), Unit("list", "second")])
    assert chunk.text == "- first\n- second"


def test_chunking_is_deterministic(cfg):
    units = [Unit("heading", "H", level=1)] + [Unit("para", prose(50, f"q{i}")) for i in range(9)]
    a = [(c.text, c.locator) for c in chunk_units(cfg, units)]
    b = [(c.text, c.locator) for c in chunk_units(cfg, units)]
    assert a == b


def test_split_sentences_and_token_estimate_are_sane(cfg):
    assert split_sentences("One. Two! Three? Four") == ["One.", "Two!", "Three?", "Four"]
    assert split_sentences("Use e.g. the queue. Then stop.") == [
        "Use e.g. the queue.",
        "Then stop.",
    ]
    assert tokens(cfg, "x" * 300) == 111  # ceil(300 / 3.0) * 1.1, rounded up
    assert tokens(cfg, "") == 1


def test_the_text_hash_is_the_sha256_of_the_text(cfg):
    import hashlib

    (chunk,) = chunk_units(cfg, [Unit("para", "Hello hash.")])
    assert chunk.text_hash == hashlib.sha256(b"Hello hash.").digest()
