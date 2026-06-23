"""Unit tests for :mod:`bot.images` — the pure, framework-free image helpers.

These exercise the magic-byte format sniffer, the pdflatex-compatibility check, the
extension map, and the deterministic on-disk filename builder. They need **no** Discord
runtime and **no** Pillow install (the downscale path lives in the async
``download_and_place`` and is not exercised here). The async path's
guarded-fallback behaviour is covered by the implementation's own end-to-end checks;
here we pin the pure contract that everything else depends on.

Import strategy mirrors the other test modules: insert the repo root on ``sys.path`` so
``import bot.images`` resolves regardless of the invoking cwd / pytest rootdir.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from bot.images import (  # noqa: E402
    PDFLATEX_OK,
    detect_format,
    ext_for_format,
    is_pdflatex_compatible,
    target_filename,
)

# --- representative magic-byte prefixes ------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF87 = b"GIF87a" + b"\x00" * 16
GIF89 = b"GIF89a" + b"\x00" * 16
# RIFF <4-byte little-endian size> WEBP ...
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 8
PDF = b"%PDF-1.7\n" + b"\x00" * 8


def _heic(brand: bytes) -> bytes:
    """An ISO-BMFF header: 4-byte box size, 'ftyp', then the major brand."""
    return b"\x00\x00\x00\x18" + b"ftyp" + brand + b"\x00" * 16


class TestDetectFormat:
    def test_png(self):
        assert detect_format(PNG) == "png"

    def test_jpeg(self):
        assert detect_format(JPEG) == "jpeg"

    @pytest.mark.parametrize("data", [GIF87, GIF89])
    def test_gif(self, data):
        assert detect_format(data) == "gif"

    def test_webp(self):
        assert detect_format(WEBP) == "webp"

    def test_pdf(self):
        assert detect_format(PDF) == "pdf"

    @pytest.mark.parametrize(
        "brand",
        [b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevx", b"mif1", b"msf1"],
    )
    def test_heic_brands(self, brand):
        assert detect_format(_heic(brand)) == "heic"

    def test_heic_unknown_brand_is_not_heic(self):
        # A valid ftyp box but a non-HEIF brand (e.g. mp4) must not be mislabelled.
        assert detect_format(_heic(b"mp42")) is None

    def test_riff_without_webp_is_not_webp(self):
        # RIFF/WAVE (audio) shares the RIFF container but is not WEBP.
        assert detect_format(b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE") is None

    @pytest.mark.parametrize("data", [b"", b"\x00", b"not an image", b"\x89PN"])
    def test_unknown_and_short_inputs(self, data):
        # Short/empty/garbage inputs return None rather than raising.
        assert detect_format(data) is None


class TestPdflatexCompat:
    @pytest.mark.parametrize("fmt", ["png", "jpeg", "pdf"])
    def test_supported(self, fmt):
        assert is_pdflatex_compatible(fmt) is True

    @pytest.mark.parametrize("fmt", ["gif", "webp", "heic", "tiff", None])
    def test_unsupported(self, fmt):
        assert is_pdflatex_compatible(fmt) is False

    def test_pdflatex_ok_set(self):
        assert PDFLATEX_OK == frozenset({"png", "jpeg", "pdf"})


class TestExtForFormat:
    @pytest.mark.parametrize(
        "fmt", ["png", "jpeg", "pdf", "gif", "webp", "heic"]
    )
    def test_identity_mapping(self, fmt):
        assert ext_for_format(fmt) == fmt

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            ext_for_format("tiff")


class TestTargetFilename:
    def test_labelled_single_letter(self):
        assert target_filename(1, "a", 1, "png") == "aufgabe1_a_1.png"

    def test_labelled_combined(self):
        # "a b" -> fragment "ab"
        assert target_filename(1, "a b", 1, "jpeg") == "aufgabe1_ab_1.jpeg"

    def test_unlabelled_whole_exercise(self):
        # Empty label -> no fragment segment.
        assert target_filename(2, "", 1, "png") == "aufgabe2_1.png"

    def test_page_index_used(self):
        assert target_filename(3, "b", 2, "png") == "aufgabe3_b_2.png"

    def test_exercise_index_used(self):
        assert target_filename(10, "", 1, "pdf") == "aufgabe10_1.pdf"
