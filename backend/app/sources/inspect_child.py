"""The sandboxed file inspector. It runs in a CHILD PROCESS started by app/sources/sandbox.py:

    python -I app/sources/inspect_child.py        (request as JSON on stdin, one JSON line on stdout)

Why a separate process: PDF and ZIP parsers are large C/C++ or complex code that parse hostile bytes.
A crash, a hang or an exploit there must not take the API down or reach its secrets. So this file:

  * imports ONLY the standard library and pypdfium2 (never `app.*`): it cannot see settings, the
    database URL, session keys or provider keys, because it never loads that code and the parent
    starts it with an almost empty environment;
  * limits itself first thing (POSIX): address space, CPU seconds, open files, bytes it may write,
    no core dumps. The parent also enforces a wall-clock timeout and kills it;
  * refuses to create sockets (defence in depth: real network isolation needs a container or
    namespace; see docs/ai.md);
  * does CHEAP inspection only: is it a valid file of the claimed kind, how many pages, which pages
    have a text layer, which look scanned, how many embedded images. No text is extracted, no OCR
    runs, and nothing leaves the machine.

Output (one line of JSON):  {"ok": true, "kind": ..., counts..., "rlimits": bool}
                       or   {"ok": false, "code": "pdf_encrypted", "message": "..."}
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from xml.parsers import expat

DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MIN_TEXT_CHARS = 10  # fewer non-space characters than this on a page = "no text layer"
MAX_OBJECTS_PER_PAGE = 5000
BINARY_MAGICS = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x7fELF",
    b"MZ",
    b"\x89PNG",
    b"GIF8",
    b"\xff\xd8\xff",
    b"\x1f\x8b",
    b"Rar!",
    b"7z\xbc\xaf",
    b"\xd0\xcf\x11\xe0",
)


class Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- sandbox self-restriction ----------------------------------------------------------------------


def apply_limits(limits: dict) -> bool:
    """POSIX resource limits. Returns False where the `resource` module does not exist (Windows)."""
    try:
        import resource
    except ImportError:
        return False
    memory = int(limits["memory_bytes"])
    cpu = int(limits["cpu_seconds"])
    for which, value in (
        (resource.RLIMIT_AS, memory),
        (resource.RLIMIT_CPU, cpu),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_FSIZE, 0),  # this process never writes a file
        (resource.RLIMIT_CORE, 0),
    ):
        soft_hard = (value, value)
        try:
            resource.setrlimit(which, soft_hard)
        except (ValueError, OSError):
            current_hard = resource.getrlimit(which)[1]
            if current_hard != resource.RLIM_INFINITY and current_hard < value:
                continue  # already lower than what we wanted: keep it
            raise
    return True


def block_network() -> None:
    import socket

    def deny(*_args, **_kwargs):
        raise OSError("network access is disabled in the inspector")

    for name in ("socket", "create_connection", "getaddrinfo", "socketpair", "fromfd"):
        if hasattr(socket, name):
            setattr(socket, name, deny)


# ---- PDF -------------------------------------------------------------------------------------------------


def inspect_pdf(path: str, limits: dict) -> dict:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == raw.FPDF_ERR_PASSWORD:
            raise Reject("pdf_encrypted", "Password-protected PDFs are not supported.") from None
        raise Reject("pdf_invalid", "The file is not a readable PDF.") from None
    try:
        # -1 = not encrypted. Anything else (even an owner-password-only file) is refused: the
        # permission flags are not something we want to interpret.
        if raw.FPDF_GetSecurityHandlerRevision(doc.raw) != -1:
            raise Reject("pdf_encrypted", "Encrypted PDFs are not supported.")
        pages = len(doc)
        if pages < 1:
            raise Reject("pdf_invalid", "The PDF has no pages.")
        if pages > int(limits["max_pages"]):
            raise Reject("too_many_pages", f"PDFs are limited to {int(limits['max_pages'])} pages.")
        text_pages = scanned = images = chars = 0
        for index in range(pages):
            page = doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    count = textpage.count_chars()
                    sample = textpage.get_text_range(0, min(count, 4000)) if count else ""
                finally:
                    textpage.close()
                visible = len("".join(sample.split()))
                has_text = visible >= MIN_TEXT_CHARS
                chars += count
                page_images = 0
                seen = 0
                for obj in page.get_objects(max_depth=3):
                    seen += 1
                    if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                        page_images += 1
                    if seen >= MAX_OBJECTS_PER_PAGE:
                        break
                images += page_images
                if has_text:
                    text_pages += 1
                elif seen > 0:  # no text layer but something is drawn: probably a scan
                    scanned += 1
            finally:
                page.close()
        return {
            "kind": "pdf",
            "page_count": pages,
            "text_page_count": text_pages,
            "likely_scanned_pages": scanned,
            "image_count": images,
            "text_chars": chars,
        }
    finally:
        doc.close()


# ---- DOCX ------------------------------------------------------------------------------------------------


def _expat_parser(**handlers):
    parser = expat.ParserCreate(namespace_separator="|")

    def no_dtd(*_args):
        raise Reject("docx_invalid", "The document contains a DOCTYPE, which is not allowed.")

    parser.StartDoctypeDeclHandler = no_dtd
    parser.EntityDeclHandler = no_dtd
    for name, fn in handlers.items():
        setattr(parser, name, fn)
    return parser


def _read_limited(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with zf.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Reject("docx_invalid", "A part of the document is too large.")
    return data


def inspect_docx(path: str, limits: dict) -> dict:
    max_total = int(limits["docx_max_uncompressed_bytes"])
    max_entries = int(limits["docx_max_entries"])
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise Reject("docx_invalid", "The file is not a valid DOCX (Word) package.") from None
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > max_entries:
            raise Reject("docx_invalid", "The DOCX package has an invalid number of parts.")
        names: set[str] = set()
        total = 0
        images = 0
        for info in infos:
            name = info.filename
            lowered = name.lower()
            if (
                not name
                or len(name) > 255
                or name.startswith("/")
                or "\\" in name
                or "\x00" in name
                or ".." in name.split("/")
            ):
                raise Reject("docx_invalid", "The DOCX package has an unsafe part name.")
            if lowered in names:
                raise Reject("docx_invalid", "The DOCX package repeats a part name.")
            names.add(lowered)
            if info.flag_bits & 0x1:
                raise Reject("docx_encrypted", "Encrypted DOCX files are not supported.")
            if (
                lowered.endswith("vbaproject.bin")
                or lowered.endswith("vbadata.xml")
                or lowered.startswith("word/activex/")
            ):
                raise Reject("docx_macros", "Documents with macros or ActiveX are not accepted.")
            total += info.file_size
            if total > max_total:
                raise Reject("docx_invalid", "The DOCX package expands to too much data.")
            if (
                info.compress_size > 0
                and info.file_size > 1_048_576
                and info.file_size / info.compress_size > 200
            ):
                raise Reject("docx_invalid", "The DOCX package looks like a compression bomb.")
            if lowered.startswith("word/media/") and not lowered.endswith("/"):
                images += 1
        by_name = {i.filename: i for i in infos}
        ct_info = by_name.get("[Content_Types].xml")
        doc_info = by_name.get("word/document.xml")
        if ct_info is None or doc_info is None:
            raise Reject("docx_invalid", "This is not a Word document (parts are missing).")

        overrides: dict[str, str] = {}

        def on_start(tag, attrs):
            if tag.endswith("|Override") or tag == "Override":
                overrides[attrs.get("PartName", "")] = attrs.get("ContentType", "")

        try:
            _expat_parser(StartElementHandler=on_start).Parse(
                _read_limited(zf, ct_info, 1_048_576), True
            )
        except expat.ExpatError:
            raise Reject(
                "docx_invalid", "The package's content-type list is not valid XML."
            ) from None
        main = overrides.get("/word/document.xml", "")
        if "macroEnabled" in main or any("macroEnabled" in v for v in overrides.values()):
            raise Reject("docx_macros", "Macro-enabled documents are not accepted.")
        if main != DOCX_MAIN_TYPE:
            raise Reject("docx_invalid", "This is not a Word document (wrong main part type).")

        chars = 0
        depth_in_t = 0

        def start(tag, _attrs):
            nonlocal depth_in_t
            if tag == f"{W_NS}|t":
                depth_in_t += 1

        def end(tag):
            nonlocal depth_in_t
            if tag == f"{W_NS}|t":
                depth_in_t -= 1

        def data(text):
            nonlocal chars
            if depth_in_t > 0:
                chars += len(text)

        parser = _expat_parser(
            StartElementHandler=start, EndElementHandler=end, CharacterDataHandler=data
        )
        parser.buffer_text = True
        try:
            with zf.open(doc_info) as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        except expat.ExpatError:
            raise Reject("docx_invalid", "The document body is not valid XML.") from None
        return {
            "kind": "docx",
            "page_count": None,
            "text_page_count": None,
            "likely_scanned_pages": None,
            "image_count": images,
            "text_chars": chars,
        }


# ---- plain text and Markdown ---------------------------------------------------------------------------


def inspect_text(path: str, kind: str, limits: dict) -> dict:
    max_bytes = int(limits["max_text_bytes"])
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise Reject("text_invalid", "The text file is too large.")
    if not data.strip():
        raise Reject("text_invalid", "The text file is empty.")
    if b"\x00" in data:
        raise Reject("text_invalid", "The file contains NUL bytes, so it is not plain text.")
    head = data[:8]
    if any(head.startswith(magic) for magic in BINARY_MAGICS):
        raise Reject("type_mismatch", "The file contents are not plain text.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Reject("text_invalid", "Text files must be UTF-8.") from None
    controls = len(re.findall(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]", text))
    if controls and controls * 100 >= len(text):
        raise Reject("text_invalid", "The file has too many control characters to be text.")
    return {
        "kind": kind,
        "page_count": None,
        "text_page_count": None,
        "likely_scanned_pages": None,
        "image_count": 0,
        "text_chars": len(text),
    }


# ---- entry point -----------------------------------------------------------------------------------------


def run(request: dict) -> dict:
    limits = request["limits"]
    applied = apply_limits(limits)
    block_network()
    kind = request["kind"]
    path = request["path"]
    try:
        if kind == "pdf":
            result = inspect_pdf(path, limits)
        elif kind == "docx":
            result = inspect_docx(path, limits)
        elif kind in ("txt", "md"):
            result = inspect_text(path, kind, limits)
        else:
            raise Reject("unsupported_media_type", "Unknown file kind.")
    except Reject as rejected:
        return {"ok": False, "code": rejected.code, "message": rejected.message, "rlimits": applied}
    return {"ok": True, "rlimits": applied, **result}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(65536))
        outcome = run(request)
    except MemoryError:
        outcome = {"ok": False, "code": "inspection_failed", "message": "Out of memory."}
    except Exception:  # noqa: BLE001 - any failure is "could not inspect"; details stay out of output
        outcome = {
            "ok": False,
            "code": "inspection_failed",
            "message": "Could not inspect the file.",
        }
    sys.stdout.write(json.dumps(outcome) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
