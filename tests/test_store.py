"""Tests for :mod:`bot.store` — the aiosqlite persistence layer.

These tests deliberately avoid a ``pytest-asyncio`` dependency (keeping the test suite
dependency-light, as the contract requests). Instead, every test is an ordinary *synchronous*
pytest function that drives the async :class:`~bot.store.Store` API by running a single async
coroutine to completion via :func:`asyncio.run`.

A small :func:`_run_with_store` helper owns the full lifecycle for each test:

    * build a :class:`Store` pointed at a throwaway sqlite file under pytest's ``tmp_path``,
    * ``await store.init()``,
    * hand the live store to the test body,
    * and ALWAYS ``await store.close()`` afterwards (even if the body raises),

so the connection is never leaked and WAL side-files are cleaned up between tests.

Coverage (mirrors CONTRACTS.md "tests/test_store.py"):
    * ``create_sheet`` returns ``True`` then ``False`` on a duplicate ``(channel, sheet)``;
    * ``set_hub_message`` + ``get_sheet`` round-trip;
    * ``add_thread`` + ``get_threads`` ordered by ``exercise_index``;
    * ``find_thread`` returns the right ``(sheet, exercise_index)`` from a globally-unique id;
    * ``replace_selection`` then ``get_selection`` round-trips ordered parts/images;
    * ``replace_selection`` run twice REPLACES (second selection wins, old rows gone);
    * ``get_all_selections`` returns only exercises that have rows, keyed/ordered correctly;
    * the SAME sheet number lives independently in two different channels;
    * a v0 guild-keyed database migrates to channel-id keying on ``init``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

# ---------------------------------------------------------------------------
# Make ``import bot.store`` resolve when pytest is invoked from anywhere by
# putting the repository root (the parent of this ``tests/`` directory) on
# sys.path. This keeps the suite runnable with a bare ``pytest`` and no
# editable install / PYTHONPATH juggling.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.store import ChannelConfig, ImageRef, Part, SheetRow, Store, ThreadRow  # noqa: E402

T = TypeVar("T")

# Fixed channel id used throughout; the exact value is irrelevant to the tests.
CHANNEL = 111_222_333


# ---------------------------------------------------------------------------
# Lifecycle helper: run one async test body against a freshly-initialised Store.
# ---------------------------------------------------------------------------


def _run_with_store(db_path: Path, body: Callable[[Store], Awaitable[T]]) -> T:
    """Open a :class:`Store` at ``db_path``, run ``body(store)``, and always close it.

    The whole thing is wrapped in a single coroutine handed to :func:`asyncio.run`, so the
    store's connection lives and dies on one event loop (aiosqlite binds its background worker
    thread to the loop that created it). ``store.close()`` runs in a ``finally`` so the
    connection is released even when the test body asserts/raises.
    """

    async def _main() -> T:
        store = Store(db_path)
        await store.init()
        try:
            return await body(store)
        finally:
            await store.close()

    return asyncio.run(_main())


def _make_image(position: int, *, message_id: int, attachment_id: int) -> ImageRef:
    """Build an :class:`ImageRef` with deterministic, easily-asserted field values."""
    return ImageRef(
        img_position=position,
        message_id=message_id,
        attachment_id=attachment_id,
        url=f"https://cdn.example/{attachment_id}.png",
        filename=f"img_{attachment_id}.png",
        content_type="image/png",
    )


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------


def test_create_sheet_returns_true_then_false_on_duplicate(tmp_path: Path) -> None:
    """First insert of a ``(channel, sheet)`` returns True; a duplicate returns False.

    Also confirms the duplicate insert does NOT clobber the original row (the first
    ``num_exercises`` survives), i.e. the PK guard rejects rather than overwrites.
    """

    async def body(store: Store) -> None:
        # First creation succeeds.
        first = await store.create_sheet(CHANNEL, sheet=6, num_exercises=3)
        assert first is True

        # Same (channel, sheet) again -> rejected by the primary-key guard.
        dup = await store.create_sheet(CHANNEL, sheet=6, num_exercises=99)
        assert dup is False

        # A different sheet number is independent and still succeeds.
        other = await store.create_sheet(CHANNEL, sheet=7, num_exercises=2)
        assert other is True

        # The original row is untouched (the rejected dup did not overwrite num_exercises).
        sheet6 = await store.get_sheet(CHANNEL, 6)
        assert sheet6 is not None
        assert sheet6.num_exercises == 3

    _run_with_store(tmp_path / "sheets_dup.sqlite3", body)


def test_set_hub_message_and_get_sheet(tmp_path: Path) -> None:
    """``set_hub_message`` persists and ``get_sheet`` reflects it; missing sheets give None."""

    async def body(store: Store) -> None:
        # Unknown sheet -> None.
        assert await store.get_sheet(CHANNEL, 6) is None

        # Create without a hub message; the column starts NULL.
        assert await store.create_sheet(CHANNEL, sheet=6, num_exercises=3) is True
        row = await store.get_sheet(CHANNEL, 6)
        assert isinstance(row, SheetRow)
        assert row.channel_id == CHANNEL
        assert row.sheet == 6
        assert row.num_exercises == 3
        assert row.hub_message_id is None
        # created_at is filled by the DEFAULT datetime('now').
        assert isinstance(row.created_at, str) and row.created_at != ""

        # Set the hub message id, then re-read.
        await store.set_hub_message(CHANNEL, sheet=6, hub_message_id=987_654_321)
        updated = await store.get_sheet(CHANNEL, 6)
        assert updated is not None
        assert updated.hub_message_id == 987_654_321
        # Other fields are unchanged by the hub update.
        assert updated.num_exercises == 3

    _run_with_store(tmp_path / "hub.sqlite3", body)


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


def test_add_thread_and_get_threads_ordered_by_exercise_index(tmp_path: Path) -> None:
    """``get_threads`` returns rows sorted by ``exercise_index`` regardless of insert order."""

    async def body(store: Store) -> None:
        await store.create_sheet(CHANNEL, sheet=6, num_exercises=3)

        # Insert deliberately out of order (3, 1, 2) to prove the ORDER BY.
        await store.add_thread(CHANNEL, 6, exercise_index=3, thread_id=3003, thread_name="Aufgabe 3")
        await store.add_thread(CHANNEL, 6, exercise_index=1, thread_id=1001, thread_name="Aufgabe 1")
        await store.add_thread(CHANNEL, 6, exercise_index=2, thread_id=2002, thread_name="Aufgabe 2")

        threads = await store.get_threads(CHANNEL, 6)
        assert [t.exercise_index for t in threads] == [1, 2, 3]
        assert [t.thread_id for t in threads] == [1001, 2002, 3003]
        assert all(isinstance(t, ThreadRow) for t in threads)
        # Spot-check a fully-populated row.
        assert threads[0].thread_name == "Aufgabe 1"
        assert threads[0].channel_id == CHANNEL
        assert threads[0].sheet == 6

        # A different sheet's threads are not mixed in.
        assert await store.get_threads(CHANNEL, 7) == []

    _run_with_store(tmp_path / "threads_order.sqlite3", body)


def test_find_thread_returns_correct_sheet_and_exercise(tmp_path: Path) -> None:
    """``find_thread`` maps a globally-unique thread id back to ``(sheet, exercise_index)``.

    Includes a second sheet so we prove the lookup discriminates by ``thread_id``, and an
    unknown id to prove it returns ``None``. Thread ids are globally unique, so the lookup
    takes no channel argument.
    """

    async def body(store: Store) -> None:
        await store.create_sheet(CHANNEL, sheet=6, num_exercises=2)
        await store.create_sheet(CHANNEL, sheet=7, num_exercises=1)

        await store.add_thread(CHANNEL, 6, exercise_index=1, thread_id=1001, thread_name="6-1")
        await store.add_thread(CHANNEL, 6, exercise_index=2, thread_id=2002, thread_name="6-2")
        await store.add_thread(CHANNEL, 7, exercise_index=1, thread_id=7007, thread_name="7-1")

        found = await store.find_thread(thread_id=2002)
        assert found is not None
        assert (found.sheet, found.exercise_index) == (6, 2)
        assert found.thread_name == "6-2"
        assert found.channel_id == CHANNEL

        # A thread belonging to the other sheet resolves to that sheet.
        found7 = await store.find_thread(thread_id=7007)
        assert found7 is not None
        assert (found7.sheet, found7.exercise_index) == (7, 1)

        # Unknown thread id -> None.
        assert await store.find_thread(thread_id=999_999) is None

    _run_with_store(tmp_path / "find_thread.sqlite3", body)


# ---------------------------------------------------------------------------
# selections
# ---------------------------------------------------------------------------


def test_replace_selection_and_get_selection_roundtrip_ordering(tmp_path: Path) -> None:
    """A full selection round-trips with parts ordered by input order and images by position.

    Builds an exercise with three parts:
        * part "a"  -> single image,
        * part "b"  -> two images (multi-page) supplied out of position order to prove the
          ``img_position`` ORDER BY,
        * part ""   -> whole-exercise image with no label.
    Then reads it back and asserts the reconstructed ``Part``/``ImageRef`` structure.
    """

    async def body(store: Store) -> None:
        await store.create_sheet(CHANNEL, sheet=6, num_exercises=2)

        part_a = Part(
            label="a",
            images=[_make_image(1, message_id=5001, attachment_id=9001)],
        )
        # Provide page 2 BEFORE page 1 to prove ordering comes from img_position, not list order.
        part_b = Part(
            label="b",
            images=[
                _make_image(2, message_id=5002, attachment_id=9003),
                _make_image(1, message_id=5002, attachment_id=9002),
            ],
        )
        part_whole = Part(
            label="",
            images=[_make_image(1, message_id=5003, attachment_id=9004)],
        )

        await store.replace_selection(CHANNEL, 6, exercise_index=1, parts=[part_a, part_b, part_whole])

        parts = await store.get_selection(CHANNEL, 6, exercise_index=1)

        # Parts preserved in input order (part_index 1,2,3).
        assert [p.label for p in parts] == ["a", "b", ""]

        # Part "a": one image.
        assert [img.img_position for img in parts[0].images] == [1]
        assert parts[0].images[0].attachment_id == 9001

        # Part "b": images reordered by img_position -> 1 then 2.
        assert [img.img_position for img in parts[1].images] == [1, 2]
        assert [img.attachment_id for img in parts[1].images] == [9002, 9003]

        # Whole-exercise part: empty label, one image, all metadata intact.
        whole_img = parts[2].images[0]
        assert parts[2].label == ""
        assert whole_img.message_id == 5003
        assert whole_img.attachment_id == 9004
        assert whole_img.filename == "img_9004.png"
        assert whole_img.content_type == "image/png"
        assert whole_img.url == "https://cdn.example/9004.png"

        # An exercise that was never picked returns an empty list.
        assert await store.get_selection(CHANNEL, 6, exercise_index=2) == []

    _run_with_store(tmp_path / "selection_roundtrip.sqlite3", body)


def test_replace_selection_run_twice_replaces_old_rows(tmp_path: Path) -> None:
    """Re-running ``replace_selection`` fully REPLACES the prior selection (second wins).

    The first selection has two parts / three images; the second has a single different part.
    After the second call, none of the first call's images may survive — proving the DELETE
    half of the atomic replace removed all old rows for that exercise.
    """

    async def body(store: Store) -> None:
        await store.create_sheet(CHANNEL, sheet=6, num_exercises=1)

        # First (soon-to-be-overwritten) selection: parts a + b, three images total.
        first = [
            Part(label="a", images=[_make_image(1, message_id=1, attachment_id=101)]),
            Part(
                label="b",
                images=[
                    _make_image(1, message_id=2, attachment_id=102),
                    _make_image(2, message_id=2, attachment_id=103),
                ],
            ),
        ]
        await store.replace_selection(CHANNEL, 6, 1, first)

        before = await store.get_selection(CHANNEL, 6, 1)
        assert {img.attachment_id for p in before for img in p.images} == {101, 102, 103}

        # Second selection: a single combined "a b" part with one fresh image.
        second = [
            Part(label="a b", images=[_make_image(1, message_id=9, attachment_id=999)]),
        ]
        await store.replace_selection(CHANNEL, 6, 1, second)

        after = await store.get_selection(CHANNEL, 6, 1)

        # Exactly the new selection — one part, one image — and the old ids are gone.
        assert len(after) == 1
        assert after[0].label == "a b"
        assert [img.attachment_id for img in after[0].images] == [999]
        surviving_ids = {img.attachment_id for p in after for img in p.images}
        assert surviving_ids == {999}
        assert surviving_ids.isdisjoint({101, 102, 103})

    _run_with_store(tmp_path / "selection_replace.sqlite3", body)


def test_get_all_selections_keys_and_orders_only_filled_exercises(tmp_path: Path) -> None:
    """``get_all_selections`` returns one entry per exercise that has rows, properly ordered.

    Exercises 1 and 3 get selections (inserted with 3 before 1 to test the mapping is keyed,
    not positional); exercise 2 is left empty and must NOT appear in the result. Within each
    exercise, parts/images keep their part_index / img_position ordering.
    """

    async def body(store: Store) -> None:
        await store.create_sheet(CHANNEL, sheet=6, num_exercises=3)

        # Fill exercise 3 first, then exercise 1, to prove keying isn't insertion-ordered.
        await store.replace_selection(
            CHANNEL,
            6,
            3,
            [Part(label="", images=[_make_image(1, message_id=30, attachment_id=300)])],
        )
        await store.replace_selection(
            CHANNEL,
            6,
            1,
            [
                Part(label="a", images=[_make_image(1, message_id=10, attachment_id=101)]),
                Part(
                    label="b",
                    images=[
                        _make_image(1, message_id=11, attachment_id=102),
                        _make_image(2, message_id=11, attachment_id=103),
                    ],
                ),
            ],
        )
        # Exercise 2 intentionally has NO selection.

        all_sel = await store.get_all_selections(CHANNEL, 6)

        # Only the two filled exercises are present; the empty one is absent.
        assert set(all_sel.keys()) == {1, 3}

        # Exercise 1: two parts, with part "b" carrying two ordered images.
        ex1 = all_sel[1]
        assert [p.label for p in ex1] == ["a", "b"]
        assert [img.attachment_id for img in ex1[1].images] == [102, 103]

        # Exercise 3: single unlabelled part with one image.
        ex3 = all_sel[3]
        assert len(ex3) == 1
        assert ex3[0].label == ""
        assert [img.attachment_id for img in ex3[0].images] == [300]

        # A sheet with no selections at all yields an empty mapping.
        assert await store.get_all_selections(CHANNEL, 7) == {}

    _run_with_store(tmp_path / "all_selections.sqlite3", body)


# ---------------------------------------------------------------------------
# channel isolation — the same sheet number in two different channels
# ---------------------------------------------------------------------------


def test_same_sheet_independent_across_channels(tmp_path: Path) -> None:
    """The same sheet number can exist independently in two different channels.

    Channel A and channel B both create sheet 6 (both ``create_sheet`` calls return ``True``,
    proving the primary key is ``(channel_id, sheet)`` and not ``sheet`` alone), and their
    threads, selections and configs stay independent of one another.
    """

    chan_a = 555_000_001
    chan_b = 555_000_002

    async def body(store: Store) -> None:
        # Both channels create sheet 6 independently — neither is a duplicate.
        assert await store.create_sheet(chan_a, sheet=6, num_exercises=3) is True
        assert await store.create_sheet(chan_b, sheet=6, num_exercises=5) is True

        # Each channel's sheet row keeps its own num_exercises.
        sheet_a = await store.get_sheet(chan_a, 6)
        sheet_b = await store.get_sheet(chan_b, 6)
        assert sheet_a is not None and sheet_a.num_exercises == 3
        assert sheet_b is not None and sheet_b.num_exercises == 5
        assert sheet_a.channel_id == chan_a
        assert sheet_b.channel_id == chan_b

        # Threads are independent: each channel's get_threads only sees its own rows.
        await store.add_thread(chan_a, 6, exercise_index=1, thread_id=1001, thread_name="A-1")
        await store.add_thread(chan_b, 6, exercise_index=1, thread_id=2002, thread_name="B-1")
        threads_a = await store.get_threads(chan_a, 6)
        threads_b = await store.get_threads(chan_b, 6)
        assert [t.thread_id for t in threads_a] == [1001]
        assert [t.thread_id for t in threads_b] == [2002]

        # Selections are independent: writing to one channel does not leak into the other.
        await store.replace_selection(
            chan_a,
            6,
            1,
            [Part(label="a", images=[_make_image(1, message_id=10, attachment_id=901)])],
        )
        await store.replace_selection(
            chan_b,
            6,
            1,
            [Part(label="b", images=[_make_image(1, message_id=20, attachment_id=902)])],
        )
        sel_a = await store.get_selection(chan_a, 6, 1)
        sel_b = await store.get_selection(chan_b, 6, 1)
        assert [p.label for p in sel_a] == ["a"]
        assert [img.attachment_id for img in sel_a[0].images] == [901]
        assert [p.label for p in sel_b] == ["b"]
        assert [img.attachment_id for img in sel_b[0].images] == [902]

        # Configs are independent: setting one channel's config leaves the other empty.
        await store.set_channel_config(chan_a, group_number="017")
        cfg_a = await store.get_channel_config(chan_a)
        cfg_b = await store.get_channel_config(chan_b)
        assert cfg_a.group_number == "017"
        assert cfg_b.is_empty()

    _run_with_store(tmp_path / "two_channels.sqlite3", body)


# ---------------------------------------------------------------------------
# channel_config — per-channel header overrides
# ---------------------------------------------------------------------------


def test_channel_config_defaults_empty(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        cfg = await store.get_channel_config(CHANNEL)
        assert isinstance(cfg, ChannelConfig)
        assert cfg.is_empty()
        assert cfg.group_number is None and cfg.authors is None

    _run_with_store(tmp_path / "cfg.sqlite3", body)


def test_channel_config_set_and_get(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        returned = await store.set_channel_config(
            CHANNEL, group_number="017", course="GLOIN", tutorium="Tut 3",
            authors="Anna, 1; Ben, 2",
        )
        assert returned.group_number == "017"
        cfg = await store.get_channel_config(CHANNEL)
        assert cfg.group_number == "017"
        assert cfg.course == "GLOIN"
        assert cfg.tutorium == "Tut 3"
        assert cfg.authors == "Anna, 1; Ben, 2"
        assert not cfg.is_empty()

    _run_with_store(tmp_path / "cfg.sqlite3", body)


def test_channel_config_partial_update_keeps_other_fields(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        await store.set_channel_config(CHANNEL, group_number="017", course="GLOIN")
        # Update only the course; group_number must survive (was the review's concern).
        await store.set_channel_config(CHANNEL, course="Mein Kurs SoSe 2026")
        cfg = await store.get_channel_config(CHANNEL)
        assert cfg.group_number == "017"  # untouched
        assert cfg.course == "Mein Kurs SoSe 2026"  # changed

    _run_with_store(tmp_path / "cfg.sqlite3", body)


def test_channel_config_clear_field_with_none(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        await store.set_channel_config(CHANNEL, group_number="017", course="GLOIN")
        await store.set_channel_config(CHANNEL, course=None)  # explicit clear
        cfg = await store.get_channel_config(CHANNEL)
        assert cfg.course is None
        assert cfg.group_number == "017"  # other field untouched

    _run_with_store(tmp_path / "cfg.sqlite3", body)


def test_channel_config_blank_normalises_to_none(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        await store.set_channel_config(CHANNEL, group_number="   ")
        cfg = await store.get_channel_config(CHANNEL)
        assert cfg.group_number is None  # whitespace-only stored as NULL

    _run_with_store(tmp_path / "cfg.sqlite3", body)


def test_channel_config_is_per_channel(tmp_path: Path) -> None:
    async def body(store: Store) -> None:
        await store.set_channel_config(CHANNEL, group_number="017")
        other = await store.get_channel_config(999_999)
        assert other.is_empty()  # a different channel has its own (empty) config

    _run_with_store(tmp_path / "cfg.sqlite3", body)


# ---------------------------------------------------------------------------
# migration — a v0 guild-keyed database is re-keyed to channel_id on init
# ---------------------------------------------------------------------------


def _build_v0_database(db_path: Path, guild_id: int) -> None:
    """Hand-build an OLD (schema v0) guild-keyed sqlite database with one row per table.

    Uses the stdlib :mod:`sqlite3` module synchronously so the fixture is independent of the
    (channel-keyed) production schema. Mirrors the original guild-keyed schema exactly: every
    keyed table carries ``guild_id`` in its primary key, the config table is ``guild_config``,
    the thread index is over ``(guild_id, thread_id)``, and ``PRAGMA user_version`` is 0.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE sheets (
              guild_id INTEGER NOT NULL, sheet INTEGER NOT NULL,
              num_exercises INTEGER NOT NULL, hub_message_id INTEGER,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (guild_id, sheet));

            CREATE TABLE threads (
              guild_id INTEGER NOT NULL, sheet INTEGER NOT NULL, exercise_index INTEGER NOT NULL,
              thread_id INTEGER NOT NULL, thread_name TEXT NOT NULL,
              PRIMARY KEY (guild_id, sheet, exercise_index));

            CREATE INDEX idx_threads_thread ON threads(guild_id, thread_id);

            CREATE TABLE selections (
              guild_id INTEGER NOT NULL, sheet INTEGER NOT NULL, exercise_index INTEGER NOT NULL,
              part_index INTEGER NOT NULL, part_label TEXT NOT NULL, img_position INTEGER NOT NULL,
              message_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
              url TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT,
              PRIMARY KEY (guild_id, sheet, exercise_index, part_index, img_position));

            CREATE TABLE guild_config (
              guild_id INTEGER NOT NULL PRIMARY KEY,
              group_number TEXT, course TEXT, tutorium TEXT, authors TEXT);
            """
        )
        conn.execute(
            "INSERT INTO sheets (guild_id, sheet, num_exercises, hub_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (guild_id, 6, 3, 424242, "2024-01-01 00:00:00"),
        )
        conn.execute(
            "INSERT INTO threads (guild_id, sheet, exercise_index, thread_id, thread_name) "
            "VALUES (?, ?, ?, ?, ?);",
            (guild_id, 6, 1, 700_700, "Aufgabe 1"),
        )
        conn.execute(
            "INSERT INTO selections (guild_id, sheet, exercise_index, part_index, part_label, "
            "img_position, message_id, attachment_id, url, filename, content_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (guild_id, 6, 1, 1, "a", 1, 8001, 8002, "https://cdn.example/8002.png",
             "img_8002.png", "image/png"),
        )
        conn.execute(
            "INSERT INTO guild_config (guild_id, group_number, course, tutorium, authors) "
            "VALUES (?, ?, ?, ?, ?);",
            (guild_id, "017", "GLOIN", "Tut 3", "Anna, 1; Ben, 2"),
        )
        conn.execute("PRAGMA user_version = 0;")
        conn.commit()
    finally:
        conn.close()


def test_migrates_v0_guild_keyed_db_to_channel_id(tmp_path: Path) -> None:
    """A pre-existing guild-keyed (v0) database is rebuilt under ``channel_id`` on ``init``.

    Build the old schema by hand, then open it with ``Store`` and a ``legacy_channel_id``. All
    rows must become reachable under that channel id (sheet/thread/selection/config), the schema
    version must advance to 1, the ``sheets`` table must now have a ``channel_id`` column (and no
    ``guild_id``), and the ``guild_config`` table must be gone.
    """

    old_guild = 999_888_777
    legacy_channel = 314_159_265
    db_path = tmp_path / "v0_migrate.sqlite3"
    _build_v0_database(db_path, old_guild)

    async def _main() -> None:
        store = Store(db_path)
        await store.init(legacy_channel_id=legacy_channel)
        try:
            # Rows are now reachable under the legacy channel id.
            sheet = await store.get_sheet(legacy_channel, 6)
            assert sheet is not None
            assert sheet.channel_id == legacy_channel
            assert sheet.num_exercises == 3
            assert sheet.hub_message_id == 424242

            thread = await store.find_thread(thread_id=700_700)
            assert thread is not None
            assert thread.channel_id == legacy_channel
            assert (thread.sheet, thread.exercise_index) == (6, 1)
            assert thread.thread_name == "Aufgabe 1"

            selection = await store.get_selection(legacy_channel, 6, 1)
            assert [p.label for p in selection] == ["a"]
            assert [img.attachment_id for img in selection[0].images] == [8002]

            cfg = await store.get_channel_config(legacy_channel)
            assert cfg.group_number == "017"
            assert cfg.course == "GLOIN"
            assert cfg.tutorium == "Tut 3"
            assert cfg.authors == "Anna, 1; Ben, 2"

            # The old guild id resolves to nothing now (rows were re-keyed, not duplicated).
            assert await store.get_sheet(old_guild, 6) is None
        finally:
            await store.close()

    asyncio.run(_main())

    # Inspect the on-disk schema directly with stdlib sqlite3 (not the async store).
    conn = sqlite3.connect(db_path)
    try:
        (version,) = conn.execute("PRAGMA user_version;").fetchone()
        assert version == 1

        sheet_cols = {row[1] for row in conn.execute("PRAGMA table_info(sheets);").fetchall()}
        assert "channel_id" in sheet_cols
        assert "guild_id" not in sheet_cols

        # guild_config must be gone; channel_config must exist instead.
        guild_config_cols = conn.execute("PRAGMA table_info(guild_config);").fetchall()
        assert guild_config_cols == []
        channel_config_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(channel_config);").fetchall()
        }
        assert "channel_id" in channel_config_cols
    finally:
        conn.close()
