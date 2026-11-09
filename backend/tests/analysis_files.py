"""Builders for the files the analysis tests use (checkpoint 3): scans with real printed text, noise
that no OCR can read, drawing pages, a diagram picture, and a Word file with headings, a list, a
table and pictures. Real bytes, real parsers: nothing here is mocked."""

from __future__ import annotations

import io
import os
import zipfile

from PIL import Image, ImageDraw, ImageFont

from tests.source_files import DOCX_MAIN, W

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

CLEAN_SCAN_LINES = [
    "Quarterly operations review for the northern region",
    "The warehouse processed twelve thousand orders in March.",
    "Delivery delays were caused by a shortage of drivers.",
    "Management approved additional hiring for the summer season.",
    "Customer satisfaction improved after the new tracking system launched.",
    "Returns decreased because packaging standards were updated.",
    "The finance team expects steady revenue growth next quarter.",
    "Safety inspections found no major incidents this period.",
]

MARKDOWN_SAMPLE = """# Release process

Every release goes through three gates.

## Build

- compile the sources
- run the unit tests

## Deploy

| Stage | Owner |
|-------|-------|
| Build | Alice |
| Deploy | Bob |

The deploy step restarts the workers one at a time.
"""


def _font(size: int):
    return ImageFont.load_default(size=size)


def page_image(lines: list[str], *, size=(2550, 3300), font_px: int = 46) -> Image.Image:
    """A white US-Letter page at 300 dpi with `lines` of black text (what a clean scan looks like)."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    y = 220
    for line in lines:
        draw.text((220, y), line, fill="black", font=_font(font_px))
        y += int(font_px * 1.9)
    return image


def pdf_from_images(images: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=300)
    return buf.getvalue()


def ocr_scan_pdf(pages: int = 1, lines: list[str] | None = None) -> bytes:
    """Scanned pages with clean, readable printed text: local OCR should accept them."""
    return pdf_from_images([page_image(lines or CLEAN_SCAN_LINES) for _ in range(pages)])


def noise_scan_pdf(pages: int = 1) -> bytes:
    """Pages that are pure noise (an unreadable scan): local OCR must reject them."""
    imgs = []
    for _ in range(pages):
        data = os.urandom(850 * 1100 * 3)
        imgs.append(Image.frombytes("RGB", (850, 1100), data).resize((2550, 3300)))
    return pdf_from_images(imgs)


def diagram_png(size=(640, 420), labels=("Client", "API", "Worker", "Database")) -> bytes:
    """A small architecture diagram: labelled boxes joined by arrows (not decorative, not blank)."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = _font(22)
    boxes = []
    for i, label in enumerate(labels):
        x = 30 + i * 150
        draw.rectangle([x, 150, x + 120, 230], outline="black", width=4, fill=(200, 220, 255))
        draw.text((x + 14, 176), label, fill="black", font=font)
        boxes.append(x)
    for left, right in zip(boxes, boxes[1:], strict=False):
        draw.line([left + 120, 190, right, 190], fill="black", width=4)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def solid_png(color=(255, 255, 255), size=(300, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def tiny_png() -> bytes:
    return solid_png((10, 10, 10), (16, 16))


def _pdf_package(streams: list[bytes]) -> bytes:
    n = len(streams)
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, stream in enumerate(streams):
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


def drawing_pdf(pages: int = 1, text: str = "System overview") -> bytes:
    """Pages that are mostly a vector drawing (many paths, a few words): figure pages."""
    streams = []
    for _ in range(pages):
        ops = ["BT /F1 14 Tf 72 740 Td (" + text + ") Tj ET"]
        for row in range(5):
            for col in range(6):
                x, y = 60 + col * 90, 120 + row * 110
                ops.append(f"0.{(row * 6 + col) % 9 + 1} 0.4 0.8 rg {x} {y} 70 60 re f")
                ops.append(f"0 0 0 RG 2 w {x} {y} 70 60 re S")
        streams.append("\n".join(ops).encode())
    return _pdf_package(streams)


def long_text_pdf(paragraphs_per_page: int = 6, pages: int = 3) -> bytes:
    """Text PDF with enough text per page to produce several chunks overall."""
    streams = []
    for p in range(pages):
        ops = ["BT /F1 11 Tf 72 740 Td 14 TL"]
        for k in range(paragraphs_per_page):
            line = f"Page {p + 1} paragraph {k + 1}: the platform team documented the rollout plan in detail."
            ops.append(f"({line}) Tj T*")
        ops.append("ET")
        streams.append("\n".join(ops).encode("latin-1"))
    return _pdf_package(streams)


def rich_docx(
    *,
    figure: bytes | None = None,
    extra_images: dict[str, bytes] | None = None,
    sections: int = 2,
    words_per_section: int = 60,
) -> bytes:
    """A Word file with Title/Heading styles, paragraphs, a list item, a 3-row table and (optionally)
    pictures referenced from the body, in order."""
    body = [
        '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Platform Handbook</w:t></w:r></w:p>'
    ]
    pictures: list[tuple[str, str, bytes]] = []
    if figure:
        pictures.append(("rId10", "image1.png", figure))
    for i, (name, blob) in enumerate((extra_images or {}).items()):
        pictures.append((f"rId{11 + i}", name, blob))
    media = {f"word/media/{name}": blob for _rid, name, blob in pictures}
    rels = [
        f'<Relationship Id="{rid}" Type="{R}/image" Target="media/{name}"/>'
        for rid, name, _blob in pictures
    ]
    filler = " ".join(["alpha"] * words_per_section)
    for s in range(1, sections + 1):
        body.append(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>Section {s} overview</w:t></w:r></w:p>"
        )
        body.append(
            f"<w:p><w:r><w:t>Section {s} describes the deployment pipeline. {filler}</w:t></w:r></w:p>"
        )
        body.append(
            '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Details</w:t></w:r></w:p>'
        )
        body.append(
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            "<w:r><w:t>Build the release candidate</w:t></w:r></w:p>"
        )
    rows = (("Stage", "Owner"), ("Build", "Alice"), ("Deploy", "Bob"))
    body.append(
        "<w:tbl>"
        + "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
            + "</w:tr>"
            for row in rows
        )
        + "</w:tbl>"
    )
    for rid, _name, _blob in pictures:
        body.append(
            f'<w:p><w:r><w:drawing><a:graphic xmlns:a="{A}"><a:graphicData>'
            f'<a:blip xmlns:r="{R}" r:embed="{rid}"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>'
        )
    styles = (
        f'<?xml version="1.0"?><w:styles xmlns:w="{W}">'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
        "</w:styles>"
    )
    doc_rels = (
        '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    )
    parts: dict[str, bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            f'<Override PartName="/word/document.xml" ContentType="{DOCX_MAIN}"/></Types>'
        ).encode(),
        "_rels/.rels": b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}">'
            f"<w:body>{''.join(body)}</w:body></w:document>"
        ).encode(),
        "word/styles.xml": styles.encode(),
        "word/_rels/document.xml.rels": doc_rels.encode(),
        **media,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()
