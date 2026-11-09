"""The sandboxed inspector: what it measures, what it refuses, and how the sandbox itself behaves
(wall-clock timeout, crash, output flood, scrubbed environment, scratch directory cleanup)."""

import json
import sys
import textwrap

import pytest

from app.sources import sandbox
from app.sources.sandbox import InspectionRejected, inspect_file
from tests import source_files as sf
from tests.factories import make_settings


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def scratch(tmp_path):
    return tmp_path / "tmp"


async def inspect(tmp_path, scratch, settings, kind, data):
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    return await inspect_file(path, kind, settings, scratch)


async def refused(tmp_path, scratch, settings, kind, data) -> str:
    with pytest.raises(InspectionRejected) as caught:
        await inspect(tmp_path, scratch, settings, kind, data)
    return caught.value.code


# ---- what it measures ---------------------------------------------------------------------------------


async def test_text_pdf_counts_pages_and_text_layers(tmp_path, scratch, settings):
    r = await inspect(tmp_path, scratch, settings, "pdf", sf.text_pdf(3))
    assert (r.page_count, r.text_page_count, r.likely_scanned_pages) == (3, 3, 0)
    assert r.image_count == 0 and r.text_chars > 100


async def test_scanned_pdf_has_no_text_layer_and_counts_its_pictures(tmp_path, scratch, settings):
    r = await inspect(tmp_path, scratch, settings, "pdf", sf.scanned_pdf(2))
    assert (r.page_count, r.text_page_count, r.likely_scanned_pages) == (2, 0, 2)
    assert r.image_count == 2 and r.text_chars == 0


async def test_mixed_pdf_separates_text_pages_from_scanned_pages(tmp_path, scratch, settings):
    r = await inspect(tmp_path, scratch, settings, "pdf", sf.mixed_pdf())
    assert (r.page_count, r.text_page_count, r.likely_scanned_pages, r.image_count) == (2, 1, 1, 1)


async def test_docx_counts_images_and_text_but_not_pages(tmp_path, scratch, settings):
    r = await inspect(tmp_path, scratch, settings, "docx", sf.docx(["abc", "de"], images=3))
    assert r.image_count == 3 and r.text_chars == 5
    assert r.page_count is None and r.likely_scanned_pages is None


@pytest.mark.parametrize("kind", ["txt", "md"])
async def test_text_files(tmp_path, scratch, settings, kind):
    r = await inspect(tmp_path, scratch, settings, kind, "café ☃\n".encode())
    assert (r.kind, r.text_chars, r.image_count, r.page_count) == (kind, 7, 0, None)


async def test_a_utf8_bom_is_accepted_and_not_counted(tmp_path, scratch, settings):
    r = await inspect(tmp_path, scratch, settings, "txt", b"\xef\xbb\xbfabc")
    assert r.text_chars == 3


# ---- what it refuses ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "data", "code"),
    [
        ("pdf", sf.encrypted_pdf("secret"), "pdf_encrypted"),
        ("pdf", sf.encrypted_pdf(""), "pdf_encrypted"),  # owner password only: still refused
        ("pdf", b"%PDF-1.4\nthis is not a pdf", "pdf_invalid"),
        ("pdf", sf.text_pdf(2)[:-200], "pdf_invalid"),  # truncated
        ("docx", b"PK\x03\x04 not really a zip", "docx_invalid"),
        ("docx", sf.docx(macros=True), "docx_macros"),
        ("docx", sf.docx(main_type=sf.DOCM_MAIN), "docx_macros"),
        ("docx", sf.docx(extra={"word/activeX/activeX1.xml": b"<a/>"}), "docx_macros"),
        ("docx", sf.docx(skip=("word/document.xml",)), "docx_invalid"),
        ("docx", sf.docx(skip=("[Content_Types].xml",)), "docx_invalid"),
        ("docx", sf.docx(main_type="application/xml"), "docx_invalid"),
        ("docx", sf.docx(doctype='<!DOCTYPE d [<!ENTITY x "y">]>'), "docx_invalid"),
        ("docx", sf.docx(extra={"../evil.xml": b"x"}), "docx_invalid"),
        ("docx", sf.docx(extra={"/abs.xml": b"x"}), "docx_invalid"),
        pytest.param(
            "docx",
            sf.docx(extra={"a\\b.xml": b"x"}),
            "docx_invalid",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="zipfile maps backslash"),
        ),
        ("docx", sf.text_pdf(1), "docx_invalid"),
        ("txt", b"abc\x00def", "text_invalid"),
        ("txt", "café".encode("latin-1"), "text_invalid"),  # not UTF-8
        ("txt", "x".encode("utf-16"), "text_invalid"),  # UTF-16 has NULs
        ("txt", b"   \n\t ", "text_invalid"),  # nothing in it
        ("md", bytes(range(1, 32)) * 40, "text_invalid"),  # control characters, no NUL
        ("txt", sf.text_pdf(1), "type_mismatch"),  # a PDF renamed .txt
    ],
)
async def test_hostile_or_invalid_files_are_refused(tmp_path, scratch, settings, kind, data, code):
    assert await refused(tmp_path, scratch, settings, kind, data) == code


async def test_a_zip_bomb_is_refused_by_the_expansion_limit(tmp_path, scratch):
    small = make_settings(source_docx_max_uncompressed_bytes=5 * 1024 * 1024)
    assert await refused(tmp_path, scratch, small, "docx", sf.zip_bomb_docx(30)) == "docx_invalid"


async def test_too_many_pages_is_refused(tmp_path, scratch):
    few = make_settings(source_max_pages=2)
    assert await refused(tmp_path, scratch, few, "pdf", sf.text_pdf(3)) == "too_many_pages"
    assert (await inspect(tmp_path, scratch, few, "pdf", sf.text_pdf(2))).page_count == 2


async def test_too_many_docx_parts_is_refused(tmp_path, scratch):
    few = make_settings(source_docx_max_entries=3)
    assert await refused(tmp_path, scratch, few, "docx", sf.docx(images=2)) == "docx_invalid"


async def test_a_text_file_over_the_upload_limit_is_refused(tmp_path, scratch):
    small = make_settings(source_max_upload_bytes=1000, source_room_quota_bytes=5000)
    assert await refused(tmp_path, scratch, small, "txt", b"a" * 2000) == "text_invalid"


# ---- the sandbox itself -------------------------------------------------------------------------------


def child(tmp_path, monkeypatch, body: str):
    """Replace the real inspector with a tiny script (it still gets the real sandbox around it)."""
    script = tmp_path / "fake_child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(sandbox, "CHILD_SCRIPT", script)


async def test_a_hanging_child_is_killed_at_the_deadline(tmp_path, scratch, monkeypatch):
    child(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    fast = make_settings(source_inspect_timeout_seconds=1)
    code = await refused(tmp_path, scratch, fast, "txt", b"abc")
    assert code == "inspection_timeout"


async def test_a_crashing_child_means_refused_not_accepted(tmp_path, scratch, monkeypatch):
    child(tmp_path, monkeypatch, "import os\nos._exit(139)\n")
    assert await refused(tmp_path, scratch, make_settings(), "pdf", sf.text_pdf(1)) == (
        "inspection_failed"
    )


@pytest.mark.parametrize(
    "body",
    [
        "print('not json')",
        "print('[1, 2, 3]')",
        "print('{\"ok\": true}')",  # missing fields
        'print(\'{"ok": true, "kind": "pdf", "page_count": "3", "text_page_count": 1,'
        ' "likely_scanned_pages": 0, "image_count": 0, "text_chars": 0}\')',  # a string number
        'print(\'{"ok": true, "kind": "exe", "page_count": 1, "text_page_count": 1,'
        ' "likely_scanned_pages": 0, "image_count": 0, "text_chars": 0}\')',
        'print(\'{"ok": true, "kind": "pdf", "page_count": -1, "text_page_count": 1,'
        ' "likely_scanned_pages": 0, "image_count": 0, "text_chars": 0}\')',
        'print(\'{"ok": false, "code": "bad code!", "message": "x"}\')',
        "",
    ],
)
async def test_a_malformed_answer_is_a_refusal(tmp_path, scratch, monkeypatch, body):
    child(tmp_path, monkeypatch, body)
    assert await refused(tmp_path, scratch, make_settings(), "pdf", sf.text_pdf(1)) == (
        "inspection_failed"
    )


async def test_an_output_flood_is_cut_off_and_refused(tmp_path, scratch, monkeypatch):
    child(
        tmp_path,
        monkeypatch,
        "import sys\nsys.stdout.write('x' * 5_000_000)\nsys.stdout.flush()\n",
    )
    assert await refused(tmp_path, scratch, make_settings(), "txt", b"abc") == "inspection_failed"


async def test_the_child_gets_a_scrubbed_environment_and_an_empty_working_directory(
    tmp_path, scratch, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/prod")
    monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-super-secret-0123456789abcdef")
    monkeypatch.setenv("CSRF_SECRET", "csrf-secret-value")
    child(
        tmp_path,
        monkeypatch,
        """
        import json, os, sys
        request = json.loads(sys.stdin.read())
        report = {"env": sorted(os.environ), "cwd_entries": os.listdir(".")}
        with open(request["path"] + ".report", "w") as handle:
            json.dump(report, handle)
        print(json.dumps({"ok": False, "code": "report", "message": "done"}))
        """,
    )
    with pytest.raises(InspectionRejected):
        await inspect(tmp_path, scratch, make_settings(), "txt", b"abc")
    report = json.loads((tmp_path / "input.bin.report").read_text())
    env = {k.upper() for k in report["env"]}
    assert not env & {"DATABASE_URL", "AI_OPENAI_API_KEY", "CSRF_SECRET", "INTERNAL_API_SECRET"}
    assert env <= {
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "LANG",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "PYTHONUTF8",
        "LC_CTYPE",
    }
    assert report["cwd_entries"] == []  # an empty directory of its own


async def test_the_scratch_directory_is_removed_after_every_outcome(tmp_path, scratch):
    await inspect(tmp_path, scratch, make_settings(), "txt", b"abc")
    await refused(tmp_path, scratch, make_settings(), "txt", b"a\x00b")
    assert [p for p in scratch.iterdir() if p.name.startswith("inspect-")] == []


async def test_a_cancelled_inspection_kills_the_child(tmp_path, scratch, monkeypatch):
    import asyncio

    marker = tmp_path / "alive.txt"
    child(
        tmp_path,
        monkeypatch,
        f"import time\nopen({str(marker)!r}, 'w').write('started')\ntime.sleep(60)\n",
    )
    path = tmp_path / "in.bin"
    path.write_bytes(b"abc")
    task = asyncio.create_task(inspect_file(path, "txt", make_settings(), scratch))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [p for p in scratch.iterdir() if p.name.startswith("inspect-")] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits do not exist on Windows")
async def test_resource_limits_are_applied_on_posix(tmp_path, scratch, settings):
    result = await inspect(tmp_path, scratch, settings, "txt", b"abc")
    assert result.rlimits is True


async def test_the_inspector_imports_nothing_from_the_application():
    """It must stay a standalone script: it may not see settings, the database or any secret."""
    source = sandbox.CHILD_SCRIPT.read_text(encoding="utf-8")
    assert "import app" not in source and "from app" not in source
    assert "sqlalchemy" not in source and "httpx" not in source
