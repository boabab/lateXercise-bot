"""Slash-command cog for the lateXercise bot: ``/sheet``, ``/pick``, ``/build``.

This module is the orchestration layer that wires the Discord runtime to the pure
helper modules (:mod:`bot.config`, :mod:`bot.store`, :mod:`bot.latex`,
:mod:`bot.images`, :mod:`bot.ui`). It contains *all* of the Discord-specific I/O —
thread creation, history scraping, attachment re-fetching, file uploads — while the
heavy lifting (spec parsing, ``.tex`` rendering, compilation, image placement) lives
in the framework-free modules so it stays unit-testable.

The three commands implement the documented workflow:

* ``/sheet <sheet> <num_exercises>`` — create one public thread per exercise plus a
  hub message linking them, and record the mapping in the store.
* ``/pick`` — run *inside* an exercise thread; gather candidate images from the
  thread history, let the user map parts → image numbers via an ephemeral
  :class:`~bot.ui.PickView`, and persist the resulting selection.
* ``/build <sheet> [skip_missing]`` — gather every exercise's picked images, place
  them under ``<project>/ex<NN>/``, render ``ex<NN>.tex``, compile it to
  ``Group_<group>_Sheet_<NN>.pdf``, and post the PDF (or a log excerpt on failure).

All commands are guild-scoped (the cog is added and synced to ``GUILD_ID`` by
``bot.py``). Every command first runs the :meth:`Exercises._operating_channel` guard so
the bot only operates in the configured operating channels (one per submission group), and
long-running
operations ``defer(thinking=True, ephemeral=True)`` so Discord's 3-second initial
response window is never missed.

See ``data/CONTRACTS.md`` (section "bot/cogs/exercises.py") for the authoritative
contract this implements.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Settings
from ..images import (
    UnsupportedImageError,
    download_and_place,
)
from ..latex import (
    CompileError,
    CompileResult,
    ExerciseDoc,
    FigurePart,
    HeaderOverrides,
    compile_pdf,
    pad_sheet,
    read_group,
    render_tex,
    resolve_compiler,
)
from ..store import ImageRef, Part, Store, ThreadRow
from ..ui import (
    Candidate,
    ParsedPart,
    build_candidate_listing,
    build_gallery_embeds,
    PickView,
    preview_text,
)

logger = logging.getLogger(__name__)

# --- domain limits (mirror the contract / PLAN) ------------------------------------
# Sheet numbers are zero-padded to two digits, so the sensible inclusive range is 1..99.
_MIN_SHEET = 1
_MAX_SHEET = 99
# A study sheet realistically has between 1 and 25 exercises; 25 also matches the
# `/pick` candidate cap so the surfaces stay consistent.
_MIN_EXERCISES = 1
_MAX_EXERCISES = 25
# Discord auto-archive duration in minutes: 10080 == 7 days, the longest option. We use
# the maximum so threads survive between collecting photos and running `/build`.
_AUTO_ARCHIVE_MINUTES = 10080
# How many messages of thread history `/pick` scans for candidate attachments. Threads
# are small (one exercise's discussion), so a generous bound is cheap and safe.
_HISTORY_LIMIT = 500
# Maximum number of candidate images `/pick` will surface and number (matches ui.py).
_MAX_CANDIDATES = 25
# Discord's "file too large" upload error maps to HTTP 413; surfaced on `/build`.
_HTTP_REQUEST_ENTITY_TOO_LARGE = 413
# Image file extensions accepted as a fallback when an attachment has no/odd
# ``content_type``. Magic-byte validation still happens later in ``download_and_place``.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".pdf"}
)
# Separator for the ;-delimited author list in /config and the AUTHORS env default.
_AUTHORS_SEP = ";"
# A group number ends up in the PDF filename (Group_<group>_Sheet_NN.pdf), so it must
# stay filesystem-safe: letters/digits only, kept short.
_GROUP_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _authors_to_list(raw: str | None) -> list[str] | None:
    """Split a ``;``-separated author string into a list of lines, or ``None``."""
    if not raw:
        return None
    names = [part.strip() for part in raw.split(_AUTHORS_SEP) if part.strip()]
    return names or None


def _safe_group(group: str) -> str:
    """Sanitise a group value for use in the output filename (jobname)."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", group)
    return cleaned or "000"


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    """Return ``True`` if *attachment* looks like a candidate image.

    A photo qualifies if its reported ``content_type`` starts with ``image/`` OR its
    filename ends with a known image/PDF extension. This is a *coarse* pre-filter for
    `/pick` numbering only; the authoritative format check (magic bytes) happens later
    in :func:`bot.images.download_and_place`, which rejects anything pdflatex cannot
    embed. PDFs are included because the LaTeX pipeline can embed them directly.
    """
    content_type = attachment.content_type
    if content_type and content_type.startswith("image/"):
        return True
    # Fall back to the filename extension when content_type is missing or generic
    # (e.g. application/octet-stream from some clients).
    suffix = attachment.filename.rsplit(".", 1)
    if len(suffix) == 2 and f".{suffix[1].lower()}" in _IMAGE_EXTENSIONS:
        return True
    return False


class Exercises(commands.Cog):
    """Cog exposing the ``/sheet``, ``/pick`` and ``/build`` slash commands.

    Construct with the live ``bot``, the validated :class:`~bot.config.Settings`, and
    an initialised :class:`~bot.store.Store`. ``bot.py`` adds this cog and copies its
    commands guild-scoped to ``GUILD_ID`` for instant availability.
    """

    def __init__(
        self, bot: commands.Bot, settings: Settings, store: Store
    ) -> None:
        """Store the runtime collaborators. No I/O happens here."""
        self.bot = bot
        self.settings = settings
        self.store = store
        # Serializes /build across ALL channels: builds share the on-disk ex<NN>/ scratch
        # folder (and root-level aux files), so two channels building the same sheet number
        # must not run concurrently. Output PDFs are named per group, so they never collide.
        self._build_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Shared guards / helpers
    # ------------------------------------------------------------------

    async def _operating_channel(self, interaction: discord.Interaction) -> int | None:
        """Guard + resolver: return the *operating channel id* for this interaction.

        Each configured ``ALLOWED_CHANNEL_IDS`` channel is an independent submission group.
        A command runs either directly in such a channel (``/sheet``, ``/build``, ``/config``)
        or inside one of its threads (``/pick``). This returns the operating channel id — the
        channel itself, or a thread's ``parent_id`` — which is the key everything is stored
        under. If the interaction is not in any allowed channel (or a thread under one), it
        replies ephemerally and returns ``None`` so the caller aborts.
        """
        allowed = self.settings.allowed_channel_ids
        channel = interaction.channel

        if channel is not None:
            if channel.id in allowed:
                return channel.id
            if isinstance(channel, discord.Thread) and channel.parent_id in allowed:
                # A thread whose parent is an operating channel belongs to that group.
                return channel.parent_id

        # Refuse ephemerally. Use the response if still un-acknowledged, else a followup
        # (defensive — guards run before defer, so response should be fresh).
        mentions = ", ".join(f"<#{cid}>" for cid in allowed)
        await self._reply_ephemeral(
            interaction,
            f"This command is only allowed in the bot's working channels ({mentions}) "
            "or their threads.",
        )
        return None

    @staticmethod
    async def _reply_ephemeral(
        interaction: discord.Interaction, content: str
    ) -> None:
        """Send *content* ephemerally, whether or not the interaction was deferred.

        After ``defer()`` the initial response is "done", so we must use the followup
        webhook; before it, we use the initial response. This helper hides that branch
        from every call site.
        """
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _resolve_overrides(self, channel_id: int) -> HeaderOverrides:
        """Merge the per-channel ``/config`` config over the ``.env`` defaults.

        Precedence per field: DB override (``/config`` for this channel) > ``.env`` default >
        unset (``None`` → ``exercise.sty``'s own default is left in place by ``render_tex``).
        The ``;``-separated author string is split into a list of lines.
        """
        cfg = await self.store.get_channel_config(channel_id)
        s = self.settings
        return HeaderOverrides(
            group_number=cfg.group_number or s.group_number,
            course=cfg.course or s.course,
            tutorium=cfg.tutorium or s.tutorium,
            authors=_authors_to_list(cfg.authors or s.authors),
        )

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    @app_commands.command(
        name="help",
        description="Shows how to use the lateXercise bot (workflow & commands).",
    )
    async def help(self, interaction: discord.Interaction) -> None:
        """Show an ephemeral usage guide describing the full workflow and every command.

        Intentionally has no channel guard — anyone may ask for help anywhere in the
        guild — and replies ephemerally so it never clutters the channel.
        """
        allowed = self.settings.allowed_channel_ids
        channels_str = ", ".join(f"<#{cid}>" for cid in allowed)
        # Each channel is its own submission group with its own /config + sheets.
        channels_line = (
            f"Work happens in {channels_str} and its exercise threads."
            if len(allowed) == 1
            else (
                f"Work happens in the channels {channels_str} (each channel is its own "
                "submission group with its own `/config`) and their exercise threads."
            )
        )
        embed = discord.Embed(
            title="📄 lateXercise bot — Help",
            description=(
                "The bot turns your solution photos into a ready-to-submit PDF.\n"
                f"{channels_line}\n\n"
                "**Workflow:** `/sheet` → upload photos into the threads → `/pick` → `/build`."
            ),
            color=0x00549F,  # RWTH-blue-ish, matching the sheet styling
        )
        embed.add_field(
            name="1) /sheet <sheet> <num_exercises>",
            value=(
                "Creates one public thread per exercise and posts an overview. "
                "Example: `/sheet 6 3` → threads *Sheet 06 · Exercise 1…3*.\n"
                "Then upload your solution photos (PNG/JPEG/PDF) into the matching "
                "exercise thread and discuss there."
            ),
            inline=False,
        )
        embed.add_field(
            name="2) /pick  — in the exercise thread",
            value=(
                "Choose the winning images **part by part**. Click **Enter parts** "
                "and write `part: number(s)` per line:\n"
                "```\na: 2\nb: 5, 6\nc: 7\n```\n"
                "• `: 2` = whole exercise (no part letter)\n"
                "• `a b: 3` = one photo covers part a **and** b\n"
                "• the same number on **consecutive** parts (`a: 2`, `b: 2`) "
                "is auto-merged into `(a) (b)` — the image appears only "
                "once (an interruption => it repeats)\n"
                "• multiple numbers = multiple pages (order preserved)\n"
                "Running `/pick` again replaces that exercise's selection."
            ),
            inline=False,
        )
        embed.add_field(
            name="3) /build <sheet> [skip_missing]",
            value=(
                "Builds the PDF and posts it in the channel. Example: `/build 6` → "
                "`Group_<no>_Sheet_06.pdf`.\n"
                "If an exercise has no selection, the command aborts and lists the "
                "gaps — use `skip_missing: true` to skip them."
            ),
            inline=False,
        )
        embed.add_field(
            name="/config  — customise header & filename",
            value=(
                "Set group number, course, tutorial and author list, e.g.\n"
                "`/config group:123 tutorial:\"Tutorial 12\" "
                "authors:\"Anna, 111; Ben, 222\"`.\n"
                "With no arguments `/config` shows the current configuration; "
                "`/config reset:true` resets it."
            ),
            inline=False,
        )
        embed.add_field(
            name="Notes",
            value=(
                "• Supported image formats: **PNG, JPEG, PDF** "
                "(convert HEIC/WEBP first).\n"
                "• `/sheet` and `/build` run **in the working channel**, "
                "`/pick` **in the exercise thread**."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /config
    # ------------------------------------------------------------------

    @app_commands.command(
        name="config",
        description="Show/change the PDF's group number, course, tutorial and authors.",
    )
    @app_commands.describe(
        group="Group number (letters/digits only) — appears in the filename.",
        course="Course / lecture name for the header, e.g. 'My Course 2026'.",
        tutorial="Tutorial / group label for the header, e.g. 'Tutorial 12'.",
        authors="Authors, separated by ';', e.g. 'Anna Sample, 111111; Ben Example, 222222'.",
        reset="Delete all stored values (back to .env / exercise.sty).",
    )
    async def config(
        self,
        interaction: discord.Interaction,
        group: str | None = None,
        course: str | None = None,
        tutorial: str | None = None,
        authors: str | None = None,
        reset: bool = False,
    ) -> None:
        """View or change **this channel's** header overrides used by ``/build``.

        Each operating channel is its own submission group with its own configuration. With
        no arguments it shows the current effective configuration for the channel it is run
        in and where each value comes from (``/config`` override, ``.env`` default, or
        ``exercise.sty``). Any provided argument updates that field (stored per channel);
        ``reset:true`` clears this channel's stored overrides. Values feed the generated
        ``.tex`` via ``\\renewcommand`` and the PDF filename, so ``exercise.sty`` is never
        edited.
        """
        channel_id = await self._operating_channel(interaction)
        if channel_id is None:
            return

        # --- reset path: clear this channel's stored overrides ---------------------
        if reset:
            await self.store.set_channel_config(
                channel_id, group_number=None, course=None, tutorium=None, authors=None
            )
            await self._reply_ephemeral(
                interaction,
                "This channel's configuration has been reset — the "
                "`.env` / `exercise.sty` defaults apply again.",
            )
            return

        # --- validate the group number (it lands in the PDF filename) --------------
        if group is not None and group.strip() and not _GROUP_RE.match(group.strip()):
            await self._reply_ephemeral(
                interaction,
                "Invalid group number. Only letters/digits are allowed "
                "(max. 10 characters), since it appears in the filename.",
            )
            return

        # --- set path: update only the fields the user provided --------------------
        updates: dict[str, str | None] = {}
        if group is not None:
            updates["group_number"] = group
        if course is not None:
            updates["course"] = course
        if tutorial is not None:
            updates["tutorium"] = tutorial
        if authors is not None:
            updates["authors"] = authors
        if updates:
            await self.store.set_channel_config(channel_id, **updates)

        # --- show the resulting effective configuration ----------------------------
        cfg = await self.store.get_channel_config(channel_id)
        s = self.settings

        def _source(db_val: str | None, env_val: str | None) -> str:
            if db_val:
                return "/config"
            if env_val:
                return ".env"
            return "exercise.sty (default)"

        eff_group = cfg.group_number or s.group_number or read_group(
            self.settings.latex_project_dir
        )
        eff_authors_raw = cfg.authors or s.authors
        eff_authors = (
            "; ".join(_authors_to_list(eff_authors_raw) or [])
            if eff_authors_raw
            else "(default from exercise.sty)"
        )

        embed = discord.Embed(
            title="⚙️ lateXercise bot — Configuration",
            description=(
                f"Configuration for this channel (<#{channel_id}>). These values go into "
                "the header and filename of the built PDF. "
                "Order: `/config` > `.env` > `exercise.sty`."
                + ("\n\n*(Updated.)*" if updates else "")
            ),
            color=0x00549F,
        )
        embed.add_field(
            name="Group number",
            value=f"`{eff_group}`  · Source: {_source(cfg.group_number, s.group_number)}",
            inline=False,
        )
        embed.add_field(
            name="Course",
            value=(
                f"{cfg.course or s.course or '(default from exercise.sty)'}  "
                f"· Source: {_source(cfg.course, s.course)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Tutorial",
            value=(
                f"{cfg.tutorium or s.tutorium or '(default from exercise.sty)'}  "
                f"· Source: {_source(cfg.tutorium, s.tutorium)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Authors",
            value=f"{eff_authors}  · Source: {_source(cfg.authors, s.authors)}",
            inline=False,
        )
        embed.set_footer(
            text="Change: /config group:… course:… tutorial:… authors:'A, 1; B, 2'  |  "
            "Reset: /config reset:true"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /sheet
    # ------------------------------------------------------------------

    @app_commands.command(
        name="sheet",
        description="Creates one thread per exercise for a problem sheet.",
    )
    @app_commands.describe(
        sheet="Sheet number (1..99), shown as two digits (e.g. 6 -> 06).",
        num_exercises="Number of exercises (1..25).",
    )
    async def sheet(
        self,
        interaction: discord.Interaction,
        sheet: int,
        num_exercises: int,
    ) -> None:
        """Create one public thread per exercise plus a hub message linking them.

        Must run directly in the operating channel (a text channel, *not* a thread).
        Validates the ranges, refuses if the sheet already exists (pointing at the
        existing threads), creates ``num_exercises`` public threads named
        ``"Sheet <NN> · Exercise <i>"``, records each in the store, posts a hub message,
        records its id, and replies with a summary.
        """
        if await self._operating_channel(interaction) is None:
            return

        # --- input validation (cheap, before deferring) ---------------------------
        if not (_MIN_SHEET <= sheet <= _MAX_SHEET):
            await self._reply_ephemeral(
                interaction,
                f"Invalid sheet number {sheet}. Allowed range is {_MIN_SHEET}..{_MAX_SHEET}.",
            )
            return
        if not (_MIN_EXERCISES <= num_exercises <= _MAX_EXERCISES):
            await self._reply_ephemeral(
                interaction,
                f"Invalid exercise count {num_exercises}. "
                f"Allowed range is {_MIN_EXERCISES}..{_MAX_EXERCISES}.",
            )
            return

        # `/sheet` must be issued in the operating *channel*, never inside a thread, so
        # the created threads attach to the right parent. The operating-channel guard
        # above already confirmed this channel is one of ALLOWED_CHANNEL_IDS.
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self._reply_ephemeral(
                interaction,
                "`/sheet` must be run directly in a working channel, not in a thread.",
            )
            return

        # Thread creation + several network round-trips: defer so we don't time out.
        await interaction.response.defer(thinking=True, ephemeral=True)

        channel_id = channel.id  # this channel == the submission group everything keys on
        padded = pad_sheet(sheet)

        # --- duplicate guard: try to create the sheet row first --------------------
        # create_sheet returns False if (channel, sheet) already exists. We create the row
        # BEFORE threads so a duplicate `/sheet` cannot spawn a second set of threads.
        created = await self.store.create_sheet(channel_id, sheet, num_exercises)
        if not created:
            existing = await self.store.get_threads(channel_id, sheet)
            if existing:
                links = ", ".join(f"<#{row.thread_id}>" for row in existing)
                detail = f" Existing threads: {links}"
            else:
                detail = ""
            await interaction.followup.send(
                f"Sheet {padded} already exists.{detail}",
                ephemeral=True,
            )
            return

        # --- create the threads ----------------------------------------------------
        created_threads: list[discord.Thread] = []
        for index in range(1, num_exercises + 1):
            thread_name = f"Sheet {padded} · Exercise {index}"
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=_AUTO_ARCHIVE_MINUTES,
                )
            except discord.HTTPException as exc:
                # Partial failure: surface what happened. The sheet row exists and any
                # threads already created are recorded, so a follow-up `/sheet` is
                # refused; the operator can inspect/clean up via the listed threads.
                logger.exception("Thread creation failed for %s", thread_name)
                links = ", ".join(f"<#{t.id}>" for t in created_threads) or "none"
                await interaction.followup.send(
                    f"Thread creation failed at exercise {index}: {exc}. "
                    f"Already created: {links}.",
                    ephemeral=True,
                )
                return

            created_threads.append(thread)
            await self.store.add_thread(
                channel_id, sheet, index, thread.id, thread_name
            )

        # --- post the hub message linking every thread -----------------------------
        thread_lines = "\n".join(
            f"- Exercise {i}: {thread.mention}"
            for i, thread in enumerate(created_threads, start=1)
        )
        hub_content = (
            f"**Sheet {padded}** — {num_exercises} exercise(s).\n"
            "Upload your solution photos into the matching thread and use `/pick` there "
            "to choose the winning images. Then `/build` in the working channel.\n"
            f"{thread_lines}"
        )
        hub_message = await channel.send(hub_content)
        await self.store.set_hub_message(channel_id, sheet, hub_message.id)

        # --- ephemeral summary back to the invoker ---------------------------------
        await interaction.followup.send(
            f"Sheet {padded} created: {num_exercises} thread(s) created and a "
            f"hub post posted.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /pick
    # ------------------------------------------------------------------

    @app_commands.command(
        name="pick",
        description="Choose the winning images per part in the current exercise thread.",
    )
    async def pick(self, interaction: discord.Interaction) -> None:
        """Select the winning image(s) for each part of the current exercise thread.

        Must run *inside* an exercise thread the bot created (located via
        :meth:`Store.find_thread`). Reads the thread history oldest-first for stable
        numbering, filters to image attachments, caps at 25 candidates, shows a numbered
        gallery + text list with a :class:`~bot.ui.PickView`, and on confirm maps the
        chosen numbers to :class:`~bot.store.Part`/:class:`~bot.store.ImageRef` and calls
        :meth:`Store.replace_selection`. Optionally posts a public confirmation.
        """
        if await self._operating_channel(interaction) is None:
            return

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await self._reply_ephemeral(
                interaction,
                "`/pick` must be run **inside** an exercise thread.",
            )
            return

        # Locate which (channel, sheet, exercise) this thread maps to. The mapping was stored
        # at creation, so renamed threads still resolve correctly. Thread ids are globally
        # unique, so no channel argument is needed.
        thread_row = await self.store.find_thread(channel.id)
        if thread_row is None:
            await self._reply_ephemeral(
                interaction,
                "This thread doesn't belong to a known exercise. "
                "Please run `/pick` in a thread created with `/sheet`.",
            )
            return

        # Reading history is a network walk; defer so we keep the interaction alive.
        await interaction.response.defer(thinking=True, ephemeral=True)

        # --- gather candidate images, OLDEST-FIRST for stable numbering ------------
        # We number candidates 1..N in chronological (oldest-first) order so the numbers
        # are stable: a later upload always gets a higher number, and re-running `/pick`
        # yields the same numbering for already-present images.
        candidates: list[Candidate] = []
        number = 0
        async for message in channel.history(
            limit=_HISTORY_LIMIT, oldest_first=True
        ):
            for attachment in message.attachments:
                if not _is_image_attachment(attachment):
                    continue
                number += 1
                candidates.append(
                    Candidate(
                        number=number,
                        message_id=message.id,
                        attachment_id=attachment.id,
                        url=attachment.url,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                    )
                )
                if number >= _MAX_CANDIDATES:
                    break
            if number >= _MAX_CANDIDATES:
                break

        if not candidates:
            await interaction.followup.send(
                "No image candidates found in this thread. "
                "Upload solution photos (PNG/JPEG/PDF) first.",
                ephemeral=True,
            )
            return

        # Capture the ids we need inside the confirm closure (avoid late-binding traps).
        # The selection is stored under the thread's recorded operating channel.
        channel_id = thread_row.channel_id
        sheet = thread_row.sheet
        exercise_index = thread_row.exercise_index
        # Map candidate number -> Candidate for O(1) lookup during confirmation.
        by_number: dict[int, Candidate] = {c.number: c for c in candidates}

        async def on_confirm(parsed: list[ParsedPart]) -> None:
            """Persist the user's selection, then optionally announce it publicly.

            Each :class:`ParsedPart` references candidate numbers (validated against the
            candidate count by the parser); we map them back to :class:`ImageRef` in the
            given order (preserving page order) and group them into ordered
            :class:`Part` objects for :meth:`Store.replace_selection`.
            """
            store_parts: list[Part] = []
            for parsed_part in parsed:
                images: list[ImageRef] = []
                # img_position is 1-based page order within this part, in input order.
                for position, num in enumerate(parsed_part.numbers, start=1):
                    candidate = by_number[num]  # parser guarantees num is in range
                    images.append(
                        ImageRef(
                            img_position=position,
                            message_id=candidate.message_id,
                            attachment_id=candidate.attachment_id,
                            url=candidate.url,
                            filename=candidate.filename,
                            content_type=candidate.content_type,
                        )
                    )
                store_parts.append(Part(label=parsed_part.label, images=images))

            await self.store.replace_selection(
                channel_id, sheet, exercise_index, store_parts
            )

            # Optional public confirmation in the thread so the group sees the decision.
            if self.settings.announce_picks:
                summary = preview_text(parsed)
                try:
                    await channel.send(
                        f"Selection for exercise {exercise_index} updated: {summary}"
                    )
                except discord.HTTPException:
                    # A failed public announcement must not undo the saved selection.
                    logger.warning(
                        "Failed to post public pick confirmation in thread %s",
                        channel.id,
                        exc_info=True,
                    )

        # --- build and show the ephemeral pick UI ----------------------------------
        view = PickView(candidates, on_confirm)
        listing = build_candidate_listing(candidates)
        embeds = build_gallery_embeds(candidates)
        content = (
            f"**Exercise {exercise_index}** — {len(candidates)} image candidate(s) "
            "found (numbered oldest-first):\n"
            f"{listing}\n\n"
            "Click **Enter parts** and give `part: number(s)` per line, "
            "e.g. `a: 2` or `: 2` for the whole exercise."
        )
        # Discord caps a message at 10 embeds; build_gallery_embeds already trims to the
        # 10 most recent candidates, while `listing` references all of them by number.
        sent = await interaction.followup.send(
            content=content,
            embeds=embeds,
            view=view,
            ephemeral=True,
            wait=True,
        )
        # Let the view edit/disable its own message on timeout.
        view.message = sent

    # ------------------------------------------------------------------
    # /build
    # ------------------------------------------------------------------

    @app_commands.command(
        name="build",
        description="Builds the PDF for a sheet from the selected images.",
    )
    @app_commands.describe(
        sheet="Sheet number (1..99) of the sheet to build.",
        skip_missing="Skip exercises without a selection instead of aborting.",
    )
    async def build(
        self,
        interaction: discord.Interaction,
        sheet: int,
        skip_missing: bool = False,
    ) -> None:
        """Assemble, render and compile a sheet's PDF, then post it.

        Must run in the operating channel. Preflights the LaTeX compiler (aborting with
        install hints if none is found), verifies the sheet exists, computes gaps
        (exercises without a selection) and aborts listing them unless ``skip_missing``.
        For each selected exercise it re-fetches every message by id (unarchiving the
        thread if needed), places each attachment under ``<project>/ex<NN>/`` (wiped and
        recreated first), renders ``ex<NN>.tex``, compiles
        ``Group_<group>_Sheet_<NN>.pdf`` and posts it — or a log excerpt on failure.
        """
        if await self._operating_channel(interaction) is None:
            return

        # --- input validation (parity with /sheet; defense-in-depth for the ex<NN>
        #     folder wipe below, which must only ever target a valid two-digit folder) ---
        if not (_MIN_SHEET <= sheet <= _MAX_SHEET):
            await self._reply_ephemeral(
                interaction,
                f"Invalid sheet number {sheet}. Allowed range is {_MIN_SHEET}..{_MAX_SHEET}.",
            )
            return

        # `/build` runs in the operating channel itself, not in a thread. The guard above
        # confirmed it is one of ALLOWED_CHANNEL_IDS; that channel is the submission group.
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self._reply_ephemeral(
                interaction,
                "`/build` must be run directly in a working channel, not in a thread.",
            )
            return

        # --- preflight: a usable LaTeX compiler must exist -------------------------
        # Done before deferring so a misconfigured host fails fast with clear hints.
        try:
            resolve_compiler(self.settings.tex_cmd)
        except CompileError as exc:
            await self._reply_ephemeral(interaction, str(exc))
            return

        channel_id = channel.id  # this channel == the submission group everything keys on

        # --- the sheet must exist --------------------------------------------------
        sheet_row = await self.store.get_sheet(channel_id, sheet)
        if sheet_row is None:
            await self._reply_ephemeral(
                interaction,
                f"Sheet {pad_sheet(sheet)} does not exist. "
                "Create it first with `/sheet`.",
            )
            return

        # Compilation + downloads take a while: defer (and keep it ephemeral until we
        # have a result to post publicly).
        await interaction.response.defer(thinking=True, ephemeral=True)

        padded = pad_sheet(sheet)
        project_dir = self.settings.latex_project_dir
        num_exercises = sheet_row.num_exercises

        # --- compute gaps: exercises in 1..num_exercises with NO selection ----------
        selections = await self.store.get_all_selections(channel_id, sheet)
        gaps = [
            index
            for index in range(1, num_exercises + 1)
            if index not in selections
        ]
        if gaps and not skip_missing:
            gap_list = ", ".join(str(g) for g in gaps)
            await interaction.followup.send(
                f"A selection is missing for exercise(s) {gap_list}. "
                "Use `/pick` in the respective threads, or run `/build` with "
                "`skip_missing: True` to skip them.",
                ephemeral=True,
            )
            return

        # Map exercise_index -> its thread row so we can re-fetch attachments.
        thread_rows = await self.store.get_threads(channel_id, sheet)
        thread_by_index = {row.exercise_index: row for row in thread_rows}

        # Resolve this channel's header overrides (group/course/tutorium/authors): /config
        # wins over .env, which wins over exercise.sty. The group number drives both the
        # \ExerciseGroup override and the output filename (sanitised to stay file-safe).
        overrides = await self._resolve_overrides(channel_id)
        group = _safe_group(overrides.group_number or read_group(project_dir))

        # --- assemble images + .tex and compile, serialized across channels --------
        # Builds share the on-disk ex<NN>/ scratch folder and the root-level aux files, so
        # two channels (or a double-invoke) building the same sheet must not run at once. The
        # work lives in a helper run under _build_lock; it replies on error and returns None.
        rel_dir = f"ex{padded}"
        dest_dir = project_dir / rel_dir
        jobname = f"Group_{group}_Sheet_{padded}"
        async with self._build_lock:
            result = await self._assemble_and_compile(
                interaction,
                sheet=sheet,
                padded=padded,
                rel_dir=rel_dir,
                dest_dir=dest_dir,
                project_dir=project_dir,
                num_exercises=num_exercises,
                selections=selections,
                thread_by_index=thread_by_index,
                overrides=overrides,
                jobname=jobname,
                skip_missing=skip_missing,
            )
        if result is None:
            return  # the helper already replied with the specific error

        if not result.ok or result.pdf_path is None:
            # Surface the trimmed log excerpt in a fenced code block for debugging.
            excerpt = result.log_excerpt or "(no log available)"
            await interaction.followup.send(
                f"LaTeX compilation failed (code {result.returncode}"
                f"{', timeout' if result.timed_out else ''}).\n"
                f"```\n{excerpt}\n```",
                ephemeral=True,
            )
            return

        # --- success: post the PDF publicly in the channel -------------------------
        skipped_note = ""
        if skip_missing and gaps:
            skipped_note = (
                f" (skipped: exercise(s) {', '.join(str(g) for g in gaps)})"
            )
        try:
            # Post the PDF publicly via channel.send (not the ephemeral-deferred followup) so the
            # whole group sees and can download it — mirrors how /sheet posts its hub message.
            # This avoids any dependency on followup ephemerality semantics after an ephemeral defer.
            await channel.send(
                content=f"**Sheet {padded}** is ready: `{jobname}.pdf`{skipped_note}",
                file=discord.File(str(result.pdf_path), filename=f"{jobname}.pdf"),
            )
        except discord.HTTPException as exc:
            if exc.status == _HTTP_REQUEST_ENTITY_TOO_LARGE:
                # Discord rejected the upload as too large (8/25/50 MB depending on tier).
                await interaction.followup.send(
                    f"The finished PDF `{jobname}.pdf` is too large for the Discord upload. "
                    "Reduce the image resolution (e.g. set `DOWNSCALE_MAX_PX`) and "
                    "build again.",
                    ephemeral=True,
                )
            else:
                logger.exception("Failed to upload built PDF for sheet %s", sheet)
                await interaction.followup.send(
                    f"PDF upload failed: {exc}",
                    ephemeral=True,
                )
            return

        # Confirm to the invoker (ephemeral) and resolve the lingering "thinking" state.
        await interaction.followup.send(
            f"Done — `{jobname}.pdf` was posted in the channel.{skipped_note}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /build helpers
    # ------------------------------------------------------------------

    async def _assemble_and_compile(
        self,
        interaction: discord.Interaction,
        *,
        sheet: int,
        padded: str,
        rel_dir: str,
        dest_dir: Path,
        project_dir: Path,
        num_exercises: int,
        selections: dict[int, list[Part]],
        thread_by_index: dict[int, ThreadRow],
        overrides: HeaderOverrides,
        jobname: str,
        skip_missing: bool,
    ) -> CompileResult | None:
        """Wipe ex<NN>/, download the picked images, render ex<NN>.tex, and compile it.

        Runs under the caller's ``_build_lock`` (it touches the shared ex<NN>/ scratch folder
        and the root-level aux files). On any preparation/abort path it sends the specific
        ephemeral error itself and returns ``None``; on a compile attempt it returns the
        :class:`~bot.latex.CompileResult` (which may itself be a failure). The caller posts
        the PDF (or the log excerpt) based on the returned result.
        """
        # --- wipe & recreate the dedicated output folder ---------------------------
        # We only ever touch our own zero-padded ex<NN>/ folder, never single-digit ex1/ex2/… a user may keep.
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            await interaction.followup.send(
                f"Could not prepare the output directory `{rel_dir}`: {exc}.",
                ephemeral=True,
            )
            return None

        # --- per exercise: download images, build FigureParts ----------------------
        exercise_docs: list[ExerciseDoc] = []
        # Cache resolved (unarchived) thread channels and fetched messages within this build.
        resolved_threads: dict[int, discord.Thread] = {}
        message_cache: dict[int, discord.Message] = {}

        try:
            for index in range(1, num_exercises + 1):
                parts = selections.get(index)
                if not parts:
                    # No selection: only reachable when skip_missing is True. render_tex omits it.
                    continue

                thread_row = thread_by_index.get(index)
                if thread_row is None:
                    # Selection exists but the thread mapping is gone (data drift).
                    if skip_missing:
                        logger.warning(
                            "No thread mapping for exercise %s on sheet %s; skipping.",
                            index,
                            sheet,
                        )
                        continue
                    await interaction.followup.send(
                        f"No thread found for exercise {index}, but a selection "
                        "exists. Please recreate the sheet with `/sheet` or "
                        "run `/build` with `skip_missing: True`.",
                        ephemeral=True,
                    )
                    return None

                thread = await self._resolve_thread(
                    thread_row.thread_id, resolved_threads
                )
                if thread is None:
                    if skip_missing:
                        logger.warning(
                            "Thread %s for exercise %s is unreachable; skipping.",
                            thread_row.thread_id,
                            index,
                        )
                        continue
                    await interaction.followup.send(
                        f"The thread for exercise {index} is unreachable "
                        "(deleted?). Please recreate it or use `skip_missing: True`.",
                        ephemeral=True,
                    )
                    return None

                figure_parts = await self._build_exercise_parts(
                    parts=parts,
                    thread=thread,
                    dest_dir=dest_dir,
                    rel_dir=rel_dir,
                    exercise_index=index,
                    message_cache=message_cache,
                )
                exercise_docs.append(ExerciseDoc(index=index, parts=figure_parts))
        except UnsupportedImageError as exc:
            # A picked image is in a format pdflatex cannot embed (HEIC/webp/...).
            await interaction.followup.send(f"Build aborted: {exc}", ephemeral=True)
            return None
        except discord.NotFound as exc:
            # A referenced message/attachment no longer exists (deleted upload).
            await interaction.followup.send(
                f"Build aborted: a selected message/attachment no longer "
                f"exists ({exc}). Please re-run `/pick` for the affected exercise.",
                ephemeral=True,
            )
            return None

        if not exercise_docs:
            await interaction.followup.send(
                "Nothing to build — no exercise has a selection.",
                ephemeral=True,
            )
            return None

        # --- render the .tex and write it into the output folder -------------------
        tex_source = render_tex(sheet, exercise_docs, overrides)
        tex_path = dest_dir / f"ex{padded}.tex"
        try:
            tex_path.write_text(tex_source, encoding="utf-8")
        except OSError as exc:
            await interaction.followup.send(
                f"Could not write `{rel_dir}/ex{padded}.tex`: {exc}.",
                ephemeral=True,
            )
            return None

        # --- compile ---------------------------------------------------------------
        try:
            return await compile_pdf(
                project_dir=project_dir,
                tex_rel_path=f"{rel_dir}/ex{padded}.tex",
                jobname=jobname,
                tex_cmd=self.settings.tex_cmd,
            )
        except CompileError as exc:
            # Compiler vanished between preflight and now (unlikely) — same hint message.
            await interaction.followup.send(str(exc), ephemeral=True)
            return None

    async def _resolve_thread(
        self, thread_id: int, cache: dict[int, discord.Thread]
    ) -> discord.Thread | None:
        """Resolve a thread by id, unarchiving it if necessary, with caching.

        ``/build`` must re-fetch messages from each exercise thread, but a thread may
        have auto-archived since `/pick`. We fetch it via ``bot.fetch_channel`` (which
        works for archived threads), and if it is archived we unarchive it via
        ``thread.edit(archived=False)`` so ``fetch_message`` works. Returns ``None`` if
        the thread cannot be resolved (e.g. deleted).
        """
        if thread_id in cache:
            return cache[thread_id]

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Could not fetch thread %s for build.", thread_id, exc_info=True)
            return None

        if not isinstance(fetched, discord.Thread):
            # Defensive: the stored id should always be a thread.
            logger.warning("Channel %s is not a Thread; skipping.", thread_id)
            return None

        if fetched.archived:
            # Unarchive so we can read message history / fetch individual messages.
            # edit() returns a fresh Thread with refreshed state; keep that one.
            try:
                fetched = await fetched.edit(archived=False)
            except discord.HTTPException:
                logger.warning(
                    "Could not unarchive thread %s; fetch may still work.",
                    thread_id,
                    exc_info=True,
                )

        cache[thread_id] = fetched
        return fetched

    async def _build_exercise_parts(
        self,
        *,
        parts: list[Part],
        thread: discord.Thread,
        dest_dir: Path,
        rel_dir: str,
        exercise_index: int,
        message_cache: dict[int, discord.Message],
    ) -> list[FigurePart]:
        """Download every image of an exercise's parts and return ordered FigureParts.

        For each :class:`Part`, each :class:`ImageRef` is resolved by re-fetching its
        message (CDN URLs expire, so we never trust the stored ``url``), locating the
        attachment by id, and placing it via :func:`bot.images.download_and_place` into
        ``dest_dir`` with the per-page ``page`` index. The resulting root-relative paths
        are collected into a :class:`~bot.latex.FigurePart` per part, preserving order.

        Raises:
            UnsupportedImageError: propagated from ``download_and_place`` for a format
                pdflatex cannot embed (caught and reported by the caller).
            discord.NotFound: if a referenced message/attachment no longer exists.
        """
        figure_parts: list[FigurePart] = []
        for part in parts:
            image_paths: list[str] = []
            # ``page`` is the 1-based page index within this part (one figure per page).
            for page, image_ref in enumerate(part.images, start=1):
                message = await self._fetch_message(
                    thread, image_ref.message_id, message_cache
                )
                attachment = self._find_attachment(message, image_ref.attachment_id)
                if attachment is None:
                    # The message exists but the specific attachment is gone.
                    raise discord.NotFound(
                        _FakeResponse(),  # type: ignore[arg-type]
                        f"Attachment {image_ref.attachment_id} not found in message "
                        f"{image_ref.message_id} ({image_ref.filename}).",
                    )

                saved = await download_and_place(
                    attachment=attachment,
                    dest_dir=dest_dir,
                    rel_dir=rel_dir,
                    exercise_index=exercise_index,
                    label=part.label,
                    page=page,
                    downscale_max_px=self.settings.downscale_max_px,
                )
                image_paths.append(saved.rel_path)

            figure_parts.append(
                FigurePart(label=part.label, image_paths=image_paths)
            )
        return figure_parts

    @staticmethod
    async def _fetch_message(
        thread: discord.Thread,
        message_id: int,
        cache: dict[int, discord.Message],
    ) -> discord.Message:
        """Fetch (and cache) a message from *thread* by id.

        ``/build`` re-fetches each message rather than trusting the stored (expiring)
        CDN URL. Multiple parts/pages may reference the same message, so results are
        cached for the duration of one build.
        """
        if message_id in cache:
            return cache[message_id]
        message = await thread.fetch_message(message_id)
        cache[message_id] = message
        return message

    @staticmethod
    def _find_attachment(
        message: discord.Message, attachment_id: int
    ) -> discord.Attachment | None:
        """Return the attachment with *attachment_id* from *message*, or ``None``."""
        for attachment in message.attachments:
            if attachment.id == attachment_id:
                return attachment
        return None

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Catch-all for unhandled app-command errors in this cog.

        Logs the full traceback for the operator and replies to the user with a generic,
        ephemeral message (whether or not the interaction was already deferred), so an
        unexpected failure never leaves the user staring at "the application did not
        respond".
        """
        # Unwrap the discord.py invoke wrapper to log the underlying cause.
        original = getattr(error, "original", error)
        logger.exception(
            "Unhandled error in app command %s: %r",
            getattr(interaction.command, "name", "<unknown>"),
            original,
        )

        message = (
            "An unexpected error occurred. Please try again; "
            "if it persists, check the bot logs."
        )
        try:
            await self._reply_ephemeral(interaction, message)
        except discord.HTTPException:
            # If even the error reply fails (e.g. interaction expired), there is nothing
            # more we can do beyond the log above.
            logger.warning("Could not deliver error reply to the user.", exc_info=True)


class _FakeResponse:
    """Minimal stand-in for an ``aiohttp`` response, used to raise :class:`discord.NotFound`.

    ``discord.NotFound`` expects a response-like object exposing ``status`` and
    ``reason``. When a *specific attachment* is missing from an otherwise-present
    message, there is no real HTTP response to attach, so we synthesise a 404 here to
    reuse the same not-found handling path in :meth:`Exercises.build`.
    """

    status = 404
    reason = "Not Found"


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point.

    ``bot.py`` adds the cog directly (it needs to pass ``settings`` and ``store``), so
    this ``setup`` is provided only for completeness / ``load_extension`` compatibility.
    It expects the bot to expose ``settings`` and ``store`` attributes; if it does not,
    the cog cannot be constructed and a clear error is raised.
    """
    settings = getattr(bot, "settings", None)
    store = getattr(bot, "store", None)
    if settings is None or store is None:
        raise RuntimeError(
            "Exercises cog requires bot.settings and bot.store to be set before "
            "load_extension; add the cog manually in setup_hook instead."
        )
    await bot.add_cog(Exercises(bot, settings, store))
