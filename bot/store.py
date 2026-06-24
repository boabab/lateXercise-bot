"""Persistence layer for the lateXercise bot.

This module owns all SQLite access. Everything is keyed by **channel_id** — each operating
channel is an independent *submission group* with its own sheets, picks, and header config.
(Discord channel IDs are globally unique snowflakes, so the channel id alone is a sufficient
key; the parent guild is not stored.) It holds four kinds of data:

* ``sheets``         — one row per ``(channel_id, sheet)`` exercise sheet created via ``/sheet``.
* ``threads``        — the *mapping* from an exercise index to its Discord thread, written at
                       creation so ``/build`` never has to parse (user-renamable) thread names.
* ``selections``     — one row per *image*, grouped into ordered parts, written by ``/pick``.
* ``channel_config`` — per-channel LaTeX header overrides (group/course/tutorium/authors).

Concurrency model: multiple people may run ``/pick`` simultaneously, so the store must be
multi-writer safe. We open exactly ONE shared :class:`aiosqlite.Connection` in
:meth:`Store.init` configured with ``PRAGMA journal_mode=WAL`` and ``PRAGMA busy_timeout=5000``.
A single aiosqlite connection serializes statements on one worker thread, but that alone does
NOT make a *multi-statement transaction* atomic against interleaving coroutines: because every
write shares one connection (one implicit transaction), a ``commit()`` from any writer would
flush another writer's half-finished transaction. Therefore an :class:`asyncio.Lock`
(``_write_lock``) is held around the execute+commit of EVERY writer. Reads do not take the lock
(WAL lets them proceed concurrently).

Schema migrations are tracked with ``PRAGMA user_version``. Version 1 introduced channel-id
keying (replacing the original guild-id keying); :meth:`Store._migrate` rebuilds an old
guild-keyed database in place, backfilling existing rows with a supplied legacy channel id.

All rows are fetched with ``row_factory = aiosqlite.Row`` so columns are addressable by name.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

_log = logging.getLogger(__name__)

__all__ = [
    "SheetRow",
    "ThreadRow",
    "ImageRef",
    "Part",
    "ChannelConfig",
    "Store",
]

# Current schema version (see _migrate). Bump when the schema changes in a breaking way.
#   v1 -> v2: add channel_config.language (per-channel output language).
_SCHEMA_VERSION = 2

# Sentinel for "argument not provided" in set_channel_config, so callers can distinguish
# "leave this field unchanged" (omit it) from "clear this field" (pass None/"").
_UNSET: object = object()


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class SheetRow:
    """A row of the ``sheets`` table: one exercise sheet within a channel."""

    channel_id: int
    sheet: int
    num_exercises: int
    hub_message_id: int | None
    created_at: str


@dataclass
class ThreadRow:
    """A row of the ``threads`` table mapping an exercise to its Discord thread."""

    channel_id: int
    sheet: int
    exercise_index: int  # 1-based
    thread_id: int
    thread_name: str


@dataclass
class ImageRef:
    """A single picked image (one page) within a :class:`Part`."""

    img_position: int  # 1-based page order within the part
    message_id: int
    attachment_id: int
    url: str
    filename: str
    content_type: str | None


@dataclass
class Part:
    """An ordered part of an exercise: an optional label plus its ordered images.

    ``label`` is the canonical raw label as produced by the UI parser: ``""`` for the whole
    exercise (no sub-part), ``"a"`` for a single sub-part, or ``"a b"`` for a combined photo
    covering several sub-parts.
    """

    label: str  # "", "a", "a b"
    images: list[ImageRef] = field(default_factory=list)  # ordered by img_position


@dataclass
class ChannelConfig:
    """Per-channel overrides for the LaTeX header/output settings, set via ``/config``.

    Every field is optional; ``None`` means "no override — fall back to the ``.env``
    default, then to ``exercise.sty``". ``authors`` is stored as a single string with
    entries separated by ``;`` (as typed in ``/config``); the cog splits it into lines.
    ``language`` is the output-language code (``"en"``/``"de"``) that localizes the PDF
    filename, headings, title, and babel language.
    """

    group_number: str | None = None
    course: str | None = None
    tutorium: str | None = None
    authors: str | None = None
    language: str | None = None

    def is_empty(self) -> bool:
        """True when no field is set (no override exists for this channel)."""
        return not any(
            (self.group_number, self.course, self.tutorium, self.authors, self.language)
        )


# ---------------------------------------------------------------------------
# Schema (channel-keyed; CREATE TABLE IF NOT EXISTS so init is idempotent).
# ---------------------------------------------------------------------------

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS sheets (
  channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL,
  num_exercises INTEGER NOT NULL, hub_message_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (channel_id, sheet));

CREATE TABLE IF NOT EXISTS threads (
  channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL, exercise_index INTEGER NOT NULL,
  thread_id INTEGER NOT NULL, thread_name TEXT NOT NULL,
  PRIMARY KEY (channel_id, sheet, exercise_index));

CREATE INDEX IF NOT EXISTS idx_threads_thread ON threads(thread_id);

CREATE TABLE IF NOT EXISTS selections (
  channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL, exercise_index INTEGER NOT NULL,
  part_index INTEGER NOT NULL, part_label TEXT NOT NULL, img_position INTEGER NOT NULL,
  message_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
  url TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT,
  PRIMARY KEY (channel_id, sheet, exercise_index, part_index, img_position));

CREATE TABLE IF NOT EXISTS channel_config (
  channel_id INTEGER NOT NULL PRIMARY KEY,
  group_number TEXT, course TEXT, tutorium TEXT, authors TEXT, language TEXT);
"""


class Store:
    """Async SQLite store backed by a single shared aiosqlite connection.

    Lifecycle: construct with a DB path, ``await init()`` once at startup, ``await close()`` at
    shutdown. All other methods require :meth:`init` to have run first.
    """

    def __init__(self, db_path: Path) -> None:
        """Record the DB path. No I/O happens here; call :meth:`init` to open the connection."""
        self._db_path: Path = Path(db_path)
        # The single shared connection, created in init(). None until then / after close().
        self._conn: aiosqlite.Connection | None = None
        # Serializes every multi-statement write so commits can't cross transactions.
        self._write_lock: asyncio.Lock = asyncio.Lock()

    # -- internal helper -----------------------------------------------------

    @property
    def _connection(self) -> aiosqlite.Connection:
        """Return the live connection, raising a clear error if the store is not initialised."""
        if self._conn is None:
            raise RuntimeError("Store.init() must be awaited before using the store.")
        return self._conn

    # -- lifecycle -----------------------------------------------------------

    async def init(self, legacy_channel_id: int | None = None) -> None:
        """Open/create the DB, apply PRAGMAs, run migrations, and create tables if missing.

        Opens exactly one shared connection with ``row_factory = aiosqlite.Row`` and applies
        ``journal_mode=WAL``, ``busy_timeout=5000`` and ``foreign_keys=ON``.

        ``legacy_channel_id`` is used only when upgrading a pre-existing *guild-keyed* database
        (schema v0): all existing rows are re-keyed to this channel id. Pass the bot's primary
        operating channel. ``None`` backfills with ``0`` (orphaning old rows under a sentinel).
        """
        # Ensure the parent directory exists (config.py also does this, but be defensive).
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row  # address columns by name everywhere downstream

        # WAL allows concurrent readers alongside a writer; busy_timeout lets a blocked write
        # wait (up to 5s) instead of immediately raising "database is locked". journal_mode
        # returns the resulting mode; SQLite silently keeps the old mode if WAL is unavailable
        # (e.g. some network filesystems), so read it back and warn if it didn't take.
        async with conn.execute("PRAGMA journal_mode=WAL;") as cursor:
            mode_row = await cursor.fetchone()
        effective_mode = mode_row[0] if mode_row else "?"
        if str(effective_mode).lower() != "wal":
            _log.warning(
                "SQLite journal_mode is %r, not 'wal' (multi-writer safety degraded) for %s",
                effective_mode,
                self._db_path,
            )
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("PRAGMA foreign_keys=ON;")

        # Upgrade an older guild-keyed database before creating any (new-schema) tables.
        await self._migrate(conn, legacy_channel_id)

        # Create any still-missing tables with the current schema. executescript commits.
        await conn.executescript(_SCHEMA)
        await conn.commit()

        self._conn = conn

    async def close(self) -> None:
        """Close the shared connection if open. Idempotent."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- migrations ----------------------------------------------------------

    @staticmethod
    async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
        """Return the set of column names of *table*, or an empty set if it doesn't exist."""
        async with conn.execute(f"PRAGMA table_info({table});") as cursor:
            rows = await cursor.fetchall()
        return {row[1] for row in rows}  # row[1] is the column name

    async def _migrate(
        self, conn: aiosqlite.Connection, legacy_channel_id: int | None
    ) -> None:
        """Bring an existing database up to ``_SCHEMA_VERSION``.

        v0 -> v1: the original schema keyed every table by ``guild_id``; v1 re-keys by
        ``channel_id`` (so one guild can host several independent submission channels). For an
        old database we rebuild each keyed table with the new primary key, backfilling
        ``channel_id`` with ``legacy_channel_id`` (the bot's primary channel). A fresh database
        has no old tables, so nothing is rebuilt — only the version marker advances.

        v1 -> v2: add the ``language`` column to an existing ``channel_config`` table (the
        per-channel output language). A fresh database has no ``channel_config`` yet — it is
        created with the column straight from ``_SCHEMA`` after this migration runs — so the
        ``ALTER`` only touches a real pre-v2 table.

        The per-table guards (skip tables that already have ``channel_id``/``language`` or
        don't exist, and drop any leftover ``_old_*`` scratch tables first) make a re-run
        after an interrupted migration safe.
        """
        async with conn.execute("PRAGMA user_version;") as cursor:
            version = (await cursor.fetchone())[0]
        if version >= _SCHEMA_VERSION:
            return

        backfill = legacy_channel_id if legacy_channel_id is not None else 0
        migrated_any = False

        # Defensive: clear scratch tables left by a previously interrupted migration.
        for scratch in ("_old_sheets", "_old_threads", "_old_selections"):
            await conn.execute(f"DROP TABLE IF EXISTS {scratch};")

        # --- sheets / threads / selections: rename -> create new -> copy -> drop -----
        rebuilds = (
            (
                "sheets",
                "CREATE TABLE sheets ("
                "channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL, "
                "num_exercises INTEGER NOT NULL, hub_message_id INTEGER, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "PRIMARY KEY (channel_id, sheet));",
                "sheet, num_exercises, hub_message_id, created_at",
            ),
            (
                "threads",
                "CREATE TABLE threads ("
                "channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL, "
                "exercise_index INTEGER NOT NULL, thread_id INTEGER NOT NULL, "
                "thread_name TEXT NOT NULL, "
                "PRIMARY KEY (channel_id, sheet, exercise_index));",
                "sheet, exercise_index, thread_id, thread_name",
            ),
            (
                "selections",
                "CREATE TABLE selections ("
                "channel_id INTEGER NOT NULL, sheet INTEGER NOT NULL, "
                "exercise_index INTEGER NOT NULL, part_index INTEGER NOT NULL, "
                "part_label TEXT NOT NULL, img_position INTEGER NOT NULL, "
                "message_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL, "
                "url TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT, "
                "PRIMARY KEY (channel_id, sheet, exercise_index, part_index, img_position));",
                "sheet, exercise_index, part_index, part_label, img_position, "
                "message_id, attachment_id, url, filename, content_type",
            ),
        )
        for name, create_sql, cols in rebuilds:
            existing = await self._table_columns(conn, name)
            if not existing or "channel_id" in existing:
                continue  # missing (fresh) or already migrated
            await conn.execute(f"ALTER TABLE {name} RENAME TO _old_{name};")
            await conn.execute(create_sql)
            await conn.execute(
                f"INSERT INTO {name}(channel_id, {cols}) SELECT ?, {cols} FROM _old_{name};",
                (backfill,),
            )
            await conn.execute(f"DROP TABLE _old_{name};")
            migrated_any = True

        # --- guild_config -> channel_config -----------------------------------------
        if await self._table_columns(conn, "guild_config"):
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS channel_config ("
                "channel_id INTEGER NOT NULL PRIMARY KEY, "
                "group_number TEXT, course TEXT, tutorium TEXT, authors TEXT);"
            )
            await conn.execute(
                "INSERT OR REPLACE INTO channel_config"
                "(channel_id, group_number, course, tutorium, authors) "
                "SELECT ?, group_number, course, tutorium, authors FROM guild_config;",
                (backfill,),
            )
            await conn.execute("DROP TABLE guild_config;")
            migrated_any = True

        # --- v1 -> v2: add channel_config.language to a pre-v2 table ------------------
        # Guarded so it is a no-op on a fresh DB (channel_config not created yet) and on an
        # already-v2 table (column present), keeping an interrupted re-run safe.
        channel_config_cols = await self._table_columns(conn, "channel_config")
        if channel_config_cols and "language" not in channel_config_cols:
            await conn.execute("ALTER TABLE channel_config ADD COLUMN language TEXT;")
            migrated_any = True

        await conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION};")
        await conn.commit()
        if migrated_any:
            _log.info(
                "Migrated DB schema to v%d (channel-keyed; channel_config.language; "
                "backfill channel=%s) for %s",
                _SCHEMA_VERSION,
                backfill,
                self._db_path,
            )

    # -- sheets --------------------------------------------------------------

    async def create_sheet(
        self,
        channel_id: int,
        sheet: int,
        num_exercises: int,
        hub_message_id: int | None = None,
    ) -> bool:
        """Insert a new sheet row.

        Returns ``True`` on insert, ``False`` if a row with the same ``(channel_id, sheet)``
        primary key already exists (the duplicate-``/sheet`` guard). The IntegrityError raised
        by the PK conflict is caught so callers get a simple boolean.
        """
        conn = self._connection
        # Hold the shared write lock so this commit cannot flush another writer's
        # in-flight transaction on the shared connection (see module docstring).
        async with self._write_lock:
            try:
                await conn.execute(
                    "INSERT INTO sheets (channel_id, sheet, num_exercises, hub_message_id) "
                    "VALUES (?, ?, ?, ?);",
                    (channel_id, sheet, num_exercises, hub_message_id),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                # Duplicate (channel_id, sheet): roll back any partial state and report failure.
                await conn.rollback()
                return False

    async def set_hub_message(
        self, channel_id: int, sheet: int, hub_message_id: int
    ) -> None:
        """Record the id of the hub message that links the sheet's threads."""
        conn = self._connection
        async with self._write_lock:
            await conn.execute(
                "UPDATE sheets SET hub_message_id = ? WHERE channel_id = ? AND sheet = ?;",
                (hub_message_id, channel_id, sheet),
            )
            await conn.commit()

    async def get_sheet(self, channel_id: int, sheet: int) -> SheetRow | None:
        """Fetch a single sheet, or ``None`` if it does not exist."""
        conn = self._connection
        async with conn.execute(
            "SELECT channel_id, sheet, num_exercises, hub_message_id, created_at "
            "FROM sheets WHERE channel_id = ? AND sheet = ?;",
            (channel_id, sheet),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return SheetRow(
            channel_id=row["channel_id"],
            sheet=row["sheet"],
            num_exercises=row["num_exercises"],
            hub_message_id=row["hub_message_id"],
            created_at=row["created_at"],
        )

    # -- threads -------------------------------------------------------------

    async def add_thread(
        self,
        channel_id: int,
        sheet: int,
        exercise_index: int,
        thread_id: int,
        thread_name: str,
    ) -> None:
        """Store the mapping from an exercise index to its Discord thread.

        Recorded at thread-creation time so ``/build`` never has to parse thread names (users
        may rename them). Uses ``INSERT OR REPLACE`` so re-running creation for the same
        ``(channel, sheet, exercise_index)`` overwrites rather than raising.
        """
        conn = self._connection
        async with self._write_lock:
            await conn.execute(
                "INSERT OR REPLACE INTO threads "
                "(channel_id, sheet, exercise_index, thread_id, thread_name) "
                "VALUES (?, ?, ?, ?, ?);",
                (channel_id, sheet, exercise_index, thread_id, thread_name),
            )
            await conn.commit()

    async def get_threads(self, channel_id: int, sheet: int) -> list[ThreadRow]:
        """Return all thread mappings for a sheet, ordered by ``exercise_index`` ascending."""
        conn = self._connection
        async with conn.execute(
            "SELECT channel_id, sheet, exercise_index, thread_id, thread_name "
            "FROM threads WHERE channel_id = ? AND sheet = ? "
            "ORDER BY exercise_index ASC;",
            (channel_id, sheet),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ThreadRow(
                channel_id=row["channel_id"],
                sheet=row["sheet"],
                exercise_index=row["exercise_index"],
                thread_id=row["thread_id"],
                thread_name=row["thread_name"],
            )
            for row in rows
        ]

    async def find_thread(self, thread_id: int) -> ThreadRow | None:
        """Locate which ``(channel, sheet, exercise_index)`` a thread belongs to.

        Used by ``/pick`` to identify the exercise it is running inside. Thread ids are globally
        unique, so no channel argument is needed. Returns ``None`` if the thread is not one the
        bot created.
        """
        conn = self._connection
        async with conn.execute(
            "SELECT channel_id, sheet, exercise_index, thread_id, thread_name "
            "FROM threads WHERE thread_id = ?;",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return ThreadRow(
            channel_id=row["channel_id"],
            sheet=row["sheet"],
            exercise_index=row["exercise_index"],
            thread_id=row["thread_id"],
            thread_name=row["thread_name"],
        )

    # -- selections ----------------------------------------------------------

    async def replace_selection(
        self,
        channel_id: int,
        sheet: int,
        exercise_index: int,
        parts: list[Part],
    ) -> None:
        """Atomically replace an exercise's entire selection.

        Within a single transaction: DELETE every existing selection row for
        ``(channel_id, sheet, exercise_index)``, then INSERT one row per image, recording
        ``part_index`` (1-based, in the order parts are given), ``part_label``, ``img_position``
        (taken from each :class:`ImageRef`), and the image's message/attachment/url metadata.

        The whole sequence runs under an :class:`asyncio.Lock` so two concurrent ``/pick``
        confirmations can never interleave their delete+insert. On any error the transaction is
        rolled back and the exception re-raised, leaving the prior selection intact.
        """
        conn = self._connection

        # Flatten the part list into one INSERT tuple per image, assigning 1-based part_index.
        insert_rows: list[
            tuple[int, int, int, int, str, int, int, int, str, str, str | None]
        ] = []
        for part_index, part in enumerate(parts, start=1):
            for image in part.images:
                insert_rows.append(
                    (
                        channel_id,
                        sheet,
                        exercise_index,
                        part_index,
                        part.label,
                        image.img_position,
                        image.message_id,
                        image.attachment_id,
                        image.url,
                        image.filename,
                        image.content_type,
                    )
                )

        async with self._write_lock:
            try:
                await conn.execute(
                    "DELETE FROM selections "
                    "WHERE channel_id = ? AND sheet = ? AND exercise_index = ?;",
                    (channel_id, sheet, exercise_index),
                )
                if insert_rows:
                    await conn.executemany(
                        "INSERT INTO selections "
                        "(channel_id, sheet, exercise_index, part_index, part_label, "
                        "img_position, message_id, attachment_id, url, filename, content_type) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        insert_rows,
                    )
                # Single commit for the combined delete+insert -> atomic replacement.
                await conn.commit()
            except Exception:
                # Undo the partial delete/insert so the previous selection survives.
                await conn.rollback()
                raise

    async def get_selection(
        self, channel_id: int, sheet: int, exercise_index: int
    ) -> list[Part]:
        """Reconstruct the ordered parts for one exercise.

        Parts come back ordered by ``part_index``; each part's images are ordered by
        ``img_position``. Returns an empty list if the exercise has no selection.
        """
        conn = self._connection
        async with conn.execute(
            "SELECT part_index, part_label, img_position, message_id, attachment_id, "
            "url, filename, content_type "
            "FROM selections "
            "WHERE channel_id = ? AND sheet = ? AND exercise_index = ? "
            "ORDER BY part_index ASC, img_position ASC;",
            (channel_id, sheet, exercise_index),
        ) as cursor:
            rows = await cursor.fetchall()
        return self._rows_to_parts(rows)

    async def get_all_selections(
        self, channel_id: int, sheet: int
    ) -> dict[int, list[Part]]:
        """Return every exercise's parts for a sheet.

        Maps ``exercise_index -> list[Part]`` for each exercise that has at least one selection
        row. Within each exercise, parts are ordered by ``part_index`` and images by
        ``img_position``.
        """
        conn = self._connection
        async with conn.execute(
            "SELECT exercise_index, part_index, part_label, img_position, message_id, "
            "attachment_id, url, filename, content_type "
            "FROM selections "
            "WHERE channel_id = ? AND sheet = ? "
            "ORDER BY exercise_index ASC, part_index ASC, img_position ASC;",
            (channel_id, sheet),
        ) as cursor:
            rows = await cursor.fetchall()

        # Bucket rows by exercise_index, preserving the SQL ordering, then build parts per bucket.
        by_exercise: dict[int, list[aiosqlite.Row]] = {}
        for row in rows:
            by_exercise.setdefault(row["exercise_index"], []).append(row)

        return {
            exercise_index: self._rows_to_parts(exercise_rows)
            for exercise_index, exercise_rows in by_exercise.items()
        }

    # -- per-channel config --------------------------------------------------

    async def get_channel_config(self, channel_id: int) -> ChannelConfig:
        """Return the per-channel overrides, or an all-``None`` config if unset."""
        conn = self._connection
        async with conn.execute(
            "SELECT group_number, course, tutorium, authors, language "
            "FROM channel_config WHERE channel_id = ?;",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return ChannelConfig()
        return ChannelConfig(
            group_number=row["group_number"],
            course=row["course"],
            tutorium=row["tutorium"],
            authors=row["authors"],
            language=row["language"],
        )

    async def set_channel_config(
        self,
        channel_id: int,
        *,
        group_number: str | None | object = _UNSET,
        course: str | None | object = _UNSET,
        tutorium: str | None | object = _UNSET,
        authors: str | None | object = _UNSET,
        language: str | None | object = _UNSET,
    ) -> ChannelConfig:
        """Update only the explicitly-passed fields of a channel's config; return the result.

        Read-modify-write under the shared write lock: fields left at the default
        (``_UNSET``) keep their stored value, while passing ``None`` or ``""`` clears a
        field (stored as NULL). Empty/whitespace strings normalise to NULL so a cleared
        field falls back to the ``.env``/``exercise.sty`` default.
        """

        def _norm(value: str | None | object, current: str | None) -> str | None:
            if value is _UNSET:
                return current
            if value is None:
                return None
            text = str(value).strip()
            return text or None

        conn = self._connection
        async with self._write_lock:
            # Read current row inside the lock so concurrent /config calls don't clobber.
            async with conn.execute(
                "SELECT group_number, course, tutorium, authors, language "
                "FROM channel_config WHERE channel_id = ?;",
                (channel_id,),
            ) as cursor:
                row = await cursor.fetchone()
            cur = ChannelConfig(
                group_number=row["group_number"] if row else None,
                course=row["course"] if row else None,
                tutorium=row["tutorium"] if row else None,
                authors=row["authors"] if row else None,
                language=row["language"] if row else None,
            )
            new = ChannelConfig(
                group_number=_norm(group_number, cur.group_number),
                course=_norm(course, cur.course),
                tutorium=_norm(tutorium, cur.tutorium),
                authors=_norm(authors, cur.authors),
                language=_norm(language, cur.language),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO channel_config "
                "(channel_id, group_number, course, tutorium, authors, language) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (
                    channel_id,
                    new.group_number,
                    new.course,
                    new.tutorium,
                    new.authors,
                    new.language,
                ),
            )
            await conn.commit()
            return new

    # -- reconstruction helper ----------------------------------------------

    @staticmethod
    def _rows_to_parts(rows: list[aiosqlite.Row]) -> list[Part]:
        """Group already-ordered selection rows into ordered :class:`Part` objects.

        ``rows`` MUST already be ordered by ``part_index`` then ``img_position`` (the queries
        guarantee this). Rows are grouped by ``part_index`` so contiguous runs become one
        :class:`Part`; the part's ``label`` is taken from ``part_label`` (identical across a
        part's rows).
        """
        parts: list[Part] = []
        current_index: int | None = None
        current_part: Part | None = None

        for row in rows:
            part_index = row["part_index"]
            if part_index != current_index:
                # Start a new part whenever the part_index changes.
                current_part = Part(label=row["part_label"], images=[])
                parts.append(current_part)
                current_index = part_index

            assert current_part is not None  # for type-checkers; set on the branch above
            current_part.images.append(
                ImageRef(
                    img_position=row["img_position"],
                    message_id=row["message_id"],
                    attachment_id=row["attachment_id"],
                    url=row["url"],
                    filename=row["filename"],
                    content_type=row["content_type"],
                )
            )

        return parts
