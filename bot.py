"""Entrypoint for the lateXercise bot.

Run from the repository root with::

    python bot.py

This module wires the pieces together: it loads and validates configuration
(:func:`bot.config.load_settings`), constructs the SQLite :class:`bot.store.Store`,
builds a :class:`discord.ext.commands.Bot` subclass, registers the
:class:`bot.cogs.exercises.Exercises` cog, syncs the application commands to the single
configured guild, and runs the client until interrupted — closing the store cleanly on
shutdown.

Intents (see ``data/CONTRACTS.md`` "CRITICAL CORRECTIONS FROM RESEARCH" #1)
--------------------------------------------------------------------------
We start from :meth:`discord.Intents.none` and enable exactly two gateway intents:

* ``guilds`` — required to receive guild/thread objects and to create/manage the
  exercise threads ``/sheet`` and ``/build`` operate on.
* ``message_content`` — a **privileged** intent that MUST be enabled, both here and in
  the Discord Developer Portal. ``/pick`` reads other group members' uploaded photos via
  ``thread.history()``; Discord gates ``content``/``attachments``/``embeds``/``components``
  behind the Message Content intent for any message NOT authored by the bot, NOT a DM, and
  NOT @mentioning the bot. Without it, attachments on other users' messages come back
  empty and ``/pick`` cannot see the candidate images. The ``members`` and ``presences``
  privileged intents stay OFF (we never need member lists or presence).

We use ``commands.Bot`` (not the bare ``discord.Client``) for its cog/command-tree
plumbing. ``commands.Bot`` requires a ``command_prefix`` even when only slash commands
are used; we pass ``"!"`` purely to satisfy that requirement — no message (prefix)
commands are ever registered, so it is never triggered (correction #2).

Command sync is **guild-scoped** for instant propagation: copying the globally declared
commands to the configured guild and syncing there avoids the up-to-one-hour delay of a
true global sync.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

from bot.cogs.exercises import Exercises
from bot.config import ConfigError, Settings, load_settings
from bot.store import Store

__all__ = ["LateXerciseBot", "main"]

# Module-level logger; configured in main() via logging.basicConfig.
log = logging.getLogger("latexercise.bot")


def _build_intents() -> discord.Intents:
    """Construct the minimal intent set the bot needs.

    Starts from no intents and enables only ``guilds`` and the privileged
    ``message_content`` intent. See the module docstring and correction #1 for why
    ``message_content`` is mandatory (reading other users' attachments in ``/pick``).
    """
    intents = discord.Intents.none()
    # Needed for guild/thread objects and thread management used by /sheet and /build.
    intents.guilds = True
    # PRIVILEGED: required to read attachments on messages the bot did not author, which
    # is exactly what /pick does when scanning thread.history() for candidate photos.
    # Must also be toggled ON in the Discord Developer Portal. (correction #1)
    intents.message_content = True
    return intents


class LateXerciseBot(commands.Bot):
    """The lateXercise bot client.

    Holds the validated :class:`Settings` and the shared :class:`Store`, opens the store
    and registers the cog in :meth:`setup_hook`, and syncs commands to the single
    configured guild. The store's lifecycle (init/close) is owned by :func:`main` so that
    it is closed even if ``setup_hook`` or login fails; this class only *uses* it.
    """

    def __init__(self, settings: Settings, store: Store) -> None:
        """Create the bot with the minimal intents and a (never-used) command prefix.

        Args:
            settings: Fully validated runtime configuration.
            store: An already-constructed (but not yet ``init``-ed) SQLite store; this
                bot's :meth:`setup_hook` calls ``await store.init()``.
        """
        super().__init__(
            # Required by commands.Bot even though no prefix commands exist (correction #2).
            command_prefix="!",
            intents=_build_intents(),
            # No application/help/owner features beyond slash commands are needed.
            help_command=None,
        )
        self.settings: Settings = settings
        self.store: Store = store

    async def setup_hook(self) -> None:
        """Initialise the store, register the cog, and sync commands to the guild.

        ``setup_hook`` runs once after login but before the gateway connection is fully
        ready, which is the discord.py-recommended place to do async setup and command
        syncing. We:

        1. ``await store.init()`` — open the DB connection and create tables.
        2. add the :class:`Exercises` cog (which declares ``/sheet``, ``/pick``, ``/build``).
        3. copy the globally declared app commands onto the configured guild and sync there,
           so the commands appear in that guild within seconds instead of up to an hour.
        """
        # Open the database (WAL, busy_timeout, schema, migrations). Must happen before the
        # cog handles any interaction; setup_hook completes before commands can be invoked.
        # The first configured channel is the migration backfill target when upgrading an
        # older guild-keyed database to the channel-keyed schema (a no-op for fresh DBs).
        legacy_channel = (
            self.settings.allowed_channel_ids[0]
            if self.settings.allowed_channel_ids
            else None
        )
        await self.store.init(legacy_channel_id=legacy_channel)
        log.info("Store initialised at %s", self.store_db_display())

        # Register the three application commands via the cog.
        await self.add_cog(Exercises(self, self.settings, self.store))

        # Guild-scoped sync for instant command propagation (correction: guild sync).
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info(
            "Synced %d application command(s) to guild %s.",
            len(synced),
            self.settings.guild_id,
        )

    async def on_ready(self) -> None:
        """Log a concise readiness banner once the gateway connection is established.

        ``on_ready`` can fire more than once (e.g. after a reconnect), so this only logs;
        it performs no setup (that lives in :meth:`setup_hook`).
        """
        user = self.user
        # Count the app commands currently registered on the tree (across all scopes).
        command_count = len(self.tree.get_commands())
        log.info(
            "Connected as %s (id=%s); %d application command(s) registered.",
            f"{user} ({user.id})" if user is not None else "<unknown>",
            user.id if user is not None else "?",
            command_count,
        )

    def store_db_display(self) -> str:
        """Return a human-friendly description of the database path for logs."""
        # Kept tiny and side-effect free; purely a logging convenience.
        return str(self.store._db_path)  # noqa: SLF001 - intentional read-only access for logging


async def _run(settings: Settings) -> None:
    """Construct the store and bot and run until disconnect, closing the store afterwards.

    Using ``bot.start`` (rather than the blocking ``bot.run``) inside an ``async`` function
    lets us guarantee, via ``try/finally``, that the shared SQLite connection is closed on
    every exit path — normal shutdown, ``KeyboardInterrupt``, or an exception during login
    or runtime. ``store.init()`` itself is performed in :meth:`LateXerciseBot.setup_hook`; closing
    is idempotent, so closing here even when init never ran is safe.

    Args:
        settings: Validated configuration to build the store and bot from.
    """
    store = Store(settings.db_path)
    bot = LateXerciseBot(settings, store)
    try:
        # bot.start logs in and connects; it returns when the connection is closed.
        await bot.start(settings.discord_token)
    finally:
        # Close the gateway/HTTP session first (no-op if never opened), then the DB.
        if not bot.is_closed():
            await bot.close()
        await store.close()
        log.info("Store closed; shutdown complete.")


def main() -> int:
    """Program entrypoint: load config, then run the bot.

    Configuration errors are reported clearly to stderr and cause a non-zero exit so an
    operator (or a process supervisor like systemd/launchd) sees the failure immediately.
    ``KeyboardInterrupt`` (Ctrl-C) is treated as a clean shutdown.

    Returns:
        Process exit code: ``0`` on clean shutdown, ``1`` on a configuration error.
    """
    # Basic logging to stderr; discord.py emits useful INFO/WARNING records under this.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Load and validate configuration up front. A ConfigError aggregates every problem.
    try:
        settings = load_settings()
    except ConfigError as exc:
        # Print to stderr (not the log) so the actionable, multi-line message is plain.
        print(str(exc), file=sys.stderr)
        return 1

    # asyncio.run owns the event loop; _run's try/finally guarantees the store is closed.
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        # Ctrl-C during startup/runtime is a normal, clean way to stop the bot.
        log.info("Interrupted; exiting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
