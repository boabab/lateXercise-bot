"""The ``bot.cogs`` package: discord.py command cogs.

Each module here defines a :class:`discord.ext.commands.Cog` that bundles related
application (slash) commands. Currently this is :mod:`bot.cogs.exercises`, which
provides ``/blatt``, ``/pick`` and ``/build``.

Cogs are added to the bot in ``bot.py``'s ``setup_hook`` and synced guild-scoped.
"""

from __future__ import annotations
