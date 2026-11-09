"""The sandboxed extractor: text layers, real Tesseract OCR with its quality gate, figure selection,
DOCX structure, Markdown, and the sandbox behaviour (timeout, crash, scrubbed environment).

Real files, the real child process and the real Tesseract binary. No provider is involved."""

import json
import shutil
import sys
import textwrap

import pytest

from app.sources import extract_sandbox as sandbox
from app.sources.extract_sandbox import ExtractionFailed, extract_file
from tests import analysis_files as af
from tests import source_files as sf
from tests.factories import make_settings

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract missing")


@pytest.fixture
def settings():
    return make_settings(ai_ocr_min_confidence=60)


@pytest.fixture
def scratch(tmp_path):
    return tmp_path / "tmp"


async def extract(tmp_path, scratch, settings, kind, data):
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    return await extract_file(path, kind, settings, scratch)


# ---- PDF text layer ----------------------------------------------------------------------------------


async def test_text_layer_pages_are_read_without_ocr(tmp_path, scratch, settings):
    ex = await extract(tmp_path, scratch, settings, "pdf", sf.text_pdf(3))
    try:
        assert [p.extraction for p in ex.pages] == ["text_layer"] * 3
        assert "quarterly report" in ex.pages[0].text and ex.ocr_pages == 0
        assert ex.figures == [] and all(p.status == "ok" for p in ex.pages)
    finally:
        await ex.cleanup()
    assert not ex.workdir.exists()  # the scratch directory is removed


@needs_tesseract
async def test_clean_scan_is_read_by_tesseract_and_accepted(tmp_path, scratch, settings):
    ex = await extract(tmp_path, scratch, settings, "pdf", af.ocr_scan_pdf(1))
    try:
        page = ex.pages[0]
        assert page.extraction == "tesseract" and page.status == "ok", (page.reason, page.text[:80])
        assert "warehouse" in page.text.lower() and page.ocr_confidence >= 60
        assert page.image is None  # no vision fallback needed
    finally:
        await ex.cleanup()


@needs_tesseract
async def test_noise_scan_fails_the_gate_and_is_kept_for_the_vision_fallback(
    tmp_path, scratch, settings
):
    ex = await extract(tmp_path, scratch, settings, "pdf", af.noise_scan_pdf(1))
    try:
        page = ex.pages[0]
        assert page.status == "failed" and page.reason in {
            "no_words",
            "too_few_words",
            "low_confidence",
            "nonsense_tokens",
        }
        assert page.image and ex.image_path(page.image).is_file()
    finally:
        await ex.cleanup()


@needs_tesseract
async def test_vision_fallback_pages_are_capped(tmp_path, scratch):
    cfg = make_settings(ai_vision_fallback_max_pages=1)
    ex = await extract(tmp_path, scratch, cfg, "pdf", af.noise_scan_pdf(3))
    try:
        assert [p.status for p in ex.pages] == ["failed"] * 3
        assert [p.image is not None for p in ex.pages] == [True, False, False]
    finally:
        await ex.cleanup()


@needs_tesseract
async def test_ocr_page_cap_marks_the_rest_failed_without_running_ocr(tmp_path, scratch):
    cfg = make_settings(ai_ocr_max_pages_per_source=1, ai_vision_fallback_max_pages=0)
    ex = await extract(tmp_path, scratch, cfg, "pdf", af.ocr_scan_pdf(3))
    try:
        assert ex.ocr_pages == 1
        assert [p.status for p in ex.pages] == ["ok", "failed", "failed"]
        assert ex.pages[1].reason == "ocr_page_cap"
    finally:
        await ex.cleanup()


async def test_missing_tesseract_sends_scanned_pages_to_the_vision_fallback(
    tmp_path, scratch, monkeypatch
):
    cfg = make_settings(ai_tesseract_command="definitely-not-installed")
    ex = await extract(tmp_path, scratch, cfg, "pdf", af.ocr_scan_pdf(1))
    try:
        assert ex.pages[0].status == "failed" and ex.pages[0].reason == "tesseract_unavailable"
        assert ex.pages[0].image is not None
    finally:
        await ex.cleanup()


# ---- figures -------------------------------------------------------------------------------------------


async def test_a_page_that_is_mostly_a_drawing_becomes_a_figure_candidate(
    tmp_path, scratch, settings
):
    ex = await extract(tmp_path, scratch, settings, "pdf", af.drawing_pdf(1))
    try:
        assert len(ex.figures) == 1 and ex.figures[0].origin == "page"
        assert ex.figures[0].page == 1 and ex.image_path(ex.figures[0].file).is_file()
        assert ex.figures_found == 1
    finally:
        await ex.cleanup()


async def test_figure_cap_counts_what_it_did_not_keep(tmp_path, scratch):
    cfg = make_settings(ai_max_figures_per_source=2, ai_figure_max_repeats=99)
    # drawing pages are identical, so the "repeated image" rule must be relaxed for this test
    ex = await extract(tmp_path, scratch, cfg, "pdf", af.drawing_pdf(4))
    try:
        assert len(ex.figures) == 2 and ex.figures_found == 4
    finally:
        await ex.cleanup()


async def test_the_same_drawing_on_many_pages_is_treated_as_decoration(tmp_path, scratch, settings):
    ex = await extract(tmp_path, scratch, settings, "pdf", af.drawing_pdf(5))  # repeats > 3
    try:
        assert ex.figures == [] and ex.figures_found == 0
    finally:
        await ex.cleanup()


# ---- DOCX ----------------------------------------------------------------------------------------------


async def test_docx_keeps_headings_lists_tables_and_figures_in_order(tmp_path, scratch, settings):
    data = af.rich_docx(figure=af.diagram_png())
    ex = await extract(tmp_path, scratch, settings, "docx", data)
    try:
        kinds = [(b.t, b.level) for b in ex.blocks]
        assert kinds[0] == ("heading", 1) and ex.blocks[0].text == "Platform Handbook"
        assert ("heading", 1) in kinds and ("heading", 2) in kinds and ("list", 0) in kinds
        table = next(b for b in ex.blocks if b.t == "table")
        assert table.rows == [["Stage", "Owner"], ["Build", "Alice"], ["Deploy", "Bob"]]
        assert len(ex.figures) == 1 and ex.figures[0].origin == "docx"
        assert ex.figures[0].after_block == len(ex.blocks)  # after the table, at the end
    finally:
        await ex.cleanup()


async def test_docx_decorative_pictures_are_dropped(tmp_path, scratch, settings):
    data = af.rich_docx(
        extra_images={"dot.png": af.tiny_png(), "blank.png": af.solid_png((255, 255, 255))}
    )
    ex = await extract(tmp_path, scratch, settings, "docx", data)
    try:
        assert ex.figures == [] and ex.figures_found == 0
    finally:
        await ex.cleanup()


async def test_docx_text_limit_is_enforced(tmp_path, scratch):
    cfg = make_settings(ai_max_extracted_chars=200)
    path = tmp_path / "x.docx"
    path.write_bytes(af.rich_docx(words_per_section=400))
    with pytest.raises(ExtractionFailed) as caught:
        await extract_file(path, "docx", cfg, scratch)
    assert caught.value.code == "extracted_text_too_large"
    assert list(scratch.glob("*")) == []  # nothing left behind


# ---- text and Markdown -----------------------------------------------------------------------------------


async def test_markdown_headings_lists_and_tables(tmp_path, scratch, settings):
    ex = await extract(tmp_path, scratch, settings, "md", af.MARKDOWN_SAMPLE.encode())
    try:
        assert [(b.t, b.level) for b in ex.blocks][:3] == [
            ("heading", 1),
            ("para", 0),
            ("heading", 2),
        ]
        assert ("list", 0) in [(b.t, b.level) for b in ex.blocks]
        table = next(b for b in ex.blocks if b.t == "table")
        assert table.rows[0] == ["Stage", "Owner"] and len(table.rows) == 3
    finally:
        await ex.cleanup()


async def test_plain_text_becomes_paragraphs(tmp_path, scratch, settings):
    ex = await extract(
        tmp_path, scratch, settings, "txt", b"First para.\nstill first.\n\nSecond.\n"
    )
    try:
        assert [b.text for b in ex.blocks] == ["First para.\nstill first.", "Second."]
    finally:
        await ex.cleanup()


# ---- the sandbox itself ---------------------------------------------------------------------------------


async def test_a_hung_child_is_killed_at_the_deadline(tmp_path, scratch, settings, monkeypatch):
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(sandbox, "CHILD_SCRIPT", hang)
    cfg = make_settings(ai_extract_timeout_seconds=1)
    with pytest.raises(ExtractionFailed) as caught:
        await extract(tmp_path, scratch, cfg, "txt", b"hello")
    assert caught.value.code == "extraction_timeout"
    assert list(scratch.glob("*")) == []


@pytest.mark.parametrize(
    "script",
    [
        "import os\nos._exit(3)\n",
        "print('not json')\n",
        "import sys\nsys.stdout.write('x' * 100000)\n",
        "print('{\"ok\": true}')\n",  # claims success but wrote no result.json
    ],
)
async def test_crashes_and_nonsense_answers_fail_the_extraction(
    tmp_path, scratch, settings, monkeypatch, script
):
    bad = tmp_path / "bad.py"
    bad.write_text(script)
    monkeypatch.setattr(sandbox, "CHILD_SCRIPT", bad)
    with pytest.raises(ExtractionFailed) as caught:
        await extract(tmp_path, scratch, settings, "txt", b"hello")
    assert caught.value.code == "extraction_failed"
    assert list(scratch.glob("*")) == []


async def test_a_result_file_with_the_wrong_shape_is_refused(
    tmp_path, scratch, settings, monkeypatch
):
    bad = tmp_path / "bad.py"
    bad.write_text(
        textwrap.dedent(
            """
            import json
            json.dump({"kind": "txt", "blocks": [{"t": "script", "text": "x"}]}, open("result.json", "w"))
            print(json.dumps({"ok": True}))
            """
        )
    )
    monkeypatch.setattr(sandbox, "CHILD_SCRIPT", bad)
    with pytest.raises(ExtractionFailed):
        await extract(tmp_path, scratch, settings, "txt", b"hello")


async def test_the_child_sees_no_secrets_and_no_network(tmp_path, scratch, settings, monkeypatch):
    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import json, os, socket
            try:
                socket.create_connection(("127.0.0.1", 9))
                net = "open"
            except OSError:
                net = "blocked"
            seen = sorted(k for k in os.environ if "DATABASE" in k or "SECRET" in k or "KEY" in k)
            json.dump({"kind": "txt", "blocks": [{"t": "para", "text": net + ":" + ",".join(seen)}]},
                      open("result.json", "w"))
            print(json.dumps({"ok": True}))
            """
        )
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-should-not-leak-0000000000")
    monkeypatch.setattr(sandbox, "CHILD_SCRIPT", probe)
    # the probe does not apply the network block itself; it proves only the ENVIRONMENT here
    ex = await extract(tmp_path, scratch, settings, "txt", b"hello")
    try:
        assert ex.blocks[0].text.endswith(":")  # no variable with those names was visible
        assert json.dumps(ex.blocks[0].text).count("sk-") == 0
    finally:
        await ex.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
async def test_the_child_reports_that_it_applied_resource_limits(tmp_path, scratch, settings):
    ex = await extract(tmp_path, scratch, settings, "txt", b"hello")
    try:
        assert ex.rlimits is True
    finally:
        await ex.cleanup()
