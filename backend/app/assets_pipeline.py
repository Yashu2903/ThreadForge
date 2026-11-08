"""Image processing shared by editor uploads and DOCX imports.

What goes in: a file on disk that a user (or a document) supplied. What comes out: a CLEAN image
that WE produced, or a rejection. The uploaded bytes are never stored or served: the stored file is
the re-encoded output, so EXIF (including GPS), ICC profiles, text chunks, comments and anything
appended after the image data are gone, and a polyglot (a file that is a valid image AND something
else) cannot survive re-encoding.

Steps (each is a rejection point, with a stable code):
  1. the real signature must be PNG, JPEG or WebP (`unsupported_media_type`): SVG, GIF, BMP, ... no;
  2. the signature must agree with the declared Content-Type (`type_mismatch`);
  3. Pillow opens it restricted to that one format (`formats=[...]`), reading only the header: width
     x height is checked BEFORE any pixel is decoded (`too_many_pixels`, `dimensions_too_large`);
  4. animated images (animated WebP, APNG) are rejected (`animated`): "no animation";
  5. decode (Pillow's decompression-bomb guard is also armed), apply EXIF orientation, convert to
     a plain RGB/RGBA image, and encode it again in the same format with no metadata.

Pillow decoding is blocking CPU work: callers run process_image() in a worker thread, and the API
bounds how many run at once.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

PNG = b"\x89PNG\r\n\x1a\n"
MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_DIMENSION_PX = 10_000  # matches the document schema's image width/height bound


class ImageRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProcessedImage:
    path: Path  # the re-encoded file (written next to the source)
    media_type: str
    width: int
    height: int
    byte_size: int
    sha256: bytes  # of the stored (re-encoded) bytes


def sniff(head: bytes) -> str | None:
    """The media type implied by the first bytes, or None."""
    if head.startswith(PNG):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def process_image(source: Path, declared_type: str | None, max_pixels: int) -> ProcessedImage:
    with source.open("rb") as handle:
        head = handle.read(16)
    real = sniff(head)
    if real is None:
        raise ImageRejected(
            "unsupported_media_type", "Only PNG, JPEG and WebP images are accepted."
        )
    if declared_type is not None and declared_type != real:
        raise ImageRejected("type_mismatch", "The file contents do not match the declared type.")
    fmt = {v: k for k, v in MEDIA_TYPES.items()}[real]

    # Pillow's guard (Image.MAX_IMAGE_PIXELS, set once at startup by configure_pillow) is a second
    # line of defence behind the explicit header check below.
    try:
        with Image.open(source, formats=[fmt]) as image:
            width, height = image.size  # header only: nothing decoded yet
            if width < 1 or height < 1:
                raise ImageRejected("invalid_image", "The image is empty.")
            if width * height > max_pixels:
                raise ImageRejected(
                    "too_many_pixels", f"Images are limited to {max_pixels:,} pixels."
                )
            if width > MAX_DIMENSION_PX or height > MAX_DIMENSION_PX:
                raise ImageRejected(
                    "dimensions_too_large",
                    f"Images can be at most {MAX_DIMENSION_PX} pixels wide or tall.",
                )
            if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                raise ImageRejected("animated", "Animated images are not supported.")
            image.load()
            oriented = ImageOps.exif_transpose(
                image
            )  # a new image; orientation applied and removed
            return _encode(oriented, fmt, real, source)
    except ImageRejected:
        raise
    except Image.DecompressionBombError:
        raise ImageRejected(
            "too_many_pixels", f"Images are limited to {max_pixels:,} pixels."
        ) from None
    except Exception as exc:  # noqa: BLE001 - any decoder failure just means "not a usable image"
        raise ImageRejected("invalid_image", "The file is not a valid image.") from exc


def configure_pillow(max_pixels: int) -> None:
    """Arm Pillow's decompression-bomb guard (error above 2x this, warning above 1x)."""
    Image.MAX_IMAGE_PIXELS = max_pixels


def _encode(image: Image.Image, fmt: str, media_type: str, source: Path) -> ProcessedImage:
    width, height = image.size
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    clean = image.convert("RGBA" if has_alpha and fmt != "JPEG" else "RGB")
    clean.info.clear()  # nothing from the original (ICC profile, comments, text chunks) survives
    out = source.with_suffix(".clean")
    if fmt == "PNG":
        clean.save(out, "PNG", optimize=True)
    elif fmt == "JPEG":
        clean.save(out, "JPEG", quality=90, optimize=True, progressive=False)
    else:
        clean.save(out, "WEBP", quality=90, method=4, exif=b"", icc_profile=None)
    data = out.read_bytes()
    return ProcessedImage(
        path=out,
        media_type=media_type,
        width=width,
        height=height,
        byte_size=len(data),
        sha256=hashlib.sha256(data).digest(),
    )
