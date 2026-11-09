"""The sandboxed EXTRACTOR. Runs in a child process started by app/sources/extract_sandbox.py:

    python -I app/sources/extract_child.py      (request as JSON on stdin; results as FILES in its cwd)

It reads one stored source file (a PDF, DOCX, TXT or Markdown file that the inspector already
accepted) and produces, in its own empty working directory:

    result.json    pages (PDF), blocks (DOCX/text), outline, figure candidates, statistics
    p<N>.png       the page image of every PDF page whose local OCR failed (for the vision fallback)
    f<N>.png       every figure candidate that is not decorative (re-encoded here, in the sandbox)

The same isolation as the inspector (app/sources/inspect_child.py): imports ONLY the standard
library, pypdfium2 and Pillow (never `app.*`), so it cannot see settings, the database URL or any key;
the parent starts it with an almost empty environment; on POSIX it limits its own memory, CPU, open
files and file size first thing; it refuses to open sockets. It may start ONE kind of subprocess:
the Tesseract binary, by the absolute path the parent gave it, with a per-page timeout.

What it decides, and what it leaves to the parent:
  * decides: which text comes from the text layer, which pages need OCR, whether the OCR result is
    good enough (confidence, word count for the page size, share of non-word-like tokens), which
    images are decorative (tiny, repeated, near-blank) and which pages are mostly drawings;
  * leaves to the parent: everything that needs a provider (vision transcription, figure
    descriptions, embeddings), chunking, storing, and the final judgement on unreadable pages.

Output on stdout (one line):  {"ok": true, "rlimits": bool}  or  {"ok": false, "code": ..., "message": ...}
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from xml.parsers import expat

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MIN_TEXT_CHARS = 10  # fewer non-space characters than this on a page = "no text layer"
MAX_OBJECTS_PER_PAGE = 5000
MAX_OUTPUT_BYTES = 400 * 1024 * 1024  # all files this process writes, together
MAX_IMAGE_SIDE = 2400  # page and figure images are scaled down to this longest side
FIGURE_PAGE_MIN_OBJECTS = 20  # path+image objects on a page with little text = "mostly a drawing"
FIGURE_PAGE_MAX_TEXT = 500
EMBEDDED_IMAGE_MAX_BYTES = 30 * 1024 * 1024
MAX_CANDIDATES = 150  # figure candidates held in memory at once; more are counted as capped


class Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- sandbox self-restriction ----------------------------------------------------------------------


def apply_limits(limits: dict) -> bool:
    try:
        import resource
    except ImportError:
        return False
    for which, value in (
        (resource.RLIMIT_AS, int(limits["memory_bytes"])),
        (resource.RLIMIT_CPU, int(limits["cpu_seconds"])),
        (resource.RLIMIT_NOFILE, 128),
        (resource.RLIMIT_FSIZE, 128 * 1024 * 1024),  # one file at most; MAX_OUTPUT_BYTES in total
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            current_hard = resource.getrlimit(which)[1]
            if current_hard != resource.RLIM_INFINITY and current_hard < value:
                continue
            raise
    return True


def block_network() -> None:
    import socket

    def deny(*_args, **_kwargs):
        raise OSError("network access is disabled in the extractor")

    for name in ("socket", "create_connection", "getaddrinfo", "socketpair", "fromfd"):
        if hasattr(socket, name):
            setattr(socket, name, deny)


class Budget:
    """Counts what this process writes so a hostile file cannot fill the disk."""

    def __init__(self, max_chars: int) -> None:
        self.chars = 0
        self.max_chars = max_chars
        self.bytes = 0

    def add_chars(self, n: int) -> None:
        self.chars += n
        if self.chars > self.max_chars:
            raise Reject("extracted_text_too_large", "The file contains too much text to analyze.")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.bytes > MAX_OUTPUT_BYTES:
            raise Reject("extraction_output_too_large", "The file produced too much data.")


# ---- text quality (OCR gate) -------------------------------------------------------------------------

_VOWELS = set("aeiouyAEIOUY")
_WORD_TRIM = re.compile(r"^[^\w]+|[^\w]+$")


def plausible_token(token: str) -> bool:
    """A cheap word-likeness test. It is a HEURISTIC, not a dictionary: no word list ships with the
    standard library. Numbers, short acronyms and ordinary words pass; keyboard mash and the
    symbol soup of a bad scan do not."""
    t = _WORD_TRIM.sub("", token)
    if not t:
        return True  # pure punctuation is not a word to judge
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    if digits and letters:
        return digits * 2 >= len(t) or (len(t) <= 8 and letters <= 2)  # "3rd", "A4", "x2"
    if digits:
        return True
    if not letters:
        return False
    if t.isupper() and len(t) <= 6:
        return True  # acronyms (TCP, HTTP)
    if len(t) <= 2:
        return True
    if not any(c in _VOWELS for c in t):
        return False
    if re.search(r"[^aeiouyAEIOUY\W\d_]{6,}", t):
        return False
    return not re.search(r"(.)\1{3,}", t)


def implausible_share(tokens: list[str]) -> float:
    judged = [t for t in tokens if _WORD_TRIM.sub("", t)]
    if not judged:
        return 1.0
    return sum(1 for t in judged if not plausible_token(t)) / len(judged)


# ---- Tesseract ------------------------------------------------------------------------------------------


def run_tesseract(png: str, limits: dict) -> dict:
    """OCR one page image. Returns {"text", "mean_conf", "words", "high_words"} or raises Reject."""
    tess = limits.get("tesseract")
    if not tess:
        raise Reject("tesseract_unavailable", "Tesseract is not installed.")
    env = {"OMP_THREAD_LIMIT": "1", "LANG": "C.UTF-8"}
    for name in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TESSDATA_PREFIX"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    try:
        done = subprocess.run(  # noqa: S603 - absolute path chosen by the parent, fixed arguments
            [tess, png, "stdout", "-l", "eng", "--dpi", str(int(limits["ocr_dpi"])), "tsv"],
            capture_output=True,
            timeout=int(limits["ocr_timeout"]),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Reject("tesseract_timeout", "OCR took too long.") from None
    except OSError:
        raise Reject("tesseract_unavailable", "Tesseract could not be started.") from None
    if done.returncode != 0:
        raise Reject("tesseract_failed", "OCR failed.")
    return parse_tsv(done.stdout.decode("utf-8", "replace"))


def parse_tsv(tsv: str) -> dict:
    """Tesseract TSV: level page_num block_num par_num line_num word_num left top width height conf
    text. Level 5 rows are words; conf is -1 for non-words."""
    lines_out: dict[tuple[int, int, int], list[str]] = {}
    order: list[tuple[int, int, int]] = []
    confs: list[float] = []
    words: list[str] = []
    high: list[str] = []
    for row in tsv.splitlines()[1:]:
        cells = row.split("\t")
        if len(cells) < 12 or cells[0] != "5":
            continue
        word = cells[11].strip()
        if not word:
            continue
        try:
            conf = float(cells[10])
            key = (int(cells[2]), int(cells[3]), int(cells[4]))
        except ValueError:
            continue
        if key not in lines_out:
            lines_out[key] = []
            order.append(key)
        lines_out[key].append(word)
        words.append(word)
        if conf >= 0:
            confs.append(conf)
            if conf >= 80 and len(word) >= 4 and word.isalpha():
                high.append(word.lower())
    paragraphs: list[str] = []
    current: list[str] = []
    last_par: tuple[int, int] | None = None
    for key in order:
        par = (key[0], key[1])
        if last_par is not None and par != last_par and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(" ".join(lines_out[key]))
        last_par = par
    if current:
        paragraphs.append(" ".join(current))
    return {
        "text": "\n\n".join(paragraphs),
        "mean_conf": (sum(confs) / len(confs)) if confs else 0.0,
        "words": len(words),
        "tokens": words,
        "high_words": sorted(set(high))[:400],
    }


def ocr_verdict(ocr: dict, page_area_sq_in: float, limits: dict) -> str | None:
    """None = good enough, else the reason it is not."""
    if ocr["words"] == 0:
        return "no_words"
    min_words = max(3, min(int(limits["ocr_min_words"]), round(page_area_sq_in * 0.25)))
    if ocr["words"] < min_words:
        return "too_few_words"
    if ocr["mean_conf"] < float(limits["ocr_min_confidence"]):
        return "low_confidence"
    if implausible_share(ocr["tokens"]) > float(limits["ocr_max_implausible"]):
        return "nonsense_tokens"
    return None


# ---- images --------------------------------------------------------------------------------------------


def _pil():
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 120_000_000
    return Image


def save_png(image, path: str, budget: Budget) -> tuple[int, int, str]:
    """Scale down to MAX_IMAGE_SIDE, save as PNG (no metadata), return (w, h, sha256 of pixels)."""
    Image = _pil()
    img = image
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        w, h = img.size
    digest = hashlib.sha256(img.tobytes()).hexdigest()
    img.save(path, "PNG", optimize=False)
    budget.add_bytes(os.path.getsize(path))
    return w, h, digest


def decode_pdf_image(obj):
    """The pixels of an embedded PDF image, or None when PDFium cannot decode it."""
    try:
        return obj.get_bitmap(render=False).to_pil()
    except Exception:  # noqa: BLE001 - an image PDFium cannot decode is simply not a candidate
        return None


def open_image(blob: bytes):
    """A decoded Pillow image from bytes, or None when Pillow cannot read them."""
    import io

    try:
        image = _pil().open(io.BytesIO(blob))
        image.load()
        return image
    except Exception:  # noqa: BLE001 - not an image Pillow can read: not a candidate
        return None


def is_near_blank(image) -> bool:
    small = image.convert("L").resize((32, 32))
    values = list(small.getdata())
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance < 9.0  # standard deviation below 3 of 255: one flat colour


# ---- PDF -----------------------------------------------------------------------------------------------


def extract_pdf(path: str, limits: dict, workdir: str, budget: Budget) -> dict:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        raise Reject("pdf_invalid", "The file is not a readable PDF.") from None
    pages_out: list[dict] = []
    candidates: list[dict] = []
    capped = False
    ocr_done = 0
    vision_marked = 0
    try:
        total = len(doc)
        if total < 1 or total > int(limits["max_pages"]):
            raise Reject("too_many_pages", "The PDF has too many pages.")
        toc: list[dict] = []
        try:
            for item in doc.get_toc():
                if len(toc) >= 500:
                    break
                toc.append(
                    {
                        "title": str(item.title or "")[:200],
                        "level": int(item.level),
                        "page": (int(item.page_index) + 1) if item.page_index is not None else None,
                    }
                )
        except Exception:  # noqa: BLE001 - a broken outline just means no outline
            toc = []
        for index in range(total):
            number = index + 1
            page = doc[index]
            try:
                width_pt, height_pt = page.get_size()
                area = (width_pt * height_pt) / 5184.0  # square inches (72 pt = 1 inch)
                textpage = page.get_textpage()
                try:
                    count = textpage.count_chars()
                    text = textpage.get_text_range(0, count) if count else ""
                finally:
                    textpage.close()
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                visible = len("".join(text.split()))
                path_objs = image_objs = 0
                big_images: list[object] = []
                seen = 0
                for obj in page.get_objects(max_depth=3):
                    seen += 1
                    if obj.type == raw.FPDF_PAGEOBJ_PATH:
                        path_objs += 1
                    elif obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                        image_objs += 1
                        big_images.append(obj)
                    if seen >= MAX_OBJECTS_PER_PAGE:
                        break
                entry: dict = {
                    "page": number,
                    "extraction": "text_layer",
                    "text": "",
                    "ocr_confidence": None,
                    "status": "ok",
                    "reason": None,
                    "image": None,
                    "tess_words": [],
                }
                if visible >= MIN_TEXT_CHARS:
                    budget.add_chars(len(text))
                    entry["text"] = text
                elif seen == 0:
                    entry["extraction"] = "none"
                    entry["status"] = "empty"  # a genuinely blank page: nothing to read, not a gap
                else:
                    # no text layer, something is drawn: a scan (or a drawing). Try local OCR.
                    scale = float(limits["ocr_dpi"]) / 72.0
                    bitmap = page.render(scale=scale)
                    pil = bitmap.to_pil()
                    shot = os.path.join(workdir, f"ocr-{number}.png")
                    pil.convert("L").save(shot, "PNG")
                    budget.add_bytes(os.path.getsize(shot))
                    verdict: str | None
                    ocr: dict | None = None
                    if ocr_done >= int(limits["max_ocr_pages"]):
                        verdict = "ocr_page_cap"
                    else:
                        ocr_done += 1
                        try:
                            ocr = run_tesseract(shot, limits)
                            verdict = ocr_verdict(ocr, area, limits)
                        except Reject as why:
                            verdict = why.code
                    entry["extraction"] = "tesseract"
                    if ocr is not None:
                        entry["ocr_confidence"] = round(ocr["mean_conf"], 1)
                        entry["tess_words"] = ocr["high_words"]
                    if verdict is None and ocr is not None:
                        budget.add_chars(len(ocr["text"]))
                        entry["text"] = ocr["text"]
                    else:
                        entry["status"] = "failed"
                        entry["reason"] = verdict
                        if ocr is not None:
                            entry["text"] = ocr["text"]  # kept for the cross-check, never indexed
                        if vision_marked < int(limits["vision_max_pages"]):
                            vision_marked += 1
                            name = f"p{number}.png"
                            rgb = bitmap.to_pil().convert("RGB")
                            save_png(rgb, os.path.join(workdir, name), budget)
                            entry["image"] = name
                    with contextlib.suppress(OSError):
                        os.remove(shot)
                pages_out.append(entry)

                # ---- figure candidates --------------------------------------------------------
                text_page = entry["extraction"] == "text_layer"
                as_page = (
                    text_page
                    and visible <= FIGURE_PAGE_MAX_TEXT
                    and (path_objs + image_objs) >= FIGURE_PAGE_MIN_OBJECTS
                )
                if (as_page or (text_page and big_images)) and len(candidates) >= MAX_CANDIDATES:
                    capped = True
                elif as_page:
                    pil = page.render(
                        scale=min(2.0, MAX_IMAGE_SIDE / max(width_pt, height_pt))
                    ).to_pil()
                    candidates.append({"pil": pil.convert("RGB"), "page": number, "origin": "page"})
                elif text_page:
                    for obj in big_images:
                        if len(candidates) >= MAX_CANDIDATES:
                            capped = True
                            break
                        pil = decode_pdf_image(obj)
                        if pil is not None:
                            candidates.append({"pil": pil, "page": number, "origin": "embedded"})
            finally:
                page.close()
    finally:
        doc.close()

    figures = select_figures(candidates, limits, workdir, budget)
    return {
        "kind": "pdf",
        "pages": pages_out,
        "toc": toc,
        "figures": figures["kept"],
        "figures_found": figures["found"],
        "figures_capped": capped,
        "stats": {"pages": total, "ocr_pages": ocr_done},
    }


def select_figures(candidates: list[dict], limits: dict, workdir: str, budget: Budget) -> dict:
    """Drop decorative images (tiny, repeated across pages, near-blank), count the rest, and save the
    first `max_figures` of them. Hashes are of the (possibly down-scaled) pixels."""
    Image = _pil()
    min_pixels = int(limits["figure_min_pixels"])
    max_repeats = int(limits["figure_max_repeats"])
    cap = int(limits["max_figures"])
    hashed: list[tuple[dict, str]] = []
    pages_of: dict[str, set[int]] = {}
    for cand in candidates:
        pil = cand["pil"]
        w, h = pil.size
        if w * h < min_pixels or min(w, h) < 32:
            continue
        small = pil.convert("RGB").resize((64, 64), Image.BILINEAR)
        digest = hashlib.sha256(small.tobytes()).hexdigest()
        pages_of.setdefault(digest, set()).add(cand["page"] if cand["page"] else -1)
        hashed.append((cand, digest))
    found = 0
    kept: list[dict] = []
    for cand, digest in hashed:
        if len(pages_of[digest]) > max_repeats:
            continue  # the same image on many pages: a logo, a letterhead, a background
        if is_near_blank(cand["pil"]):
            continue
        found += 1
        if len(kept) >= cap:
            continue
        name = f"f{len(kept) + 1}.png"
        w, h, sha = save_png(cand["pil"], os.path.join(workdir, name), budget)
        kept.append(
            {
                "file": name,
                "page": cand.get("page"),
                "after_block": cand.get("after_block"),
                "origin": cand["origin"],
                "sha256": sha,
                "width": w,
                "height": h,
            }
        )
    return {"kept": kept, "found": found}


# ---- DOCX ----------------------------------------------------------------------------------------------


def _expat(**handlers):
    parser = expat.ParserCreate(namespace_separator="|")

    def no_dtd(*_args):
        raise Reject("docx_invalid", "The document contains a DOCTYPE, which is not allowed.")

    parser.StartDoctypeDeclHandler = no_dtd
    parser.EntityDeclHandler = no_dtd
    for name, fn in handlers.items():
        setattr(parser, name, fn)
    parser.buffer_text = True
    return parser


def _read_part(zf: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    with zf.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Reject("docx_invalid", "A part of the document is too large.")
    return data


def docx_style_levels(zf: zipfile.ZipFile) -> dict[str, int]:
    """styleId -> heading level, from word/styles.xml ("heading 2" -> 2, "Title" -> 1)."""
    data = _read_part(zf, "word/styles.xml", 8 * 1024 * 1024)
    levels: dict[str, int] = {}
    if data is None:
        return levels
    current: dict[str, str | None] = {"id": None}

    def start(tag, attrs):
        if tag == f"{W_NS}|style":
            current["id"] = attrs.get(f"{W_NS}|styleId")
        elif tag == f"{W_NS}|name" and current["id"]:
            name = (attrs.get(f"{W_NS}|val") or "").lower()
            match = re.fullmatch(r"heading\s*(\d)", name)
            if match:
                levels[current["id"]] = int(match.group(1))
            elif name == "title":
                levels[current["id"]] = 1

    try:
        _expat(StartElementHandler=start).Parse(data, True)
    except expat.ExpatError:
        return {}
    return levels


def docx_relationships(zf: zipfile.ZipFile) -> dict[str, str]:
    data = _read_part(zf, "word/_rels/document.xml.rels", 2 * 1024 * 1024)
    rels: dict[str, str] = {}
    if data is None:
        return rels

    def start(tag, attrs):
        if (
            tag == f"{REL_NS}|Relationship"
            and attrs.get("Type", "").endswith("/image")
            and attrs.get("TargetMode") != "External"
        ):
            rels[attrs.get("Id", "")] = attrs.get("Target", "")

    try:
        _expat(StartElementHandler=start).Parse(data, True)
    except expat.ExpatError:
        return {}
    return rels


def extract_docx(path: str, limits: dict, workdir: str, budget: Budget) -> dict:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise Reject("docx_invalid", "The file is not a valid DOCX package.") from None
    with zf:
        styles = docx_style_levels(zf)
        rels = docx_relationships(zf)
        body = zf.getinfo("word/document.xml")
        if body.file_size > int(limits["docx_max_uncompressed_bytes"]):
            raise Reject("docx_invalid", "The document body is too large.")

        blocks: list[dict] = []
        image_refs: list[tuple[str, int]] = []  # (relationship id, index of the NEXT block)
        state: dict = {
            "para_text": [],
            "style": None,
            "in_para": False,
            "in_t": False,
            "numbered": False,
            "table_depth": 0,
            "rows": [],
            "row": None,
            "cell": None,
        }

        def flush_paragraph() -> None:
            text = "".join(state["para_text"]).strip()
            state["para_text"] = []
            if state["cell"] is not None:  # inside a table: paragraphs join into the cell
                if text:
                    state["cell"].append(text)
                return
            if not text:
                return
            budget.add_chars(len(text))
            level = styles.get(state["style"] or "")
            if level:
                blocks.append({"t": "heading", "level": level, "text": text})
            elif state["numbered"]:
                blocks.append({"t": "list", "text": text})
            else:
                blocks.append({"t": "para", "text": text})

        def start(tag, attrs):
            if tag == f"{W_NS}|p":
                state.update(in_para=True, style=None, numbered=False, para_text=[])
            elif tag == f"{W_NS}|pStyle" and state["in_para"]:
                state["style"] = attrs.get(f"{W_NS}|val")
            elif tag == f"{W_NS}|numPr" and state["in_para"]:
                state["numbered"] = True
            elif tag == f"{W_NS}|t":
                state["in_t"] = True
            elif tag == f"{W_NS}|tab" and state["in_para"]:
                state["para_text"].append("\t")
            elif tag in (f"{W_NS}|br", f"{W_NS}|cr") and state["in_para"]:
                state["para_text"].append("\n")
            elif tag == f"{W_NS}|tbl":
                state["table_depth"] += 1
                if state["table_depth"] == 1:
                    state["rows"] = []
            elif tag == f"{W_NS}|tr" and state["table_depth"] == 1:
                state["row"] = []
            elif tag == f"{W_NS}|tc" and state["table_depth"] == 1:
                state["cell"] = []
            elif tag == f"{A_NS}|blip":
                rid = attrs.get(f"{R_NS}|embed")
                if rid:
                    image_refs.append((rid, len(blocks)))

        def end(tag):
            if tag == f"{W_NS}|t":
                state["in_t"] = False
            elif tag == f"{W_NS}|p":
                flush_paragraph()
                state["in_para"] = False
            elif tag == f"{W_NS}|tc" and state["table_depth"] == 1:
                if state["row"] is not None:
                    state["row"].append(" ".join(state["cell"] or []))
                state["cell"] = None
            elif tag == f"{W_NS}|tr" and state["table_depth"] == 1:
                if state["row"] is not None and any(c.strip() for c in state["row"]):
                    state["rows"].append(state["row"])
                state["row"] = None
            elif tag == f"{W_NS}|tbl":
                if state["table_depth"] == 1 and state["rows"]:
                    for row in state["rows"]:
                        budget.add_chars(sum(len(c) for c in row))
                    blocks.append({"t": "table", "rows": state["rows"]})
                    state["rows"] = []
                state["table_depth"] = max(0, state["table_depth"] - 1)

        def data(text):
            if state["in_t"] and state["in_para"]:
                state["para_text"].append(text)

        parser = _expat(StartElementHandler=start, EndElementHandler=end, CharacterDataHandler=data)
        try:
            with zf.open(body) as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        except expat.ExpatError:
            raise Reject("docx_invalid", "The document body is not valid XML.") from None

        # ---- images, in the order they appear -------------------------------------------------
        candidates: list[dict] = []
        seen_targets: set[str] = set()
        for rid, after in image_refs:
            target = rels.get(rid)
            if not target or target in seen_targets:
                continue
            seen_targets.add(target)
            name = os.path.normpath(os.path.join("word", target)).replace("\\", "/")
            if not name.startswith("word/media/"):
                continue
            blob = _read_part(zf, name, EMBEDDED_IMAGE_MAX_BYTES)
            if blob is None:
                continue
            pil = open_image(blob)
            if pil is not None:
                candidates.append(
                    {"pil": pil, "page": None, "after_block": after, "origin": "docx"}
                )
        figures = select_figures(candidates, limits, workdir, budget)
    return {
        "kind": "docx",
        "blocks": blocks,
        "figures": figures["kept"],
        "figures_found": figures["found"],
        "stats": {"blocks": len(blocks)},
    }


# ---- plain text and Markdown ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.+)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def extract_text(path: str, kind: str, limits: dict, budget: Budget) -> dict:
    with open(path, "rb") as handle:
        data = handle.read(int(limits["max_text_bytes"]) + 1)
    if len(data) > int(limits["max_text_bytes"]):
        raise Reject("text_invalid", "The text file is too large.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Reject("text_invalid", "Text files must be UTF-8.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    budget.add_chars(len(text))
    blocks: list[dict] = []
    lines = text.split("\n")
    para: list[str] = []
    fence = False
    i = 0

    def flush() -> None:
        joined = "\n".join(para).strip()
        para.clear()
        if joined:
            blocks.append({"t": "para", "text": joined})

    while i < len(lines):
        line = lines[i]
        if kind == "md" and line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            para.append(line)
            i += 1
            continue
        if fence:
            para.append(line)
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        if kind == "md":
            m = _HEADING.match(line)
            if m:
                flush()
                blocks.append({"t": "heading", "level": len(m.group(1)), "text": m.group(2)})
                i += 1
                continue
            if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
                flush()
                rows = [_split_row(line)]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(_split_row(lines[i]))
                    i += 1
                blocks.append({"t": "table", "rows": rows})
                continue
            m = _LIST.match(line)
            if m and not para:
                blocks.append({"t": "list", "text": m.group(1).strip()})
                i += 1
                continue
        para.append(line)
        i += 1
    flush()
    return {
        "kind": kind,
        "blocks": blocks,
        "figures": [],
        "figures_found": 0,
        "stats": {"blocks": len(blocks)},
    }


# ---- entry point ---------------------------------------------------------------------------------------


def run(request: dict) -> dict:
    limits = request["limits"]
    applied = apply_limits(limits)
    block_network()
    workdir = os.getcwd()
    budget = Budget(int(limits["max_extracted_chars"]))
    kind = request["kind"]
    path = request["path"]
    try:
        if kind == "pdf":
            result = extract_pdf(path, limits, workdir, budget)
        elif kind == "docx":
            result = extract_docx(path, limits, workdir, budget)
        elif kind in ("txt", "md"):
            result = extract_text(path, kind, limits, budget)
        else:
            raise Reject("unsupported_media_type", "Unknown file kind.")
    except Reject as rejected:
        return {"ok": False, "code": rejected.code, "message": rejected.message, "rlimits": applied}
    result["chars"] = budget.chars
    with open(os.path.join(workdir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return {"ok": True, "rlimits": applied}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(262144))
        outcome = run(request)
    except MemoryError:
        outcome = {"ok": False, "code": "extraction_failed", "message": "Out of memory."}
    except Exception:  # noqa: BLE001 - any failure is "could not extract"; details stay out of output
        outcome = {
            "ok": False,
            "code": "extraction_failed",
            "message": "Could not extract the file.",
        }
    sys.stdout.write(json.dumps(outcome) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
