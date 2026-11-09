"""File names and file types for sources: pure functions, no I/O.

Accepted: PDF, DOCX, plain text, Markdown. The KIND is decided from the file extension and then
checked three ways: the type the browser declared must be compatible, the first bytes (the
"signature") must be what that kind really starts with, and later the sandboxed inspector parses the
whole file. A name or a declared type alone never decides anything.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
KIND_BY_EXTENSION = {"pdf": "pdf", "docx": "docx", "txt": "txt", "md": "md", "markdown": "md"}
MEDIA_TYPE_BY_KIND = {
    "pdf": "application/pdf",
    "docx": DOCX_TYPE,
    "txt": "text/plain",
    "md": "text/markdown",
}
# What a browser may declare for each kind (an empty/unknown declaration is allowed: browsers send
# nothing for .md). application/octet-stream and every other type are refused.
DECLARED_OK = {
    "pdf": {"application/pdf"},
    "docx": {DOCX_TYPE},
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/x-markdown", "text/plain"},
}
MAX_NAME_LENGTH = 150
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_BAD = re.compile(r'[<>:"|?*]')


class UnsupportedFile(Exception):
    """code is 'unsupported_media_type' (415) or 'type_mismatch' (415)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def extension_of(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def sanitize_name(raw: str) -> str:
    """A name that is safe to store, show and put in a download header.

    * Unicode NFC; only the last path component survives (both / and \\ separate), so "..\\x.pdf"
      or "a/b/c.pdf" become "x.pdf" and "c.pdf";
    * control characters, bidirectional controls (they can make "txt.exe" look like "exe.txt"),
      line/paragraph separators and unassigned or private-use code points are removed;
    * Windows-reserved punctuation becomes "_", whitespace runs collapse, leading dots/spaces and
      trailing dots/spaces go, reserved device names get a prefix;
    * the extension is lower-cased and kept; the whole name is cut to MAX_NAME_LENGTH characters.
    Never empty: falls back to "file".
    """
    name = unicodedata.normalize("NFC", raw)
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(
        c for c in name if not unicodedata.category(c).startswith(("C", "Z")) or c == " "
    )
    name = _BAD.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").rstrip(". ")
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        name = f"{stem.rstrip(' .')}.{ext.lower()}" if stem.strip(" .") else ext
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    room = MAX_NAME_LENGTH - (len(ext) + 1 if ext else 0)
    stem = stem[: max(1, room)].rstrip(" .") or "file"
    return f"{stem}.{ext}" if ext else stem


def classify(name: str, declared_type: str | None) -> str:
    """The kind for a (sanitized) file name and the declared type, or UnsupportedFile."""
    kind = KIND_BY_EXTENSION.get(extension_of(name))
    if kind is None:
        raise UnsupportedFile(
            "unsupported_media_type", "Only PDF, DOCX, TXT and Markdown (.md) files are accepted."
        )
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared and declared not in DECLARED_OK[kind]:
        raise UnsupportedFile("type_mismatch", "The file type does not match its extension.")
    return kind


def sniff(head: bytes) -> str:
    """What the first bytes are: 'pdf', 'zip' (DOCX is a ZIP), 'binary' (other known binary
    formats) or 'text-like' (none of the known signatures)."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    for magic in (
        b"\x7fELF",
        b"MZ",
        b"\x89PNG",
        b"GIF8",
        b"\xff\xd8\xff",
        b"\x1f\x8b",
        b"Rar!",
        b"7z\xbc\xaf",
        b"\xd0\xcf\x11\xe0",
    ):
        if head.startswith(magic):
            return "binary"
    return "text-like"


_EXPECTED_SIGNATURE = {"pdf": "pdf", "docx": "zip", "txt": "text-like", "md": "text-like"}


def check_signature(kind: str, head: bytes) -> None:
    if sniff(head) != _EXPECTED_SIGNATURE[kind]:
        raise UnsupportedFile("type_mismatch", "The file contents do not match its extension.")


def content_disposition(name: str) -> str:
    """Attachment header with an ASCII fallback and the real name in RFC 5987 form."""
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name).replace('"', "_") or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
