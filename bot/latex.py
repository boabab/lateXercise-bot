"""Pure, framework-free LaTeX generation, compilation, and log parsing for the lateXercise bot.

This module is the single place that knows how to turn a study group's *selection*
(an ordered list of exercises, each an ordered list of parts, each holding ordered
image pages) into a `.tex` document that matches the bundled template style
(``latex-project/exercise.sty`` and ``example.tex``) — and how to compile that
document into the ``Gruppe_<group>_Blatt_<NN>.pdf`` the group submits.

Design constraints (see ``data/CONTRACTS.md``):

* **No discord, no Pillow imports.** Everything here is plain stdlib so it is fully
  unit-testable without a Discord runtime or a LaTeX install.
* The compiler is launched with :func:`asyncio.create_subprocess_exec` (never
  ``shell=True`` — the project path contains a space) inside a *new session* so a
  timeout can kill the whole process group via :func:`os.killpg`.
* Generated ``\\includegraphics`` paths are **root-relative** POSIX paths (e.g.
  ``ex06/aufgabe1_a_1.png``) because the compiler runs with ``cwd`` set to the
  project root, exactly like the bundled example sheet.

The data shapes :class:`FigurePart` and :class:`ExerciseDoc` are the contract between
the store/cog layer and this module: callers translate their persisted selection into
these dataclasses and hand them to :func:`render_tex`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "pad_sheet",
    "label_to_paragraph",
    "label_to_filename_fragment",
    "FigurePart",
    "ExerciseDoc",
    "HeaderOverrides",
    "render_tex",
    "CompileResult",
    "CompileError",
    "resolve_compiler",
    "compile_pdf",
    "parse_log",
    "read_group",
]


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def pad_sheet(sheet: int) -> str:
    """Zero-pad a sheet number to two digits for display and paths.

    Examples:
        >>> pad_sheet(6)
        '06'
        >>> pad_sheet(13)
        '13'

    Numbers with three or more digits are returned unpadded by :func:`format`
    (``f"{n:02d}"`` only pads up to two digits), which is the intended behaviour:
    the contract restricts sheets to ``1..99`` elsewhere, so two digits suffice.
    """
    return f"{sheet:02d}"


# Characters that must be escaped when a token is placed into LaTeX text such as a
# ``\paragraph{...}`` argument. Labels are normally just ``a``/``b``/``c`` but we
# escape defensively so an unexpected label can never break compilation or inject
# markup. Order matters: backslash is handled first so we do not double-escape the
# replacements we insert for the other characters.
_LATEX_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)


def _latex_escape(text: str) -> str:
    """Minimally escape *text* so it is safe inside LaTeX text-mode arguments."""
    for raw, replacement in _LATEX_ESCAPES:
        text = text.replace(raw, replacement)
    return text


def label_to_paragraph(label: str) -> str:
    """Render the ``\\paragraph`` argument from a canonical part label.

    The label is the canonical form produced by the spec parser: whitespace-separated
    lowercase tokens, e.g. ``""`` (whole exercise), ``"a"``, or ``"a b"`` (a single
    photo that covers parts a *and* b).

    Mapping:
        * ``""``    -> ``""``      (caller emits **no** ``\\paragraph`` for an empty label)
        * ``"a"``   -> ``"(a)"``
        * ``"a b"`` -> ``"(a) (b)"``

    Each token is wrapped in parentheses and LaTeX-escaped defensively. Tokens are
    joined by a single space, matching ``\\paragraph{(a) (b)}`` in the bundled
    ``example.tex``.
    """
    tokens = label.split()
    if not tokens:
        return ""
    return " ".join(f"({_latex_escape(token)})" for token in tokens)


def label_to_filename_fragment(label: str) -> str:
    """Turn a canonical label into an ASCII fragment safe for filenames.

    Mapping:
        * ``""``    -> ``""``
        * ``"a"``   -> ``"a"``
        * ``"a b"`` -> ``"ab"``

    Implementation: lowercase, then keep only ``[a-z0-9]`` characters, dropping
    everything else (spaces, parentheses, punctuation). This guarantees a portable,
    shell- and TeX-safe fragment that slots into ``aufgabe{N}_{frag}_{page}.{ext}``.
    """
    return re.sub(r"[^a-z0-9]", "", label.lower())


# ---------------------------------------------------------------------------
# Document data shapes
# ---------------------------------------------------------------------------

@dataclass
class FigurePart:
    """One part of an exercise: an optional label and its ordered image pages.

    Attributes:
        label: The canonical raw label — ``""`` for the whole exercise (no
            ``\\paragraph`` emitted), ``"a"`` for a single sub-part, or ``"a b"`` for a
            combined photo covering parts a and b.
        image_paths: Ordered, root-relative POSIX paths to the page images, e.g.
            ``["ex06/aufgabe1_a_1.png"]``. One ``figure`` block is emitted per path.
    """

    label: str
    image_paths: list[str] = field(default_factory=list)


@dataclass
class ExerciseDoc:
    """One exercise (Aufgabe) within a sheet.

    Attributes:
        index: 1-based Aufgabe number. Exercises are sorted ascending by this value
            in :func:`render_tex`, regardless of input order.
        parts: Ordered parts. May be empty only when the caller explicitly allows
            skipping; an empty list yields just the ``\\section*`` header.
    """

    index: int
    parts: list[FigurePart] = field(default_factory=list)


@dataclass
class HeaderOverrides:
    """Per-group overrides for the header/title fields defined in ``exercise.sty``.

    ``exercise.sty`` hard-codes the group number, course, tutorium, and author list.
    Since the bot must not edit that shared file, :func:`render_tex` instead emits
    ``\\renewcommand`` lines in the *generated* document for whichever of these are set
    (after ``\\usepackage{exercise}``, so the package's own definitions exist to renew).
    A field left ``None``/empty is simply not overridden — ``exercise.sty``'s default
    stands. See the cog's ``/konfig`` command and the ``.env`` defaults.

    Attributes:
        group_number: Overrides ``\\ExerciseGroup`` (e.g. ``"000"``). Also drives the
            output filename ``Gruppe_<group>_Blatt_<NN>.pdf`` (the cog uses it for the
            ``-jobname``). Should be alphanumeric so the filename stays portable.
        course: Overrides ``\\exerciseCourse`` (e.g. ``"Course Name"``).
        tutorium: Overrides ``\\exerciseGroup`` (e.g. ``"Tutorium 00"``).
        authors: Overrides ``\\exerciseAuthors``; each entry is one line, joined with a
            LaTeX ``\\\\`` line break (e.g. ``["Anna Muster, 111111", "Ben Beispiel, ..."]``).
    """

    group_number: str | None = None
    course: str | None = None
    tutorium: str | None = None
    authors: list[str] | None = None


# ---------------------------------------------------------------------------
# .tex rendering
# ---------------------------------------------------------------------------

def _render_header_overrides(overrides: HeaderOverrides | None) -> list[str]:
    """Return ``\\renewcommand`` preamble lines for the set fields of *overrides*.

    Emits a line only for each field that is present and non-empty, so unset fields
    keep ``exercise.sty``'s defaults. All values are LaTeX-escaped. Returns ``[]`` when
    there is nothing to override. The lines belong AFTER ``\\usepackage{exercise}`` (the
    commands must already be defined for ``\\renewcommand`` to succeed).
    """
    if overrides is None:
        return []

    lines: list[str] = []
    if overrides.group_number and overrides.group_number.strip():
        lines.append(
            f"\\renewcommand{{\\ExerciseGroup}}{{{_latex_escape(overrides.group_number.strip())}}}"
        )
    if overrides.course and overrides.course.strip():
        lines.append(
            f"\\renewcommand{{\\exerciseCourse}}{{{_latex_escape(overrides.course.strip())}}}"
        )
    if overrides.tutorium and overrides.tutorium.strip():
        lines.append(
            f"\\renewcommand{{\\exerciseGroup}}{{{_latex_escape(overrides.tutorium.strip())}}}"
        )
    if overrides.authors:
        escaped = [_latex_escape(a.strip()) for a in overrides.authors if a.strip()]
        if escaped:
            # Join author lines with a LaTeX line break (\\) + newline + indent, mirroring
            # the multi-line \exerciseAuthors block in exercise.sty.
            body = "\\\\\n    ".join(escaped)
            lines.append("\\renewcommand{\\exerciseAuthors}{%\n    " + body + "}")

    if not lines:
        return []
    # A blank separator line + a comment keeps the generated preamble readable.
    return ["", "% Per-group header overrides (set via /konfig or .env defaults)."] + lines

def _render_figure(image_path: str) -> str:
    """Render a single ``figure`` block for *image_path*.

    Matches the example sheet exactly: ``[!htb]`` placement, 4-space-indented
    ``\\centering`` and ``\\includegraphics[width=0.95\\linewidth]{...}``. The image
    path is inserted verbatim — it is a controlled, sanitized root-relative POSIX
    path produced by ``images.target_filename`` / ``label_to_filename_fragment``, not
    user free-text, so it needs no LaTeX escaping (and escaping would corrupt the
    path separators).
    """
    return (
        "\\begin{figure}[!htb]\n"
        "    \\centering\n"
        f"    \\includegraphics[width=0.95\\linewidth]{{{image_path}}}\n"
        "\\end{figure}"
    )


def render_tex(
    sheet: int,
    exercises: list[ExerciseDoc],
    overrides: HeaderOverrides | None = None,
) -> str:
    """Return the full ``.tex`` document as a string.

    The output mirrors the bundled example sheet (``example.tex``) and the
    contract's skeleton::

        \\documentclass[a4paper,11pt]{scrartcl}

        \\usepackage{exercise}
        \\setExerciseSheet{06}
        \\exerciseMakeHeaders

        \\begin{document}
        \\section*{Aufgabe 1}
        <parts...>
        \\newpage
        \\section*{Aufgabe 2}
        <parts...>
        \\end{document}

    Per exercise:
        * ``\\section*{Aufgabe <index>}`` header (followed by a blank line).
        * For each part: if the label is non-empty, ``\\paragraph{<label>}`` via
          :func:`label_to_paragraph`; then one ``figure`` block per image, with a
          blank line between consecutive figure blocks.
        * ``\\newpage`` is emitted **between** exercises but never after the last one.

    Exercises are sorted by ``.index`` ascending. An exercise with an empty ``parts``
    list contributes only its ``\\section*`` header — callers normally filter these
    out or abort before reaching here.

    Args:
        sheet: The sheet number; zero-padded into ``\\setExerciseSheet`` via
            :func:`pad_sheet`.
        exercises: The exercises to render.
        overrides: Optional per-group header overrides (group/course/tutorium/authors).
            Each set field emits a ``\\renewcommand`` after ``\\usepackage{exercise}``;
            unset fields keep ``exercise.sty``'s defaults.

    Returns:
        The complete document text, ending with a trailing newline.
    """
    padded = pad_sheet(sheet)

    # Preamble. Mirrors example.tex / ex*.tex so the generated file is indistinguishable
    # in style from the example sheet, with ONE robustness shim:
    #
    #   \providecommand{\exerciseTitle}{}
    #
    # The shipped exercise.sty does `\renewcommand{\exerciseTitle}{Blatt \ExerciseSheet}`
    # (line ~102) on a command it never `\newcommand`'d (the defining line right below is
    # commented out), so loading the package fails with "Command \exerciseTitle undefined"
    # on a clean TeX install. We must NOT edit the shared exercise.sty, so instead the
    # generated document defines \exerciseTitle as empty *before* loading the package; the
    # package's \renewcommand then succeeds and still sets the title to "Blatt <NN>".
    # \providecommand is a no-op if a future exercise.sty defines the command itself.
    lines: list[str] = [
        "\\documentclass[a4paper,11pt]{scrartcl}",
        "",
        "% Work around a latent bug in exercise.sty (renewcommand on an undefined",
        "% \\exerciseTitle); define it first so \\usepackage{exercise} can renew it.",
        "\\providecommand{\\exerciseTitle}{}",
        "\\usepackage{exercise}",
        f"\\setExerciseSheet{{{padded}}}",
    ]
    # Per-group header overrides (group number / course / tutorium / authors), emitted
    # after the package defines the commands and before \exerciseMakeHeaders uses them.
    lines.extend(_render_header_overrides(overrides))
    lines.extend([
        "\\exerciseMakeHeaders",
        "",
        "\\begin{document}",
    ])

    # Stable, ascending order by Aufgabe number — the store may hand us any order.
    ordered = sorted(exercises, key=lambda ex: ex.index)

    for ex_pos, exercise in enumerate(ordered):
        # Section header, then a blank line, exactly like the example sheet.
        lines.append(f"\\section*{{Aufgabe {exercise.index}}}")
        lines.append("")

        for part in exercise.parts:
            # Emit a \paragraph label only for non-empty labels; a whole-exercise
            # part is just the bare figure(s).
            paragraph = label_to_paragraph(part.label)
            if paragraph:
                lines.append(f"\\paragraph{{{paragraph}}}")

            # One figure block per page image, blank line between blocks.
            for img_pos, image_path in enumerate(part.image_paths):
                if img_pos > 0:
                    lines.append("")
                lines.append(_render_figure(image_path))

            # Blank line after the part's figures for readability between parts.
            lines.append("")

        # Page break between exercises, never after the last one.
        if ex_pos < len(ordered) - 1:
            lines.append("\\newpage")
            lines.append("")

    lines.append("\\end{document}")

    # Join with newlines and guarantee a trailing newline (POSIX text file).
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

class CompileError(Exception):
    """Raised when no LaTeX compiler can be resolved (with install hints)."""


@dataclass
class CompileResult:
    """Outcome of a :func:`compile_pdf` run.

    Attributes:
        ok: ``True`` iff the compiler exited 0 *and* the expected PDF exists.
        pdf_path: Absolute path to the produced PDF on success, else ``None``.
        returncode: Exit code of the (last) compiler invocation.
        log_excerpt: Trimmed log excerpt; ``""`` on a clean success.
        cmd: The argv actually run, for diagnostics.
        timed_out: ``True`` if the run was killed for exceeding the timeout.
    """

    ok: bool
    pdf_path: Path | None
    returncode: int
    log_excerpt: str
    cmd: list[str]
    timed_out: bool = False


# Hints surfaced when no compiler is found, so the operator knows how to fix it.
_INSTALL_HINTS = (
    "No LaTeX compiler found (looked for 'latexmk' then 'pdflatex' on PATH). "
    "Install a TeX distribution and ensure it is on PATH:\n"
    "  macOS:  install MacTeX (https://tug.org/mactex/) — adds /Library/TeX/texbin; "
    "if running under launchd, set TEX_CMD or the service PATH explicitly.\n"
    "  Linux:  apt install texlive-latex-recommended texlive-latex-extra "
    "texlive-lang-german latexmk\n"
    "Alternatively set TEX_CMD in your .env to the absolute path of latexmk or pdflatex."
)


def _infer_kind(name: str) -> str:
    """Infer the compiler kind from a binary name/path.

    Returns ``"latexmk"`` if the basename contains ``latexmk`` (case-insensitive),
    otherwise ``"pdflatex"``. The default favours ``pdflatex`` semantics (run twice)
    for any non-latexmk binary the operator points us at.
    """
    base = Path(name).name.lower()
    return "latexmk" if "latexmk" in base else "pdflatex"


def resolve_compiler(tex_cmd: str | None) -> tuple[str, Path]:
    """Decide which LaTeX binary to use.

    Resolution order:
        1. If *tex_cmd* is given and non-blank: resolve it via :func:`shutil.which`
           (so a bare name like ``latexmk`` is found on PATH) or accept it directly if
           it is an existing file. Infer the kind from its name. Raise
           :class:`CompileError` if it cannot be resolved.
        2. Otherwise prefer ``latexmk``, then ``pdflatex``, via :func:`shutil.which`.

    Args:
        tex_cmd: Optional explicit compiler override (name or absolute path); ``None``
            or blank means "auto-detect".

    Returns:
        ``(kind, absolute_path)`` where ``kind`` is ``"latexmk"`` or ``"pdflatex"``.

    Raises:
        CompileError: If no usable compiler can be found (message includes install
            hints for MacTeX / TeX Live).
    """
    if tex_cmd and tex_cmd.strip():
        candidate = tex_cmd.strip()
        # First try PATH lookup (handles bare names like "latexmk").
        resolved = shutil.which(candidate)
        if resolved is not None:
            return _infer_kind(resolved), Path(resolved).resolve()
        # Then accept an existing file path directly (e.g. an absolute override that
        # PATH lookup would not surface, or one lacking the +x heuristics of which).
        path = Path(candidate)
        if path.is_file():
            return _infer_kind(candidate), path.resolve()
        raise CompileError(
            f"Configured TEX_CMD {tex_cmd!r} could not be resolved "
            f"(not on PATH and not an existing file).\n{_INSTALL_HINTS}"
        )

    # Auto-detect: latexmk preferred (handles multi-pass automatically), then pdflatex.
    for name in ("latexmk", "pdflatex"):
        resolved = shutil.which(name)
        if resolved is not None:
            return _infer_kind(resolved), Path(resolved).resolve()

    raise CompileError(_INSTALL_HINTS)


def _build_argv(kind: str, binary: Path, jobname: str, tex_rel_path: str) -> list[str]:
    """Build the compiler argv for one invocation.

    ``-jobname`` controls **both** the produced ``.pdf`` and ``.log`` filenames, which
    is why we can reliably read ``<jobname>.log`` afterwards.
    """
    if kind == "latexmk":
        return [
            str(binary),
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-jobname={jobname}",
            tex_rel_path,
        ]
    # pdflatex (or any non-latexmk binary): single-pass argv; caller runs it twice.
    return [
        str(binary),
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-jobname={jobname}",
        tex_rel_path,
    ]


async def _run_once(
    argv: list[str], project_dir: Path, timeout: float
) -> tuple[int, bytes, bytes, bool]:
    """Run *argv* once in *project_dir*, returning ``(rc, stdout, stderr, timed_out)``.

    The subprocess is started in a *new session* (``start_new_session=True``) so that on
    timeout we can signal the entire process group — LaTeX can spawn helper children,
    and killing only the parent would orphan them. ``stdin`` is connected to
    ``/dev/null`` so an interactive prompt (despite ``nonstopmode``) can never hang the
    build waiting for input.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode if proc.returncode is not None else -1, stdout, stderr, False
    except asyncio.TimeoutError:
        # Kill the whole process group, then drain/reap so we do not leak the pipes or
        # leave a zombie. os.killpg requires the session leader's pgid, which equals the
        # child pid because we created a new session above.
        _kill_process_group(proc)
        try:
            stdout, stderr = await proc.communicate()
        except Exception:
            # Best effort: if draining fails after the kill, fall back to empty output.
            stdout, stderr = b"", b""
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout, stderr, True


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the subprocess's whole process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        # Process already gone or pgid unavailable; try a direct kill as a fallback.
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def compile_pdf(
    *,
    project_dir: Path,
    tex_rel_path: str,
    jobname: str,
    tex_cmd: str | None = None,
    timeout: float = 120.0,
) -> CompileResult:
    """Compile *tex_rel_path* into ``<jobname>.pdf`` inside *project_dir*.

    The compiler runs with ``cwd=project_dir`` so that ``exercise.sty`` and the
    root-relative ``\\includegraphics{ex06/...}`` paths resolve via kpathsea, exactly
    like the example sheet. The compiler is **never** launched via a shell
    (``shell=True``) — the project path legitimately contains a space.

    Behaviour by compiler kind:
        * ``latexmk`` — a single invocation (it handles multi-pass itself).
        * ``pdflatex`` — run **twice** so hyperref labels / scrlayer headers settle.
          The second run is skipped if the first already failed (returncode != 0).

    A timeout kills the whole process group (see :func:`_run_once`) and sets
    ``timed_out=True``.

    Args:
        project_dir: Absolute path to the LaTeX project root (the compile ``cwd``).
        tex_rel_path: The ``.tex`` file path *relative* to ``project_dir`` (e.g.
            ``"ex06/ex06.tex"``).
        jobname: Output base name; produces ``<jobname>.pdf`` and ``<jobname>.log``.
        tex_cmd: Optional compiler override forwarded to :func:`resolve_compiler`.
        timeout: Per-invocation wall-clock limit in seconds.

    Returns:
        A :class:`CompileResult`. ``ok`` is ``True`` iff the final returncode is 0 and
        the expected PDF exists. On failure, ``log_excerpt`` is populated from the
        ``<jobname>.log`` file (falling back to combined stdout/stderr) via
        :func:`parse_log`.

    Raises:
        CompileError: If no compiler can be resolved (propagated from
            :func:`resolve_compiler`) — this is a preflight/configuration error, not a
            compile failure.
    """
    kind, binary = resolve_compiler(tex_cmd)
    argv = _build_argv(kind, binary, jobname, tex_rel_path)

    # First (and for latexmk, only) pass.
    returncode, stdout, stderr, timed_out = await _run_once(argv, project_dir, timeout)

    # pdflatex needs a second pass for references/headers — but only if the first
    # pass succeeded and did not time out (a failing first pass will just fail again).
    if kind == "pdflatex" and returncode == 0 and not timed_out:
        returncode, stdout, stderr, timed_out = await _run_once(argv, project_dir, timeout)

    pdf_path = (project_dir / f"{jobname}.pdf").resolve()
    pdf_exists = pdf_path.is_file()
    ok = (returncode == 0) and pdf_exists and not timed_out

    if ok:
        # Clean success: no excerpt, return the PDF path.
        return CompileResult(
            ok=True,
            pdf_path=pdf_path,
            returncode=returncode,
            log_excerpt="",
            cmd=argv,
            timed_out=False,
        )

    # Failure (or timeout): assemble a diagnostic excerpt. Prefer the .log file the
    # compiler wrote (named after -jobname); fall back to the captured streams.
    log_path = project_dir / f"{jobname}.log"
    log_text = ""
    try:
        if log_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    if not log_text:
        # Combine the captured streams as a fallback source.
        combined = b"\n".join(part for part in (stdout, stderr) if part)
        log_text = combined.decode("utf-8", errors="replace")

    excerpt = parse_log(log_text)
    if timed_out:
        # Make the timeout explicit in the excerpt so the cog can surface it clearly.
        prefix = f"[timed out after {timeout:g}s]\n"
        excerpt = (prefix + excerpt) if excerpt else prefix.strip()

    return CompileResult(
        ok=False,
        pdf_path=pdf_path if pdf_exists else None,
        returncode=returncode,
        log_excerpt=excerpt,
        cmd=argv,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

# A TeX error line begins with '!' (e.g. "! Undefined control sequence.").
_ERROR_LINE = re.compile(r"^!")
# The context line TeX prints with the offending input line number, e.g. "l.42 ...".
_CONTEXT_LINE = re.compile(r"^l\.\d+")


def parse_log(log_text: str, max_chars: int = 1500) -> str:
    """Extract a concise, human-readable excerpt from a LaTeX log.

    Strategy:
        1. Collect every line that starts with ``!`` (TeX error lines), plus — for each
           such error — the next following ``l.<n>`` context line if present (TeX emits
           it a line or two later), so the operator sees both the error and where it
           occurred.
        2. Append a tail of the whole log (its last lines) so context is preserved even
           when no ``!`` line was found (e.g. a missing-file or fatal-config failure).
        3. Truncate the result to *max_chars*, inserting an explicit ``[…]`` ellipsis
           marker so callers know the excerpt was clipped.

    The function is robust to empty / malformed / non-LaTeX input: it never raises and
    returns ``""`` for empty input.

    Args:
        log_text: The raw log contents (or fallback stdout/stderr).
        max_chars: Maximum length of the returned excerpt.

    Returns:
        A trimmed excerpt suitable for posting in a Discord code block.
    """
    if not log_text:
        return ""

    lines = log_text.splitlines()

    # 1) Error lines plus their nearest following context line.
    error_section: list[str] = []
    for i, line in enumerate(lines):
        if _ERROR_LINE.match(line):
            error_section.append(line)
            # Look ahead a few lines for the matching "l.<n>" context line.
            for follow in lines[i + 1 : i + 6]:
                if _CONTEXT_LINE.match(follow):
                    error_section.append(follow)
                    break

    # 2) Tail of the log (last ~15 non-empty lines), to give surrounding context.
    tail_lines = [ln for ln in lines if ln.strip()][-15:]

    parts: list[str] = []
    if error_section:
        parts.append("\n".join(error_section))
    if tail_lines:
        tail_block = "\n".join(tail_lines)
        # Avoid duplicating the tail if it is already fully contained in the errors.
        if not error_section or tail_block not in "\n".join(error_section):
            parts.append("--- log tail ---\n" + tail_block)

    excerpt = "\n\n".join(parts).strip()
    if not excerpt:
        # No error lines and no non-empty tail: fall back to the raw text, trimmed.
        excerpt = log_text.strip()

    # 3) Truncate with an explicit ellipsis marker (keep the start, which holds the
    #    error lines that matter most).
    if len(excerpt) > max_chars:
        marker = "\n[…]"
        keep = max(0, max_chars - len(marker))
        excerpt = excerpt[:keep].rstrip() + marker

    return excerpt


# ---------------------------------------------------------------------------
# Reading the configured exercise group from exercise.sty
# ---------------------------------------------------------------------------

# Matches e.g. `\newcommand{\ExerciseGroup}{000}` — captures the numeric group.
_GROUP_RE = re.compile(r"\\ExerciseGroup\}\{(\d+)\}")


def read_group(project_dir: Path, *, default: str = "000") -> str:
    """Read the exercise group number from ``exercise.sty``.

    The group is defined in the (never-edited) style file as
    ``\\newcommand{\\ExerciseGroup}{000}``. We extract the digits via regex so the
    bot's output filename (``Gruppe_<group>_Blatt_<NN>.pdf``) always tracks whatever
    the style file declares.

    Args:
        project_dir: The LaTeX project root containing ``exercise.sty``.
        default: Group string to return if the file is missing/unreadable or the
            command cannot be found (defaults to ``"000"``).

    Returns:
        The captured group string, or *default* on any failure.
    """
    sty_path = project_dir / "exercise.sty"
    try:
        text = sty_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default

    match = _GROUP_RE.search(text)
    if match is None:
        return default
    return match.group(1)
