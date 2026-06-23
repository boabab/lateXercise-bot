"""Unit tests for :func:`bot.ui.merge_consecutive_parts`.

These exercise the pure normalization step that folds *consecutive* exercise parts
sharing the identical image(s) into a single combined-label part, so a photo covering
parts a, b, c is shown once as ``(a) (b) (c)`` rather than repeated per part — while a
photo that reappears after an interruption is still repeated.

Import strategy mirrors :mod:`tests.test_pickspec`: ``bot.ui`` imports ``discord`` at
module top level (the widget classes subclass ``discord.ui``), so if discord.py is
absent we ``pytest.skip`` the whole module rather than erroring. The repo root is
prepended to ``sys.path`` so ``import bot.ui`` resolves from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --- make ``import bot.ui`` resolve from the repo root -----------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from bot.ui import ParsedPart, merge_consecutive_parts, parse_pick_spec, preview_text
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without discord
    if exc.name in {"discord", "discord.ui"}:
        pytest.skip(
            "discord.py not installed; skipping bot.ui merge tests",
            allow_module_level=True,
        )
    raise


# ---------------------------------------------------------------------------
# Core folding behaviour
# ---------------------------------------------------------------------------


def test_three_consecutive_same_image_merge_into_one() -> None:
    """a:2 / b:2 / c:2 → a single combined part ``a b c`` carrying image 2 once."""
    result = merge_consecutive_parts(
        [
            ParsedPart(label="a", numbers=[2]),
            ParsedPart(label="b", numbers=[2]),
            ParsedPart(label="c", numbers=[2]),
        ]
    )
    assert result == [ParsedPart(label="a b c", numbers=[2])]


def test_interrupted_run_repeats_the_image() -> None:
    """a:2 / b:2 / c:7 / d:2 → ``a b`` keep 2; c keeps 7; d repeats 2 (interrupted)."""
    result = merge_consecutive_parts(
        [
            ParsedPart(label="a", numbers=[2]),
            ParsedPart(label="b", numbers=[2]),
            ParsedPart(label="c", numbers=[7]),
            ParsedPart(label="d", numbers=[2]),
        ]
    )
    assert result == [
        ParsedPart(label="a b", numbers=[2]),
        ParsedPart(label="c", numbers=[7]),
        ParsedPart(label="d", numbers=[2]),
    ]


def test_non_consecutive_same_image_not_merged() -> None:
    """a:2 / b:7 / c:2 — the two 2s are not adjacent, so nothing merges."""
    parts = [
        ParsedPart(label="a", numbers=[2]),
        ParsedPart(label="b", numbers=[7]),
        ParsedPart(label="c", numbers=[2]),
    ]
    assert merge_consecutive_parts(parts) == parts


def test_already_combined_label_absorbs_following_same_image() -> None:
    """An explicit ``a b: 2`` followed by ``c: 2`` folds into ``a b c``."""
    result = merge_consecutive_parts(
        [
            ParsedPart(label="a b", numbers=[2]),
            ParsedPart(label="c", numbers=[2]),
        ]
    )
    assert result == [ParsedPart(label="a b c", numbers=[2])]


# ---------------------------------------------------------------------------
# Boundaries of "same image(s)"
# ---------------------------------------------------------------------------


def test_multi_page_identical_sequences_merge() -> None:
    """Parts with the identical multi-page sequence [2, 3] merge (order matches)."""
    result = merge_consecutive_parts(
        [
            ParsedPart(label="a", numbers=[2, 3]),
            ParsedPart(label="b", numbers=[2, 3]),
        ]
    )
    assert result == [ParsedPart(label="a b", numbers=[2, 3])]


def test_different_page_order_does_not_merge() -> None:
    """Same images in a different page order ([2,3] vs [3,2]) stay separate."""
    parts = [
        ParsedPart(label="a", numbers=[2, 3]),
        ParsedPart(label="b", numbers=[3, 2]),
    ]
    assert merge_consecutive_parts(parts) == parts


def test_different_image_count_does_not_merge() -> None:
    """A superset of pages ([2] vs [2,3]) is a different image set; no merge."""
    parts = [
        ParsedPart(label="a", numbers=[2]),
        ParsedPart(label="b", numbers=[2, 3]),
    ]
    assert merge_consecutive_parts(parts) == parts


# ---------------------------------------------------------------------------
# Whole-exercise (empty-label) parts never fold
# ---------------------------------------------------------------------------


def test_empty_label_is_not_folded_into_a_labelled_neighbour() -> None:
    """A whole-exercise part ('') is never merged, even with a matching image."""
    parts = [
        ParsedPart(label="", numbers=[2]),
        ParsedPart(label="a", numbers=[2]),
    ]
    assert merge_consecutive_parts(parts) == parts


def test_labelled_part_is_not_folded_into_empty_neighbour() -> None:
    """Symmetric guard: a labelled part is not absorbed by a preceding ''-label part."""
    parts = [
        ParsedPart(label="a", numbers=[2]),
        ParsedPart(label="", numbers=[2]),
    ]
    assert merge_consecutive_parts(parts) == parts


# ---------------------------------------------------------------------------
# Degenerate / passthrough inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    """An empty part list folds to an empty list."""
    assert merge_consecutive_parts([]) == []


def test_single_part_passes_through() -> None:
    """A lone part is returned unchanged (as an equal value)."""
    assert merge_consecutive_parts([ParsedPart(label="a", numbers=[2])]) == [
        ParsedPart(label="a", numbers=[2])
    ]


def test_all_distinct_images_pass_through_unchanged() -> None:
    """Distinct images on every part means no folding at all."""
    parts = [
        ParsedPart(label="a", numbers=[1]),
        ParsedPart(label="b", numbers=[2]),
        ParsedPart(label="c", numbers=[3]),
    ]
    assert merge_consecutive_parts(parts) == parts


# ---------------------------------------------------------------------------
# Purity: inputs are not mutated and outputs do not alias the inputs
# ---------------------------------------------------------------------------


def test_does_not_mutate_input_parts() -> None:
    """The input parts and their ``numbers`` lists are left untouched."""
    a = ParsedPart(label="a", numbers=[2])
    b = ParsedPart(label="b", numbers=[2])
    merge_consecutive_parts([a, b])
    assert a == ParsedPart(label="a", numbers=[2])
    assert b == ParsedPart(label="b", numbers=[2])


def test_output_numbers_do_not_alias_input() -> None:
    """A merged part owns a fresh ``numbers`` list (mutating it can't touch the input)."""
    a = ParsedPart(label="a", numbers=[2])
    b = ParsedPart(label="b", numbers=[2])
    (merged,) = merge_consecutive_parts([a, b])
    merged.numbers.append(99)
    assert a.numbers == [2]
    assert b.numbers == [2]


# ---------------------------------------------------------------------------
# End-to-end: parse → merge → preview reflects the combined label
# ---------------------------------------------------------------------------


def test_parse_then_merge_then_preview_shows_combined_label() -> None:
    """The user-visible preview after merging shows ``(a) (b) (c)`` for a shared photo."""
    parsed = parse_pick_spec("a: 2\nb: 2\nc: 2", num_candidates=7)
    merged = merge_consecutive_parts(parsed)
    assert preview_text(merged) == "Part (a) (b) (c): #2"


def test_parse_then_merge_preserves_interruption_in_preview() -> None:
    """An interrupted reappearance still shows the image twice in the preview."""
    parsed = parse_pick_spec("a: 2\nb: 2\nc: 7\nd: 2", num_candidates=7)
    merged = merge_consecutive_parts(parsed)
    assert preview_text(merged) == (
        "Part (a) (b): #2 · Part (c): #7 · Part (d): #2"
    )
