"""Image handling for the lateXercise bot.

This module turns a Discord message attachment into a file on disk that
``pdflatex`` can actually ``\\includegraphics``. It is split into two layers:

* **Pure helpers** — format detection by magic bytes, pdflatex-compatibility
  checks, extension/filename derivation. These have no Discord and no Pillow
  dependency at import time, so they are trivially unit-testable.
* **One async I/O function** — :func:`download_and_place`, which reads the
  attachment bytes, validates the format, optionally downscales via Pillow,
  writes the file, and returns a :class:`SavedImage`.

Design constraints (see ``data/CONTRACTS.md``):

* ``discord`` is **never** imported at module top level. The attachment passed
  to :func:`download_and_place` is duck-typed (it only needs an awaitable
  ``read()`` method and ``filename`` / ``content_type`` attributes), which keeps
  the rest of the module importable and testable without a Discord runtime.
* Pillow is an **optional** dependency. The downscale path is guarded behind a
  late import inside a ``try``/``except`` so that a missing Pillow install, or a
  single corrupt image, can never crash ``/build`` — we simply fall back to
  writing the raw downloaded bytes.
* Magic-byte sniffing (not the attachment's claimed ``content_type`` or file
  extension) is the source of truth for the format, because phone uploads
  routinely mislabel HEIC as ``.jpg`` and similar.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from .latex import label_to_filename_fragment

__all__ = [
    "detect_format",
    "PDFLATEX_OK",
    "is_pdflatex_compatible",
    "ext_for_format",
    "target_filename",
    "SavedImage",
    "UnsupportedImageError",
    "download_and_place",
]

logger = logging.getLogger(__name__)

# ISO Base Media File Format "major brands" that identify a HEIF/HEIC still
# image (or sequence). The brand lives in bytes 8..12, immediately after the
# 4-byte box size and the literal "ftyp" box type at bytes 4..8.
_HEIC_BRANDS: frozenset[bytes] = frozenset(
    {
        b"heic",  # single image, HEVC
        b"heix",  # single image, HEVC (10-bit / extended)
        b"hevc",  # image sequence, HEVC
        b"heim",  # multi-image
        b"heis",  # scalable
        b"hevx",  # image sequence (extended)
        b"mif1",  # generic HEIF image
        b"msf1",  # generic HEIF image sequence
    }
)


def detect_format(data: bytes) -> str | None:
    """Sniff the image/document format of ``data`` from its leading magic bytes.

    Returns one of ``{"png", "jpeg", "gif", "webp", "heic", "pdf"}`` or
    ``None`` when the bytes match no known signature.

    Signatures used (offsets are 0-based, half-open slices):

    * **PNG**  — first 8 bytes ``89 50 4E 47 0D 0A 1A 0A`` (``\\x89PNG\\r\\n\\x1a\\n``).
    * **JPEG** — first 3 bytes ``FF D8 FF``.
    * **GIF**  — first 4 bytes ``GIF8`` (covers both ``GIF87a`` and ``GIF89a``).
    * **WEBP** — ``bytes[0:4] == b"RIFF"`` *and* ``bytes[8:12] == b"WEBP"``
      (RIFF container with a WEBP form-type).
    * **HEIC/HEIF** — ``bytes[4:8] == b"ftyp"`` *and* ``bytes[8:12]`` is one of
      the HEIF major brands in :data:`_HEIC_BRANDS`.
    * **PDF**  — first 4 bytes ``%PDF``.

    The function is defensive about short inputs: a slice past the end of a
    ``bytes`` object simply yields fewer bytes, and the equality checks below
    fail rather than raising.
    """
    # PNG: full 8-byte signature, but the leading "\x89PNG" is the load-bearing
    # part; the contract specifies "\x89PNG", so match on that prefix.
    if data[:4] == b"\x89PNG":
        return "png"
    # JPEG: SOI marker followed by a marker start byte.
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    # GIF: "GIF8" covers GIF87a and GIF89a.
    if data[:4] == b"GIF8":
        return "gif"
    # WEBP: RIFF container whose form-type is "WEBP" (bytes 8..12).
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    # HEIC/HEIF: "ftyp" box (bytes 4..8) with a HEIF major brand (bytes 8..12).
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    # PDF: "%PDF" header.
    if data[:4] == b"%PDF":
        return "pdf"
    return None


# Formats ``pdflatex`` can embed directly via ``\includegraphics``. Everything
# else (gif, webp, heic) must be reported to the user rather than placed.
PDFLATEX_OK: frozenset[str] = frozenset({"png", "jpeg", "pdf"})


def is_pdflatex_compatible(fmt: str | None) -> bool:
    """Return ``True`` iff ``fmt`` is a format ``pdflatex`` can embed directly.

    ``None`` (unknown format) is treated as incompatible.
    """
    return fmt in PDFLATEX_OK


def ext_for_format(fmt: str) -> str:
    """Map a detected format to the file extension used when saving.

    The mapping is the identity for every supported format
    (``png``/``jpeg``/``pdf``/``gif``/``webp``/``heic``); it exists so callers
    have a single, named choke point and so the saved-on-disk extension always
    reflects the *detected* format rather than the original (often wrong)
    upload name.

    Raises:
        KeyError: if ``fmt`` is not a recognised format.
    """
    mapping = {
        "png": "png",
        "jpeg": "jpeg",
        "pdf": "pdf",
        "gif": "gif",
        "webp": "webp",
        "heic": "heic",
    }
    return mapping[fmt]


def target_filename(exercise_index: int, label: str, page: int, ext: str) -> str:
    """Build the deterministic on-disk filename for one placed image.

    The original upload name (e.g. ``IMG_2026 (1).JPG``) is discarded entirely
    in favour of an ASCII-safe, ``\\includegraphics``-friendly name derived from
    the exercise number, the part label, and the 1-based page index.

    The label is sanitised via :func:`bot.latex.label_to_filename_fragment`
    (``"a"`` -> ``"a"``, ``"a b"`` -> ``"ab"``, ``""`` -> ``""``):

    * **labelled** part  ->  ``aufgabe{N}_{frag}_{page}.{ext}``
      e.g. ``aufgabe1_a_1.png``, ``aufgabe1_ab_1.jpeg``.
    * **unlabelled** part (empty fragment)  ->  ``aufgabe{N}_{page}.{ext}``
      e.g. ``aufgabe1_1.png``.

    Args:
        exercise_index: 1-based Aufgabe number.
        label: canonical raw label (``""``, ``"a"``, ``"a b"``).
        page: 1-based page order within the part.
        ext: file extension without a leading dot (typically from
            :func:`ext_for_format`).
    """
    frag = label_to_filename_fragment(label)
    if frag:
        return f"aufgabe{exercise_index}_{frag}_{page}.{ext}"
    return f"aufgabe{exercise_index}_{page}.{ext}"


@dataclass
class SavedImage:
    """Result of placing one attachment on disk.

    Attributes:
        path: absolute path of the file actually written.
        rel_path: POSIX path relative to the LaTeX project root, suitable for
            ``\\includegraphics`` (e.g. ``"ex06/aufgabe1_a_1.png"``).
        fmt: the detected format actually written (one of :data:`PDFLATEX_OK`).
        width: pixel width if known (Pillow inspected the image), else ``None``.
        height: pixel height if known, else ``None``.
    """

    path: Path
    rel_path: str
    fmt: str
    width: int | None
    height: int | None


class UnsupportedImageError(Exception):
    """Raised when an attachment's format cannot be used by ``pdflatex``.

    In v1 the bot does not attempt HEIC/webp/gif conversion, so it surfaces a
    clear, user-facing message naming the offending filename and the detected
    format together with guidance on how to fix it (re-upload as PNG/JPEG/PDF).
    """


async def download_and_place(
    *,
    attachment,
    dest_dir: Path,
    rel_dir: str,
    exercise_index: int,
    label: str,
    page: int,
    downscale_max_px: int | None,
) -> SavedImage:
    """Download one Discord attachment and write a pdflatex-ready file on disk.

    Workflow:

    1. ``await attachment.read()`` to fetch the bytes (re-fetched at build time,
       since Discord CDN URLs are signed/expiring).
    2. Detect the true format via :func:`detect_format` (magic bytes, *not* the
       attachment's claimed ``content_type``/extension).
    3. If the format is not pdflatex-compatible, raise
       :class:`UnsupportedImageError` — no conversion is attempted in v1.
    4. Choose the extension (:func:`ext_for_format`) and filename
       (:func:`target_filename`), ensure ``dest_dir`` exists.
    5. If ``downscale_max_px`` is set, Pillow is importable, and the format is
       ``png``/``jpeg``: open the image, apply EXIF orientation, and shrink it
       to fit within ``downscale_max_px`` on the longest side *only if it is
       currently larger* (never upscale). The whole Pillow path is guarded:
       any failure (missing Pillow, decode error, save error) falls back to
       writing the raw downloaded bytes and logs a warning, so a single bad
       image can never crash ``/build``. PDFs are always written raw.
    6. Return a :class:`SavedImage` with a POSIX ``rel_path`` of
       ``f"{rel_dir}/{name}"``.

    Args:
        attachment: a ``discord.Attachment`` (duck-typed: needs an awaitable
            ``read()`` and a ``filename`` attribute; ``content_type`` is read
            only for diagnostics).
        dest_dir: absolute directory to write into (e.g.
            ``<project>/ex06``). Created if missing. The *caller* is
            responsible for wiping it before a build.
        rel_dir: POSIX directory fragment relative to the project root used in
            ``rel_path`` (e.g. ``"ex06"``).
        exercise_index: 1-based Aufgabe number.
        label: canonical raw part label (``""``, ``"a"``, ``"a b"``).
        page: 1-based page order within the part.
        downscale_max_px: longest-side cap in pixels, or ``None``/``<=0`` to
            disable downscaling.

    Returns:
        SavedImage describing the file written.

    Raises:
        UnsupportedImageError: if the detected format is not embeddable by
            pdflatex (gif/webp/heic/unknown).
    """
    # 1. Fetch the raw bytes. Discord's Attachment.read() is a coroutine.
    data: bytes = await attachment.read()

    # 2. Detect the real format from magic bytes (ignore the claimed type).
    fmt = detect_format(data)

    # 3. Reject formats pdflatex cannot embed; v1 does not convert.
    if not is_pdflatex_compatible(fmt):
        filename = getattr(attachment, "filename", "<unknown>")
        detected = fmt if fmt is not None else "unknown"
        raise UnsupportedImageError(
            f"'{filename}' has an unsupported image format ({detected}). "
            "pdflatex can only embed PNG, JPEG, or PDF. "
            "Please re-upload this image as PNG or JPEG (e.g. export/convert it "
            "from HEIC/WEBP first)."
        )

    # ``fmt`` is now guaranteed to be one of {"png", "jpeg", "pdf"}.
    assert fmt is not None  # narrows the type for static checkers

    # 4. Derive extension + filename and ensure the destination exists.
    ext = ext_for_format(fmt)
    name = target_filename(exercise_index, label, page, ext)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / name

    width: int | None = None
    height: int | None = None
    wrote_via_pillow = False

    # 5. Optional Pillow downscale — only for raster formats we can re-encode.
    #    PDFs are always passed through untouched.
    if downscale_max_px and downscale_max_px > 0 and fmt in ("png", "jpeg"):
        try:
            # Guarded, late imports so a missing Pillow never breaks the module.
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(data)) as im:
                # Apply the EXIF orientation tag and bake it in. exif_transpose
                # returns a NEW image (or None for some inputs), so reassign.
                transposed = ImageOps.exif_transpose(im)
                if transposed is not None:
                    im = transposed

                # Only shrink; never enlarge. thumbnail() preserves aspect ratio
                # and is a no-op when the image already fits.
                if max(im.size) > downscale_max_px:
                    im.thumbnail(
                        (downscale_max_px, downscale_max_px),
                        Image.Resampling.LANCZOS,
                    )

                if fmt == "jpeg":
                    # JPEG has no alpha/palette; convert modes that would fail
                    # to encode (RGBA / P / LA) down to plain RGB first.
                    if im.mode in ("RGBA", "P", "LA"):
                        im = im.convert("RGB")
                    im.save(out_path, format="JPEG", quality=90)
                else:  # png
                    im.save(out_path, format="PNG")

                width, height = im.size
                wrote_via_pillow = True
        except Exception:  # noqa: BLE001 — any Pillow failure must be non-fatal
            # Missing Pillow, undecodable image, or a save error: fall back to
            # the raw bytes so the build can still proceed. Log for diagnostics.
            logger.warning(
                "Pillow downscale failed for %s; writing raw bytes instead.",
                getattr(attachment, "filename", name),
                exc_info=True,
            )
            wrote_via_pillow = False

    # 6. Fallback / non-downscale path: write the original bytes verbatim.
    if not wrote_via_pillow:
        out_path.write_bytes(data)

    rel_path = f"{rel_dir}/{name}"  # POSIX-style; rel_dir is already POSIX.
    return SavedImage(
        path=out_path,
        rel_path=rel_path,
        fmt=fmt,
        width=width,
        height=height,
    )
