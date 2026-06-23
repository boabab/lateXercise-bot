"""Unit tests for :mod:`bot.latex` — the pure LaTeX generation / log-parsing layer.

These tests need **no** Discord runtime and **no** LaTeX install: they exercise only
the framework-free helpers (``pad_sheet``, ``label_to_paragraph``,
``label_to_filename_fragment``), the document renderer (``render_tex``), and the log
excerptor (``parse_log``). They deliberately do *not* invoke ``compile_pdf`` /
``resolve_compiler`` so the suite stays dependency-light and runnable on any machine.

Import strategy: ``bot/`` ships no ``__init__.py`` (it is an implicit namespace
package), so we insert the **repository root** onto ``sys.path`` here, at module import
time, and then ``import bot.latex``. Doing it in the test file itself (rather than via a
``pytest.ini``/``conftest.py`` packaging assumption) makes the suite runnable from the
repo root with a bare ``pytest`` invocation regardless of rootdir discovery.

Assertions target *substrings* and *structure* (ordering, presence/absence of markers,
relative positions) rather than brittle whole-string equality, so the tests survive
incidental whitespace/comment changes in the generator while still pinning the contract.
"""

from __future__ import annotations

import os
import sys

# --- Make `import bot.latex` work when invoked from the repo root or anywhere. ------
# tests/ lives directly under the repo root; its parent is the directory that contains
# the `bot/` namespace package. Insert it at the front of sys.path before importing.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot.latex import (  # noqa: E402  (path setup must precede this import)
    ExerciseDoc,
    FigurePart,
    HeaderOverrides,
    label_to_filename_fragment,
    label_to_paragraph,
    pad_sheet,
    parse_log,
    render_tex,
)


# ===========================================================================
# pad_sheet
# ===========================================================================

def test_pad_sheet_zero_pads_single_digit() -> None:
    assert pad_sheet(6) == "06"


def test_pad_sheet_keeps_two_digits() -> None:
    assert pad_sheet(13) == "13"


def test_pad_sheet_boundaries() -> None:
    # The contract restricts sheets to 1..99; both ends pad to exactly two digits.
    assert pad_sheet(1) == "01"
    assert pad_sheet(99) == "99"


# ===========================================================================
# label_to_paragraph
# ===========================================================================

def test_label_to_paragraph_empty_yields_empty() -> None:
    # An empty (whole-exercise) label produces no paragraph text at all; the caller
    # relies on this empty string to decide NOT to emit a \paragraph.
    assert label_to_paragraph("") == ""


def test_label_to_paragraph_empty_whitespace_only() -> None:
    # Whitespace-only labels also split into zero tokens -> empty.
    assert label_to_paragraph("   ") == ""


def test_label_to_paragraph_single_token() -> None:
    assert label_to_paragraph("a") == "(a)"


def test_label_to_paragraph_combined_tokens() -> None:
    # A combined photo covering parts a and b: each token wrapped, space-joined,
    # matching `\paragraph{(a) (b)}` in the hand-made example.tex / ex3.tex.
    assert label_to_paragraph("a b") == "(a) (b)"


# ===========================================================================
# label_to_filename_fragment
# ===========================================================================

def test_label_to_filename_fragment_empty() -> None:
    assert label_to_filename_fragment("") == ""


def test_label_to_filename_fragment_single() -> None:
    assert label_to_filename_fragment("a") == "a"


def test_label_to_filename_fragment_combined_drops_space() -> None:
    # "a b" -> "ab": spaces (and any non-[a-z0-9]) are dropped, lowercased.
    assert label_to_filename_fragment("a b") == "ab"


# ===========================================================================
# render_tex — preamble & sheet number
# ===========================================================================

def test_render_tex_sets_exercise_sheet_zero_padded() -> None:
    # \setExerciseSheet{06} for sheet 6 (zero-padded to two digits).
    tex = render_tex(6, [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/a.png"])])])
    assert "\\setExerciseSheet{06}" in tex
    # And the rest of the fixed preamble the hand-made sheets use.
    assert "\\documentclass[a4paper,11pt]{scrartcl}" in tex
    assert "\\usepackage{exercise}" in tex
    assert "\\exerciseMakeHeaders" in tex
    assert "\\begin{document}" in tex
    assert "\\end{document}" in tex


def test_render_tex_provides_exercise_title_before_loading_package() -> None:
    # exercise.sty does \renewcommand{\exerciseTitle}{...} on an undefined command, so the
    # generated doc must \providecommand it BEFORE \usepackage{exercise} or the package
    # fails to load on a clean TeX install. Assert both presence and ordering.
    tex = render_tex(6, [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/a.png"])])])
    assert "\\providecommand{\\exerciseTitle}{}" in tex
    # Check ordering against the real directives only (comment lines may mention
    # \usepackage{exercise} in prose, which would fool a naive str.index).
    code = "\n".join(ln for ln in tex.splitlines() if not ln.lstrip().startswith("%"))
    assert code.index("\\providecommand{\\exerciseTitle}{}") < code.index("\\usepackage{exercise}")


def test_render_tex_sheet_thirteen_padding() -> None:
    tex = render_tex(13, [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex13/a.png"])])])
    assert "\\setExerciseSheet{13}" in tex


# ===========================================================================
# render_tex — whole-exercise unlabelled single image (NO \paragraph)
# ===========================================================================

def test_render_tex_unlabelled_single_image_has_no_paragraph() -> None:
    # A whole-exercise part (empty label) must produce a BARE figure with NO \paragraph,
    # exactly like ex1.tex.
    tex = render_tex(
        6,
        [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/aufgabe1_1.png"])])],
    )
    assert "\\paragraph" not in tex
    assert "\\section*{Aufgabe 1}" in tex
    # Exactly one figure block for the single image.
    assert tex.count("\\begin{figure}[!htb]") == 1
    assert tex.count("\\end{figure}") == 1
    assert "\\centering" in tex
    assert "\\includegraphics[width=0.95\\linewidth]{ex06/aufgabe1_1.png}" in tex


# ===========================================================================
# render_tex — one labelled part
# ===========================================================================

def test_render_tex_single_labelled_part_emits_paragraph() -> None:
    tex = render_tex(
        6,
        [ExerciseDoc(index=1, parts=[FigurePart(label="a", image_paths=["ex06/aufgabe1_a_1.png"])])],
    )
    assert "\\paragraph{(a)}" in tex
    # The \paragraph must come BEFORE its figure block.
    assert tex.index("\\paragraph{(a)}") < tex.index("\\begin{figure}")
    assert "\\includegraphics[width=0.95\\linewidth]{ex06/aufgabe1_a_1.png}" in tex
    assert tex.count("\\begin{figure}[!htb]") == 1


def test_render_tex_two_labelled_parts_ordered() -> None:
    # One labelled part each for (a) and (b), in order.
    tex = render_tex(
        6,
        [
            ExerciseDoc(
                index=1,
                parts=[
                    FigurePart(label="a", image_paths=["ex06/aufgabe1_a_1.png"]),
                    FigurePart(label="b", image_paths=["ex06/aufgabe1_b_1.jpeg"]),
                ],
            )
        ],
    )
    assert "\\paragraph{(a)}" in tex
    assert "\\paragraph{(b)}" in tex
    # Part order is preserved: (a) before (b).
    assert tex.index("\\paragraph{(a)}") < tex.index("\\paragraph{(b)}")
    # Each part's image follows its own paragraph and precedes the next paragraph.
    assert tex.index("aufgabe1_a_1.png") < tex.index("\\paragraph{(b)}")
    assert tex.index("\\paragraph{(b)}") < tex.index("aufgabe1_b_1.jpeg")
    assert tex.count("\\begin{figure}[!htb]") == 2


# ===========================================================================
# render_tex — combined "a b" part -> \paragraph{(a) (b)}
# ===========================================================================

def test_render_tex_combined_part_paragraph() -> None:
    # A single photo covering parts a and b: \paragraph{(a) (b)} above one figure.
    tex = render_tex(
        6,
        [ExerciseDoc(index=1, parts=[FigurePart(label="a b", image_paths=["ex06/aufgabe1_ab_1.jpg"])])],
    )
    assert "\\paragraph{(a) (b)}" in tex
    assert tex.count("\\begin{figure}[!htb]") == 1
    assert "\\includegraphics[width=0.95\\linewidth]{ex06/aufgabe1_ab_1.jpg}" in tex
    assert tex.index("\\paragraph{(a) (b)}") < tex.index("\\begin{figure}")


# ===========================================================================
# render_tex — multi-page stacked part (two figures under one paragraph)
# ===========================================================================

def test_render_tex_multipage_part_stacks_figures_under_one_paragraph() -> None:
    # A single labelled part with two ordered pages must yield TWO figure blocks but
    # only ONE \paragraph, with both figures after that paragraph and in page order.
    tex = render_tex(
        6,
        [
            ExerciseDoc(
                index=1,
                parts=[
                    FigurePart(
                        label="b",
                        image_paths=["ex06/aufgabe1_b_1.jpeg", "ex06/aufgabe1_b_2.jpeg"],
                    )
                ],
            )
        ],
    )
    # Exactly one paragraph for the part...
    assert tex.count("\\paragraph") == 1
    assert "\\paragraph{(b)}" in tex
    # ...but two stacked figure blocks.
    assert tex.count("\\begin{figure}[!htb]") == 2
    assert tex.count("\\end{figure}") == 2
    # Page order preserved: page 1 before page 2, both after the paragraph.
    p = tex.index("\\paragraph{(b)}")
    i1 = tex.index("aufgabe1_b_1.jpeg")
    i2 = tex.index("aufgabe1_b_2.jpeg")
    assert p < i1 < i2
    # A blank line separates consecutive figure blocks (readability convention).
    assert "\\end{figure}\n\n\\begin{figure}[!htb]" in tex


# ===========================================================================
# render_tex — multi-exercise \newpage placement
# ===========================================================================

def test_render_tex_newpage_between_exercises_not_after_last() -> None:
    # Three exercises: \newpage appears BETWEEN them (twice) but NOT after the last.
    tex = render_tex(
        6,
        [
            ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/a1.png"])]),
            ExerciseDoc(index=2, parts=[FigurePart(label="", image_paths=["ex06/a2.png"])]),
            ExerciseDoc(index=3, parts=[FigurePart(label="", image_paths=["ex06/a3.png"])]),
        ],
    )
    # Two separators for three exercises (n-1).
    assert tex.count("\\newpage") == 2
    # The last exercise's figure must come AFTER the final \newpage, and nothing
    # (other than \end{document}) follows it: i.e. there is no \newpage after the
    # last section.
    last_section = tex.rindex("\\section*{Aufgabe 3}")
    assert "\\newpage" not in tex[last_section:]
    # \end{document} is the tail and is not preceded by a stray trailing \newpage.
    assert tex.rstrip().endswith("\\end{document}")
    # Section ordering is ascending.
    assert tex.index("Aufgabe 1") < tex.index("Aufgabe 2") < tex.index("Aufgabe 3")


def test_render_tex_sorts_exercises_by_index() -> None:
    # Exercises handed in out of order must be rendered ascending by .index.
    tex = render_tex(
        6,
        [
            ExerciseDoc(index=3, parts=[FigurePart(label="", image_paths=["ex06/a3.png"])]),
            ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/a1.png"])]),
            ExerciseDoc(index=2, parts=[FigurePart(label="", image_paths=["ex06/a2.png"])]),
        ],
    )
    assert tex.index("\\section*{Aufgabe 1}") < tex.index("\\section*{Aufgabe 2}")
    assert tex.index("\\section*{Aufgabe 2}") < tex.index("\\section*{Aufgabe 3}")
    # Newpage still appears only between, twice.
    assert tex.count("\\newpage") == 2


def test_render_tex_single_exercise_has_no_newpage() -> None:
    tex = render_tex(
        6,
        [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex06/a1.png"])])],
    )
    assert "\\newpage" not in tex


# ===========================================================================
# render_tex — root-relative includegraphics paths
# ===========================================================================

def test_render_tex_includegraphics_paths_are_root_relative() -> None:
    # Paths must be inserted verbatim and root-relative (POSIX "ex06/..."), matching
    # the cwd=project-root compile convention. No leading slash, no absolute path.
    tex = render_tex(
        6,
        [
            ExerciseDoc(
                index=1,
                parts=[FigurePart(label="a", image_paths=["ex06/aufgabe1_a_1.png"])],
            )
        ],
    )
    assert "{ex06/aufgabe1_a_1.png}" in tex
    # The path is root-relative: it appears right after the closing brace of the
    # width option, with no leading slash.
    assert "\\includegraphics[width=0.95\\linewidth]{ex06/aufgabe1_a_1.png}" in tex
    assert "{/ex06/" not in tex  # not absolute


# ===========================================================================
# render_tex — empty parts contributes only a section header
# ===========================================================================

def test_render_tex_empty_parts_yields_only_section_header() -> None:
    # An exercise with no parts contributes its \section* header and no figures.
    tex = render_tex(6, [ExerciseDoc(index=1, parts=[])])
    assert "\\section*{Aufgabe 1}" in tex
    assert "\\begin{figure}" not in tex
    assert "\\paragraph" not in tex


# ===========================================================================
# parse_log
# ===========================================================================

def test_parse_log_surfaces_bang_error_lines() -> None:
    # Lines beginning with '!' (TeX errors) must be surfaced in the excerpt, along
    # with their nearby "l.<n>" context line.
    log = (
        "This is pdfTeX, Version 3.14159265\n"
        "(./ex06/ex06.tex\n"
        "! Undefined control sequence.\n"
        "l.42 \\foo\n"
        "        bar\n"
        "Some trailing noise line one\n"
        "Some trailing noise line two\n"
    )
    excerpt = parse_log(log)
    assert "! Undefined control sequence." in excerpt
    # The matching context line is captured too.
    assert "l.42" in excerpt


def test_parse_log_multiple_errors_all_present() -> None:
    log = (
        "preamble line\n"
        "! Missing $ inserted.\n"
        "l.10 x_2\n"
        "more text\n"
        "! Undefined control sequence.\n"
        "l.20 \\bar\n"
        "tail line\n"
    )
    excerpt = parse_log(log)
    assert "! Missing $ inserted." in excerpt
    assert "! Undefined control sequence." in excerpt


def test_parse_log_tail_fallback_when_no_bang_lines() -> None:
    # No '!' lines at all (e.g. a missing-file / fatal-config failure): the excerpt
    # must still carry useful context via the log's tail.
    log = "\n".join(f"line {i}" for i in range(1, 40)) + "\nfinal distinctive line\n"
    excerpt = parse_log(log)
    assert excerpt != ""
    # The tail (last lines) must be present even with no '!' lines.
    assert "final distinctive line" in excerpt
    # Early lines should have been dropped (only a tail is kept).
    assert "line 1\n" not in excerpt


def test_parse_log_empty_input_returns_empty() -> None:
    # Robust to empty/missing data: never raises, returns "".
    assert parse_log("") == ""


def test_parse_log_truncates_to_max_chars() -> None:
    # Excerpts longer than max_chars are clipped with an explicit ellipsis marker.
    log = "\n".join(f"! error number {i} with some padding text here" for i in range(500))
    excerpt = parse_log(log, max_chars=200)
    assert len(excerpt) <= 200
    assert "[" in excerpt and "]" in excerpt  # ellipsis marker present


# ===========================================================================
# render_tex — HeaderOverrides (per-group customization)
# ===========================================================================

def _doc():
    return [ExerciseDoc(index=1, parts=[FigurePart(label="", image_paths=["ex07/a.png"])])]


def test_render_tex_no_overrides_emits_no_renewcommand() -> None:
    tex = render_tex(7, _doc())
    assert "\\renewcommand{\\ExerciseGroup}" not in tex
    assert "\\renewcommand{\\exerciseCourse}" not in tex
    assert "\\renewcommand{\\exerciseGroup}" not in tex
    assert "\\renewcommand{\\exerciseAuthors}" not in tex


def test_render_tex_none_overrides_object_emits_nothing() -> None:
    # An explicit but all-None HeaderOverrides behaves like no overrides.
    tex = render_tex(7, _doc(), HeaderOverrides())
    assert "\\renewcommand{\\Exercise" not in tex
    assert "\\renewcommand{\\exercise" not in tex


def test_render_tex_overrides_emit_renewcommands_after_usepackage() -> None:
    ov = HeaderOverrides(
        group_number="017",
        course="GLOIN WiSe 2026",
        tutorium="Tutorium 03",
        authors=["Anna Beispiel, 111111", "Ben Muster, 222222"],
    )
    tex = render_tex(7, _doc(), ov)
    assert "\\renewcommand{\\ExerciseGroup}{017}" in tex
    assert "\\renewcommand{\\exerciseCourse}{GLOIN WiSe 2026}" in tex
    assert "\\renewcommand{\\exerciseGroup}{Tutorium 03}" in tex
    assert "\\renewcommand{\\exerciseAuthors}{" in tex
    # Both authors present and separated by a LaTeX line break (\\).
    assert "Anna Beispiel, 111111" in tex
    assert "Ben Muster, 222222" in tex
    assert "111111\\\\" in tex  # trailing LaTeX line break after the first author
    # Overrides must come AFTER the package load (renewcommand needs the macros to exist)
    # and BEFORE \exerciseMakeHeaders so the headers pick up the new values.
    assert tex.index("\\usepackage{exercise}") < tex.index("\\renewcommand{\\ExerciseGroup}")
    assert tex.index("\\renewcommand{\\ExerciseGroup}") < tex.index("\\exerciseMakeHeaders")


def test_render_tex_partial_overrides_only_set_fields() -> None:
    tex = render_tex(7, _doc(), HeaderOverrides(group_number="999"))
    assert "\\renewcommand{\\ExerciseGroup}{999}" in tex
    assert "\\renewcommand{\\exerciseCourse}" not in tex
    assert "\\renewcommand{\\exerciseAuthors}" not in tex


def test_render_tex_override_values_are_latex_escaped() -> None:
    # Special chars in a free-text override must be escaped, not break compilation.
    tex = render_tex(7, _doc(), HeaderOverrides(course="A & B 100% _x_"))
    assert "\\&" in tex and "\\%" in tex and "\\_" in tex
    assert "A & B" not in tex  # the raw, unescaped form must not appear


def test_render_tex_blank_authors_entries_dropped() -> None:
    # Empty/whitespace author entries are filtered out.
    tex = render_tex(7, _doc(), HeaderOverrides(authors=["  ", "Real Person, 1", ""]))
    assert "Real Person, 1" in tex
    # Only one author -> no line break inside the authors block.
    authors_block = tex.split("\\renewcommand{\\exerciseAuthors}{", 1)[1].split("}", 1)[0]
    assert "\\\\" not in authors_block
