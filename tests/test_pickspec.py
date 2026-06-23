"""Unit tests for :func:`bot.ui.parse_pick_spec` (the pure pick-spec parser).

These tests exercise *only* the framework-free half of :mod:`bot.ui`: the
``parse_pick_spec`` function together with its data shapes (``ParsedPart``) and
its error type (``PickSpecError``). No Discord runtime is required to run them.

Import strategy
---------------
``bot.ui`` imports ``discord`` unconditionally at module top level (the widget
classes subclass ``discord.ui``), so importing the module pulls in discord.py.
discord.py *is* a project dependency, so in the normal environment the plain
``from bot.ui import ...`` below simply works.

To keep these parser tests robust even when discord.py happens to be missing
(e.g. a stripped-down CI runner), we attempt the import and, if it fails *only*
because ``discord`` is unavailable, ``pytest.skip`` the whole module rather than
erroring out. We deliberately do **not** re-implement or vendor the parser here:
the point of the test is to pin the real ``bot.ui`` behaviour.

``sys.path`` is prepended with the repository root so ``import bot.ui`` resolves
when pytest is invoked from anywhere (the repo root, ``tests/``, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --- make ``import bot.ui`` resolve from the repo root -----------------------
# tests/test_pickspec.py -> parents[1] is the repository root containing ``bot/``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from bot.ui import ParsedPart, PickSpecError, parse_pick_spec
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without discord
    # Only tolerate the failure when it is discord.py that is missing; any other
    # ModuleNotFoundError is a real problem and should surface.
    if exc.name in {"discord", "discord.ui"}:
        pytest.skip(
            "discord.py not installed; skipping bot.ui parser tests",
            allow_module_level=True,
        )
    raise


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_multiline_happy_path() -> None:
    """The canonical multi-line spec parses into one ParsedPart per line, in order."""
    result = parse_pick_spec("a: 2\nb: 5, 6\nc: 7", num_candidates=7)
    assert result == [
        ParsedPart(label="a", numbers=[2]),
        ParsedPart(label="b", numbers=[5, 6]),
        ParsedPart(label="c", numbers=[7]),
    ]


def test_returns_parsedpart_instances() -> None:
    """Returned items are ParsedPart dataclass instances with the documented fields."""
    (part,) = parse_pick_spec("a: 1", num_candidates=3)
    assert isinstance(part, ParsedPart)
    assert part.label == "a"
    assert part.numbers == [1]


def test_whole_exercise_blank_label() -> None:
    """A blank label (``: 2``) denotes the whole exercise and canonicalizes to ''."""
    result = parse_pick_spec(": 2", num_candidates=5)
    assert result == [ParsedPart(label="", numbers=[2])]


def test_number_order_is_preserved() -> None:
    """Numbers within a part keep the user-given order (page order), not sorted."""
    (part,) = parse_pick_spec("b: 6, 5", num_candidates=6)
    assert part.numbers == [6, 5]


def test_space_separated_numbers() -> None:
    """Numbers may be separated by whitespace as well as commas."""
    (part,) = parse_pick_spec("a: 1 2 3", num_candidates=5)
    assert part.numbers == [1, 2, 3]


def test_blank_lines_between_parts_are_ignored() -> None:
    """Blank lines between parts are skipped, not treated as empty parts."""
    result = parse_pick_spec("a: 2\n\nb: 5, 6\n", num_candidates=6)
    assert result == [
        ParsedPart(label="a", numbers=[2]),
        ParsedPart(label="b", numbers=[5, 6]),
    ]


# ---------------------------------------------------------------------------
# Combined-label canonicalization: 'a) b)', 'a+b', 'a b' all -> 'a b'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_label",
    [
        "a) b)",  # combined photo written with closing parens
        "a+b",  # combined photo written with a plus
        "a b",  # combined photo written with a space
        "a, b",  # combined photo written with a comma
        "(a) (b)",  # combined photo with full parens
    ],
)
def test_combined_label_canonicalizes_to_a_b(raw_label: str) -> None:
    """Different ways of writing a combined a+b part all canonicalize to 'a b'."""
    result = parse_pick_spec(f"{raw_label}: 3", num_candidates=7)
    assert result == [ParsedPart(label="a b", numbers=[3])]


def test_label_is_lowercased() -> None:
    """Labels are lowercased during canonicalization."""
    (part,) = parse_pick_spec("A: 1", num_candidates=3)
    assert part.label == "a"


def test_single_label_with_parens_strips_to_token() -> None:
    """A single parenthesised label like ``(a)`` reduces to the bare token 'a'."""
    (part,) = parse_pick_spec("(a): 1", num_candidates=3)
    assert part.label == "a"


# ---------------------------------------------------------------------------
# A number may be repeated across parts (one photo covering several parts)
# ---------------------------------------------------------------------------


def test_number_repeated_across_parts_is_allowed() -> None:
    """The same candidate number may appear in multiple parts (shared photo)."""
    result = parse_pick_spec("a: 3\nb: 3", num_candidates=7)
    assert result == [
        ParsedPart(label="a", numbers=[3]),
        ParsedPart(label="b", numbers=[3]),
    ]


def test_number_repeated_within_a_part_is_allowed() -> None:
    """Repeating a number within one part is preserved as given (no de-duplication)."""
    (part,) = parse_pick_spec("a: 2, 2", num_candidates=5)
    assert part.numbers == [2, 2]


# ---------------------------------------------------------------------------
# Validation errors — all raise PickSpecError
# ---------------------------------------------------------------------------


def test_pickspecerror_is_valueerror_subclass() -> None:
    """PickSpecError is a ValueError subclass, per the contract."""
    assert issubclass(PickSpecError, ValueError)


def test_missing_colon_raises() -> None:
    """A line without a ':' is invalid."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a 2", num_candidates=7)


def test_missing_colon_on_second_line_raises() -> None:
    """The colon requirement applies to every non-blank line, not just the first."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: 2\nb 3", num_candidates=7)


def test_empty_part_label_but_no_numbers_raises() -> None:
    """A labelled part with no numbers after the colon is an empty part -> error."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a:", num_candidates=7)


def test_empty_part_blank_label_no_numbers_raises() -> None:
    """A bare ':' with nothing after it is also an empty part -> error."""
    with pytest.raises(PickSpecError):
        parse_pick_spec(":", num_candidates=7)


def test_out_of_range_zero_raises() -> None:
    """Candidate numbers are 1-based; 0 is out of range."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: 0", num_candidates=7)


def test_out_of_range_above_max_raises() -> None:
    """A number greater than num_candidates is out of range."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: 8", num_candidates=7)


def test_out_of_range_message_reports_range() -> None:
    """The out-of-range error message names the offending value and the valid range."""
    with pytest.raises(PickSpecError) as excinfo:
        parse_pick_spec("a: 9", num_candidates=7)
    message = str(excinfo.value)
    assert "9" in message
    assert "7" in message  # the upper bound of the valid 1..7 range


def test_non_integer_token_raises() -> None:
    """A non-integer number token (e.g. 'x') is invalid."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: x", num_candidates=7)


def test_float_token_raises() -> None:
    """A float-looking token (e.g. '1.5') is not a valid integer candidate number."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: 1.5", num_candidates=7)


def test_empty_input_raises() -> None:
    """Empty input means no parts at all -> error."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("", num_candidates=7)


def test_whitespace_only_input_raises() -> None:
    """Whitespace-only input (only blank lines) yields no parts -> error."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("   \n  \n", num_candidates=7)


def test_any_number_invalid_when_no_candidates() -> None:
    """With zero candidates, every referenced number is out of range."""
    with pytest.raises(PickSpecError):
        parse_pick_spec("a: 1", num_candidates=0)
