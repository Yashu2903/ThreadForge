"""File names and file types for sources: pure functions."""

import pytest

from app.sources import files
from tests import source_files as sf


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("Report.PDF", "Report.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\windows\\system32\\notes.md", "notes.md"),
        ("a/b/c.docx", "c.docx"),
        ("  spaced   out  name .pdf", "spaced out name.pdf"),
        (".hidden.txt", "hidden.txt"),
        ("trailing dots...", "trailing dots"),
        ('we<ird>:na"me|?*.txt', "we_ird__na_me___.txt"),
        ("con.txt", "_con.txt"),
        ("NUL.md", "_NUL.md"),
        ("", "file"),
        ("///", "file"),
        ("...", "file"),
        ("tab\there\x00nul.pdf", "tabhere" + "nul.pdf"),
    ],
)
def test_sanitize_name(raw, expected):
    assert files.sanitize_name(raw) == expected


def test_bidi_and_control_characters_are_removed():
    # U+202E (right-to-left override) would make "exe.txt" display as "txt.exe"
    evil = "invoice‮fdp.exe"
    cleaned = files.sanitize_name(evil + ".pdf")
    assert "‮" not in cleaned and cleaned.endswith(".pdf")
    for ch in ("‎", "‏", "⁦", "⁩", " ", " ", "\x1b", "\x7f", "﻿"):
        assert files.sanitize_name(f"a{ch}b.txt") == "ab.txt"


def test_names_are_normalized_and_cut_to_length_keeping_the_extension():
    assert files.sanitize_name("café.txt") == "café.txt"  # NFC
    long = files.sanitize_name("x" * 400 + ".pdf")
    assert len(long) <= files.MAX_NAME_LENGTH and long.endswith(".pdf")


@pytest.mark.parametrize(
    ("name", "declared", "kind"),
    [
        ("a.pdf", "application/pdf", "pdf"),
        ("a.pdf", None, "pdf"),
        ("a.docx", files.DOCX_TYPE, "docx"),
        ("a.txt", "text/plain; charset=utf-8", "txt"),
        ("a.md", "text/markdown", "md"),
        ("a.md", "", "md"),
        ("a.markdown", "text/plain", "md"),
        ("A.MD", None, "md"),
    ],
)
def test_classify_accepts(name, declared, kind):
    assert files.classify(name, declared) == kind


@pytest.mark.parametrize(
    ("name", "declared", "code"),
    [
        ("a.exe", None, "unsupported_media_type"),
        ("a", None, "unsupported_media_type"),
        ("a.doc", "application/msword", "unsupported_media_type"),
        ("a.png", "image/png", "unsupported_media_type"),
        ("a.html", "text/html", "unsupported_media_type"),
        ("a.pdf", "text/plain", "type_mismatch"),
        ("a.docx", "application/pdf", "type_mismatch"),
        ("a.txt", "application/octet-stream", "type_mismatch"),
        ("a.md", "application/pdf", "type_mismatch"),
    ],
)
def test_classify_refuses(name, declared, code):
    with pytest.raises(files.UnsupportedFile) as caught:
        files.classify(name, declared)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("kind", "data", "ok"),
    [
        ("pdf", sf.text_pdf(1), True),
        ("pdf", sf.docx(), False),
        ("pdf", b"hello", False),
        ("docx", sf.docx(), True),
        ("docx", sf.text_pdf(1), False),
        ("docx", b"plain text", False),
        ("txt", b"plain text\n", True),
        ("txt", sf.text_pdf(1), False),  # a PDF renamed .txt
        ("txt", sf.docx(), False),  # a ZIP renamed .txt
        ("md", b"\x7fELF....", False),
        ("md", b"# Title\n", True),
        ("md", b"MZ\x90\x00", False),
        ("txt", b"\x89PNG\r\n\x1a\n", False),
    ],
)
def test_signature_must_match_the_kind(kind, data, ok):
    head = data[:16]
    if ok:
        files.check_signature(kind, head)
    else:
        with pytest.raises(files.UnsupportedFile) as caught:
            files.check_signature(kind, head)
        assert caught.value.code == "type_mismatch"


def test_download_header_has_a_safe_ascii_fallback_and_the_real_name():
    header = files.content_disposition('résumé "final".pdf')
    assert header.startswith("attachment;")
    assert 'filename="r_sum_ _final_.pdf"' in header or 'filename="r_sum' in header
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20%22final%22.pdf" in header
    assert "\n" not in header and "\r" not in header
