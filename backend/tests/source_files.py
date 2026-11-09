"""Builders for the files the source tests upload: real PDFs, DOCX packages and text files, plus the
hostile variants (encrypted, macro-enabled, bomb, wrong signature)."""

from __future__ import annotations

import io
import os
import zipfile

from PIL import Image

from app.sources.files import DOCX_TYPE  # noqa: F401 - re-exported for tests

DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def text_pdf(pages: list[str] | int = 2) -> bytes:
    """A real PDF with a text layer (Helvetica), one line per page."""
    if isinstance(pages, int):
        pages = [
            f"Page {i + 1}: the quarterly report says revenue grew steadily." for i in range(pages)
        ]
    objects: list[bytes] = []
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, line in enumerate(pages):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1", "replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def scanned_pdf(pages: int = 2) -> bytes:
    """Image-only pages: no text layer, one picture each (what a scan looks like)."""
    images = []
    for i in range(pages):
        data = os.urandom(120 * 90 * 3)
        images.append(Image.frombytes("RGB", (120, 90), data).convert("L").convert("RGB"))
        images[-1].putpixel((0, 0), (i, i, i))
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return buf.getvalue()


def mixed_pdf() -> bytes:
    """Page 1 has text, page 2 is a scan. Built by importing pages with pypdfium2."""
    import pypdfium2 as pdfium

    first = pdfium.PdfDocument(text_pdf(1))
    second = pdfium.PdfDocument(scanned_pdf(1))
    first.import_pages(second)
    buf = io.BytesIO()
    first.save(buf)
    return buf.getvalue()


def encrypted_pdf(user_password: str = "secret") -> bytes:  # noqa: S107
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password=user_password, owner_password="owner-pw")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def docx(
    paragraphs: list[str] | None = None,
    *,
    images: int = 0,
    macros: bool = False,
    main_type: str = DOCX_MAIN,
    extra: dict[str, bytes] | None = None,
    skip: tuple[str, ...] = (),
    doctype: str = "",
) -> bytes:
    paragraphs = (
        paragraphs
        if paragraphs is not None
        else ["Hello from a Word document.", "Second paragraph."]
    )
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    parts: dict[str, bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{main_type}"/></Types>'
        ).encode(),
        "_rels/.rels": b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?>{doctype}<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        ).encode(),
    }
    for i in range(images):
        parts[f"word/media/image{i + 1}.png"] = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    if macros:
        parts["word/vbaProject.bin"] = b"\xd0\xcf\x11\xe0 macro"
    parts.update(extra or {})
    for name in skip:
        parts.pop(name, None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


def zip_bomb_docx(megabytes: int = 30) -> bytes:
    """Valid structure, but one part decompresses to `megabytes` MiB from a few KB."""
    return docx(extra={"word/bomb.bin": b"\x00" * (megabytes * 1024 * 1024)})


def utf8_text(text: str = "A plain text source.\nSecond line.\n") -> bytes:
    return text.encode("utf-8")
