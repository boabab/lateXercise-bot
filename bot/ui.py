"""Pick-flow UI for the lateXercise bot.

This module has two distinct halves:

1. A **pure**, framework-free spec parser (`parse_pick_spec`) plus its data shapes
   (`ParsedPart`, `PickSpecError`). These depend only on the standard library (``re``)
   and are imported and unit-tested *without* a Discord runtime. They translate the
   free-text modal input — one line per exercise part, ``<label>: <n>[, <n> ...]`` — into
   an ordered list of `ParsedPart`, applying label canonicalization and validation.

2. The **Discord widgets** (`Candidate`, `build_gallery_embeds`, `preview_text`,
   `PickModal`, `PickView`) which require a live ``discord.py`` runtime. They keep their
   logic thin and delegate all parsing/validation to `parse_pick_spec` so the
   interesting behaviour stays testable.

Flow recap (driven by the `/pick` cog):
    * The cog gathers candidate images from the thread, numbers them, and shows an
      ephemeral message: a numbered text list of *all* candidates (up to 25) plus a
      gallery of up to 10 thumbnail embeds (Discord's hard cap is 10 embeds/message).
    * The user clicks "Teile eingeben", which opens `PickModal` — a single paragraph
      `TextInput`. On submit the text is run through `parse_pick_spec`.
    * On a `PickSpecError` the message is shown ephemerally and the user can retry.
    * On success the parse is stored on the `PickView`, and the ephemeral message is
      edited to show `preview_text` plus "Bestätigen"/"Abbrechen" buttons.
    * "Bestätigen" awaits the cog-supplied ``on_confirm(parsed)`` callback (which
      persists the selection) and then disables the view. "Abbrechen" just disables it.

Discord cap note: a message may carry at most 10 embeds, but `/pick` may surface up to
25 candidates. We therefore render the 10 *most recent* candidates as thumbnail embeds
*and* list all (up to 25) as a numbered text block in the message content, so users can
still reference numbers > 10 by reading the filename in the text list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

if TYPE_CHECKING:
    # Imported only for type checking so the pure parser half stays import-light at
    # runtime where Discord is unavailable. (discord is imported above unconditionally
    # because the widget classes subclass it, but this guards the callback alias.)
    OnConfirm = Callable[["list[ParsedPart]"], Awaitable[None]]


# ---------------------------------------------------------------------------
# Pure spec parser (stdlib only; unit-testable without a Discord runtime)
# ---------------------------------------------------------------------------


@dataclass
class ParsedPart:
    """One parsed exercise part.

    Attributes:
        label: Canonical label. ``""`` means the whole exercise (no sub-part);
            ``"a"`` a single labelled part; ``"a b"`` a photo covering parts a+b.
        numbers: 1-based candidate indices in the order the user gave them (page
            order). Numbers may repeat across parts — a single photo can cover
            several parts — which is allowed.
    """

    label: str
    numbers: list[int]


class PickSpecError(ValueError):
    """Raised when the modal spec is invalid.

    Carries a human-readable, user-facing message (German, to match the bot's
    surface language) that is shown ephemerally so the user can correct and retry.
    """


# Separators that join label tokens. Whitespace, comma, ``+`` and ``)`` all delimit
# tokens, so ``"a) b)"``, ``"a,b"``, ``"a b"`` and ``"a+b"`` all canonicalize to
# ``"a b"``. ``re.split`` on this class yields tokens; we then keep only ``[a-z0-9]``.
_LABEL_SEP_RE = re.compile(r"[\s,+)]+")
# After splitting, keep only characters in the allowed set inside each token.
_LABEL_KEEP_RE = re.compile(r"[^a-z0-9]")


def _canonicalize_label(raw: str) -> str:
    """Canonicalize a raw label fragment.

    Lowercases, splits on any run of whitespace / ``,`` / ``+`` / ``)``, strips every
    character outside ``[a-z0-9]`` from each token, drops now-empty tokens, and joins
    the survivors with single spaces.

    Examples:
        ``"a) b)"`` -> ``"a b"``;  ``"a,b"`` -> ``"a b"``;  ``"a+b"`` -> ``"a b"``;
        ``"a b"`` -> ``"a b"``;  ``"(a)"`` -> ``"a"``;  ``""`` / ``"   "`` -> ``""``.
    """
    lowered = raw.strip().lower()
    if not lowered:
        return ""
    tokens: list[str] = []
    for chunk in _LABEL_SEP_RE.split(lowered):
        token = _LABEL_KEEP_RE.sub("", chunk)
        if token:
            tokens.append(token)
    return " ".join(tokens)


def parse_pick_spec(text: str, num_candidates: int) -> list[ParsedPart]:
    """Parse the modal text into an ordered list of `ParsedPart`.

    Grammar: one part per non-empty line, each line ``<label>: <n>[, <n> ...]``. The
    label is everything before the FIRST ``:`` (stripped); the remainder holds the
    candidate numbers, separated by commas and/or whitespace. A blank label means the
    whole exercise.

    Validation — raise `PickSpecError` with a clear, user-facing German message on any
    of these violations:
        * at least one part total (the spec must be non-empty);
        * every non-blank line must contain a ``:``;
        * each part must reference at least one number (no empty part);
        * every number must be an integer within ``1..num_candidates``.

    Label canonicalization is delegated to `_canonicalize_label`, so ``"a) b)"``,
    ``"a,b"``, ``"a b"`` and ``"a+b"`` all collapse to ``"a b"`` and a blank label
    stays ``""``.

    Args:
        text: Raw multi-line text from the modal's paragraph `TextInput`.
        num_candidates: How many candidate images exist (the valid upper bound for
            referenced numbers; must be ``>= 1`` for any number to be valid).

    Returns:
        The parsed parts in input order.

    Raises:
        PickSpecError: on any validation failure described above.
    """
    parts: list[ParsedPart] = []

    # Iterate over physical lines, keeping 1-based numbers for friendly error messages.
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            # Blank lines are ignored — users naturally leave gaps between parts.
            continue

        if ":" not in line:
            raise PickSpecError(
                f"Zeile {lineno} hat keinen Doppelpunkt. Erwartet wird "
                f"`Teil: Nummer(n)`, z.B. `a: 2` oder `: 2` (ganze Aufgabe). "
                f"Betroffene Zeile: `{line}`"
            )

        label_part, _, numbers_part = line.partition(":")
        label = _canonicalize_label(label_part)

        # Numbers are comma/space separated. Split on any run of comma/whitespace and
        # keep non-empty tokens.
        raw_numbers = [tok for tok in re.split(r"[\s,]+", numbers_part.strip()) if tok]
        if not raw_numbers:
            shown = label if label else "(ganze Aufgabe)"
            raise PickSpecError(
                f"Teil `{shown}` (Zeile {lineno}) enthaelt keine Nummer. "
                f"Jeder Teil braucht mindestens eine Bild-Nummer, z.B. `a: 2`."
            )

        numbers: list[int] = []
        for tok in raw_numbers:
            try:
                value = int(tok)
            except ValueError:
                raise PickSpecError(
                    f"`{tok}` (Zeile {lineno}) ist keine ganze Zahl. "
                    f"Bitte nur Bild-Nummern angeben."
                )
            if value < 1 or value > num_candidates:
                raise PickSpecError(
                    f"Bild-Nummer {value} (Zeile {lineno}) liegt ausserhalb des "
                    f"gueltigen Bereichs 1..{num_candidates}."
                )
            numbers.append(value)

        parts.append(ParsedPart(label=label, numbers=numbers))

    if not parts:
        raise PickSpecError(
            "Keine Teile angegeben. Schreibe mindestens eine Zeile, z.B. `a: 2` "
            "oder `: 2` fuer die ganze Aufgabe."
        )

    return parts


def merge_consecutive_parts(parts: list[ParsedPart]) -> list[ParsedPart]:
    """Fold consecutive parts that reference the *same* image(s) into one combined part.

    A single uploaded photo frequently covers several *adjacent* sub-parts (a, b, c).
    The natural way to enter that — one line per part with the same image number::

        a: 2
        b: 2
        c: 2

    would otherwise place image #2 three separate times (once per part). This collapses
    any maximal run of *consecutive* parts whose image numbers are identical into a
    single :class:`ParsedPart` whose label concatenates the run's labels, so the shared
    photo is shown once under ``(a) (b) (c)`` — exactly as if the user had written
    ``a b c: 2`` (which canonicalizes to the label ``"a b c"`` and the existing
    combined-photo path).

    The fold is strictly positional, mirroring "chains of *following* parts": only parts
    adjacent in the list merge. If the same image reappears after an interruption::

        a: 2
        b: 2
        c: 7
        d: 2

    the trailing ``d: 2`` starts a fresh run (it is separated from ``a``/``b`` by ``c``),
    so image #2 is repeated for ``d`` — yielding ``(a) (b)`` · ``(c)`` · ``(d)``.

    Merge conditions (both must hold for two adjacent parts to combine):

    * both labels are non-empty — a whole-exercise part (``""``) is never folded into a
      neighbour; and
    * their ``numbers`` lists are equal *including order*, so ``a: 2, 3`` and
      ``b: 3, 2`` (same images, different page order) stay separate.

    Args:
        parts: Parsed parts in input order (typically straight from
            :func:`parse_pick_spec`).

    Returns:
        A new list with consecutive same-image parts merged. Input parts are never
        mutated, and every returned part owns a fresh ``numbers`` list (no aliasing of
        the input), so callers can store/iterate the result freely.
    """
    merged: list[ParsedPart] = []
    for part in parts:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.label
            and part.label
            and previous.numbers == part.numbers
        ):
            # Same image(s) as the run we are building: extend the combined label and
            # keep the shared numbers (the photo is placed/figured once downstream).
            merged[-1] = ParsedPart(
                label=f"{previous.label} {part.label}",
                numbers=list(previous.numbers),
            )
        else:
            # A new run: copy the numbers so a later merge never aliases the input list.
            merged.append(ParsedPart(label=part.label, numbers=list(part.numbers)))
    return merged


# ---------------------------------------------------------------------------
# Discord widgets (require a live discord.py runtime)
# ---------------------------------------------------------------------------

# Discord allows at most 10 embeds in a single message.
MAX_GALLERY_EMBEDS = 10
# `/pick` surfaces at most this many candidates (mirrors the cog cap).
MAX_CANDIDATES = 25
# How long the ephemeral pick view stays interactive before timing out (seconds).
PICK_VIEW_TIMEOUT = 300.0


@dataclass
class Candidate:
    """A numbered candidate image surfaced in the `/pick` gallery.

    Attributes:
        number: 1-based index as shown to the user in the gallery/text list.
        message_id: ID of the message the attachment came from (used by ``/build``
            to re-fetch, since CDN URLs expire).
        attachment_id: ID of the attachment within that message.
        url: Current (expiring) CDN URL, used only for the thumbnail preview.
        filename: Original attachment filename, shown in titles and the text list.
        content_type: Reported MIME type, if any.
    """

    number: int
    message_id: int
    attachment_id: int
    url: str
    filename: str
    content_type: str | None


def build_gallery_embeds(candidates: list[Candidate]) -> list[discord.Embed]:
    """Build up to 10 thumbnail embeds for the candidate gallery.

    Discord caps a message at 10 embeds, but `/pick` may have up to 25 candidates. We
    render the 10 *most recent* candidates (i.e. the tail of the ordered list, which the
    cog supplies oldest-first) as thumbnail embeds. The cog separately lists *all*
    candidates as a numbered text block in the message content so numbers beyond the
    gallery remain referenceable by filename.

    Each embed is titled ``#<n> · <filename>`` and shows the image. We call BOTH
    ``set_image`` (large preview) and ``set_thumbnail`` (compact corner preview) with the
    candidate's URL so the image renders regardless of how the client lays the embed out.

    Args:
        candidates: Ordered candidate list (oldest-first as produced by the cog).

    Returns:
        Between 0 and 10 `discord.Embed` objects, one per shown candidate.
    """
    # Take the last MAX_GALLERY_EMBEDS candidates (most recent) so the freshest uploads
    # are always visible as thumbnails even when more than 10 exist.
    shown = candidates[-MAX_GALLERY_EMBEDS:] if candidates else []
    embeds: list[discord.Embed] = []
    for cand in shown:
        embed = discord.Embed(title=f"#{cand.number} · {cand.filename}")
        embed.set_image(url=cand.url)
        embed.set_thumbnail(url=cand.url)
        embeds.append(embed)
    return embeds


def build_candidate_listing(candidates: list[Candidate]) -> str:
    """Render the numbered text block listing every candidate (up to 25).

    This complements `build_gallery_embeds`: it guarantees every candidate is
    referenceable by number even when more than 10 thumbnails would be needed.

    Args:
        candidates: Ordered candidate list (oldest-first).

    Returns:
        A newline-joined ``"#<n> · <filename>"`` block, or a placeholder line when the
        thread has no candidate images.
    """
    if not candidates:
        return "_Keine Bild-Kandidaten in diesem Thread gefunden._"
    return "\n".join(
        f"#{cand.number} · {cand.filename}"
        for cand in candidates[:MAX_CANDIDATES]
    )


def preview_text(parsed: list[ParsedPart]) -> str:
    """Render a one-line human preview of a parsed selection.

    Format: parts joined by `` · ``. A labelled part renders as
    ``Teil (a): #2`` or ``Teil (a) (b): #5, #6``; an unlabelled (whole-exercise) part
    renders as ``Ganze Aufgabe: #2``.

    Args:
        parsed: The parsed parts (typically from `parse_pick_spec`).

    Returns:
        The preview string (empty string for an empty list).
    """
    chunks: list[str] = []
    for part in parsed:
        numbers = ", ".join(f"#{n}" for n in part.numbers)
        if part.label:
            # "a" -> "(a)", "a b" -> "(a) (b)" — wrap each token in parens.
            label_display = " ".join(f"({tok})" for tok in part.label.split())
            chunks.append(f"Teil {label_display}: {numbers}")
        else:
            chunks.append(f"Ganze Aufgabe: {numbers}")
    return " · ".join(chunks)


class PickModal(discord.ui.Modal):
    """Modal with a single paragraph text field for entering the part spec.

    The submitted text is parsed by `parse_pick_spec`. The modal does not own any
    persistence or view state itself: it delegates the outcome back to its owning
    `PickView` via `PickView.handle_submission`, keeping all flow logic in one place.
    """

    # Paragraph (multi-line) input. One line per part, e.g. "a: 2" / "b: 5, 6" / ": 2".
    spec: discord.ui.TextInput[PickModal] = discord.ui.TextInput(
        label="Teile (eine Zeile pro Teil)",
        style=discord.TextStyle.paragraph,
        placeholder="a: 2\nb: 5, 6\nc: 7\n\n(leeres Label = ganze Aufgabe, z.B. : 2)",
        required=True,
        max_length=2000,
    )

    def __init__(self, view: PickView) -> None:
        super().__init__(title="Teile eingeben")
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Hand the raw text to the owning view, which parses and updates the message."""
        await self._view.handle_submission(interaction, str(self.spec.value))


class PickView(discord.ui.View):
    """Ephemeral view that drives the `/pick` part-selection flow.

    Constructed with the candidate list and an async ``on_confirm`` callback supplied by
    the cog. The view holds the candidate list, the latest successful parse, and renders
    the appropriate buttons for each stage:

        * Stage 1 (initial): only "Teile eingeben" — opens `PickModal`.
        * Stage 2 (after a valid parse): "Teile eingeben" (to re-edit) plus "Bestätigen"
          and "Abbrechen".

    On "Bestätigen" the view awaits ``on_confirm(parsed)`` (which persists the
    selection), then disables itself. On "Abbrechen" it simply disables itself. On
    timeout it disables all items so stale buttons can't be clicked.
    """

    def __init__(
        self,
        candidates: list[Candidate],
        on_confirm: OnConfirm,
        *,
        timeout: float = PICK_VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.candidates: list[Candidate] = candidates
        self._on_confirm: OnConfirm = on_confirm
        # The most recent successful parse; populated on a valid modal submission and
        # consumed by the cog's on_confirm callback.
        self.parsed: list[ParsedPart] | None = None
        # Track the ephemeral message so on_timeout can disable its components.
        self.message: discord.Message | discord.InteractionMessage | None = None
        # Start in stage 1: only the "Teile eingeben" entry button is present.
        self._show_entry_only()

    # -- internal layout helpers ------------------------------------------------

    def _show_entry_only(self) -> None:
        """Reset the view to stage 1: just the "Teile eingeben" button."""
        self.clear_items()
        self.add_item(self._entry_button())

    def _show_confirm_stage(self) -> None:
        """Switch to stage 2: re-edit entry plus Bestätigen / Abbrechen."""
        self.clear_items()
        self.add_item(self._entry_button())
        self.add_item(self._confirm_button())
        self.add_item(self._cancel_button())

    def _entry_button(self) -> discord.ui.Button[PickView]:
        button: discord.ui.Button[PickView] = discord.ui.Button(
            label="Teile eingeben", style=discord.ButtonStyle.primary
        )
        button.callback = self._on_entry  # type: ignore[assignment]
        return button

    def _confirm_button(self) -> discord.ui.Button[PickView]:
        button: discord.ui.Button[PickView] = discord.ui.Button(
            label="Bestätigen", style=discord.ButtonStyle.success
        )
        button.callback = self._on_confirm_click  # type: ignore[assignment]
        return button

    def _cancel_button(self) -> discord.ui.Button[PickView]:
        button: discord.ui.Button[PickView] = discord.ui.Button(
            label="Abbrechen", style=discord.ButtonStyle.secondary
        )
        button.callback = self._on_cancel  # type: ignore[assignment]
        return button

    def _disable_all(self) -> None:
        """Disable every interactive item (used after confirm/cancel/timeout)."""
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    # -- button callbacks -------------------------------------------------------

    async def _on_entry(self, interaction: discord.Interaction) -> None:
        """Open the spec modal. Modals must be the first interaction response."""
        await interaction.response.send_modal(PickModal(self))

    async def handle_submission(
        self, interaction: discord.Interaction, raw_text: str
    ) -> None:
        """Parse the modal text and update the ephemeral message accordingly.

        On a `PickSpecError`, show the message ephemerally (the original view stays
        intact so the user can retry). On success, store the parse, advance to the
        confirm stage, and edit the ephemeral message to show the preview plus the
        confirm/cancel buttons.
        """
        try:
            parsed = parse_pick_spec(raw_text, len(self.candidates))
        except PickSpecError as exc:
            # Surface validation errors ephemerally; the message/view are unchanged.
            if not interaction.response.is_done():
                await interaction.response.send_message(str(exc), ephemeral=True)
            else:
                await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Fold consecutive parts that share the same image(s) into one combined-label
        # part, so a photo covering a, b, c is shown once as "(a) (b) (c)" instead of
        # repeated per part. The preview and the persisted selection both reflect this.
        parsed = merge_consecutive_parts(parsed)

        self.parsed = parsed
        self._show_confirm_stage()
        content = (
            "**Vorschau** — bitte pruefen und bestaetigen:\n"
            f"{preview_text(parsed)}"
        )
        # The modal submission's response edits the underlying ephemeral message.
        if not interaction.response.is_done():
            await interaction.response.edit_message(content=content, view=self)
        else:
            await interaction.edit_original_response(content=content, view=self)

    async def _on_confirm_click(self, interaction: discord.Interaction) -> None:
        """Persist via the cog callback, then disable the view."""
        if self.parsed is None:
            # Defensive: confirm should only be reachable after a successful parse.
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Es liegt keine gueltige Auswahl vor. Bitte zuerst Teile eingeben.",
                    ephemeral=True,
                )
            return

        # Run the cog-supplied persistence callback. If it raises, report ephemerally
        # and leave the view interactive so the user can retry.
        try:
            await self._on_confirm(self.parsed)
        except Exception as exc:  # noqa: BLE001 - surface any save failure to the user
            message = f"Speichern fehlgeschlagen: {exc}"
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
            return

        self._disable_all()
        self.stop()
        confirmed = (
            "✅ Auswahl gespeichert:\n" f"{preview_text(self.parsed)}"
        )
        if not interaction.response.is_done():
            await interaction.response.edit_message(content=confirmed, view=self)
        else:
            await interaction.edit_original_response(content=confirmed, view=self)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """Discard the in-progress parse and disable the view."""
        self._disable_all()
        self.stop()
        if not interaction.response.is_done():
            await interaction.response.edit_message(
                content="Abgebrochen. Es wurde nichts gespeichert.", view=self
            )
        else:
            await interaction.edit_original_response(
                content="Abgebrochen. Es wurde nichts gespeichert.", view=self
            )

    async def on_timeout(self) -> None:
        """Disable all components when the view times out so they can't be clicked."""
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                # The ephemeral message may already be gone; nothing to recover.
                pass
