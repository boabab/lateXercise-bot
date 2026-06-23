"""Configuration loading and validation for the lateXercise bot.

This module owns the single source of truth for runtime configuration. It reads a
``.env`` file (via :func:`python-dotenv.load_dotenv` with ``override=False`` so that
real process environment — e.g. systemd ``EnvironmentFile`` or launchd — always wins
over file values) and produces a fully validated, immutable :class:`Settings` object.

Design goals:

* **Fail loud, fail once.** All missing/invalid variables are collected and reported
  together in a single :class:`ConfigError`, so an operator fixes everything in one
  pass instead of rerunning to discover the next problem.
* **Paths are resolved to absolute** at load time, so every downstream module
  (``latex``, ``images``, ``store``) can rely on absolute paths without re-resolving.
* **The LaTeX project is verified up front** — it must exist and contain
  ``exercise.sty`` — because ``/build`` depends on compiling against that project.
* **The database directory is created eagerly** so the first ``store.init()`` never
  fails on a missing parent directory.

See ``data/CONTRACTS.md`` (section "bot/config.py") for the authoritative contract.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_log = logging.getLogger(__name__)

__all__ = ["Settings", "ConfigError", "load_settings"]


@dataclass(frozen=True)
class Settings:
    """Immutable, fully validated runtime configuration.

    All fields are produced by :func:`load_settings`; never construct this directly
    from raw environment values, as the validation lives in the loader.

    Attributes:
        discord_token: Bot token from the Discord Developer Portal (secret).
        guild_id: Discord guild (server) ID; commands are synced guild-scoped to it.
        allowed_channel_ids: The text channels the bot may operate in — one per
            submission group. Each channel is independent (own sheets/picks/config).
        latex_project_dir: Absolute path to the LaTeX project root. Guaranteed to exist
            and to contain ``exercise.sty``.
        db_path: Absolute path to the SQLite database file. Its parent directory is
            guaranteed to exist (created during load if missing).
        tex_cmd: Optional override for the TeX compiler binary (path or name). ``None``
            when unset or blank, in which case ``latex.resolve_compiler`` picks one.
        downscale_max_px: Optional longest-side pixel cap for image downscaling. ``None``
            when unset, blank, or ``<= 0`` (the downscale feature is then disabled).
        announce_picks: Whether ``/pick`` posts a public confirmation in the thread.
            Defaults to ``True``.
        group_number: Optional default for the group number (``\\ExerciseGroup``) and the
            PDF filename. ``None`` when unset. Overridden per-guild by ``/konfig``.
        course: Optional default for the course header (``\\exerciseCourse``).
        tutorium: Optional default for the tutorium header (``\\exerciseGroup``).
        authors: Optional default author list, ``;``-separated (``\\exerciseAuthors``).
    """

    discord_token: str
    guild_id: int
    allowed_channel_ids: tuple[int, ...]  # >=1 operating channels (submission groups)
    latex_project_dir: Path  # resolved absolute Path; exists & contains exercise.sty
    db_path: Path  # resolved absolute Path; parent dir created if missing
    tex_cmd: str | None  # None when unset/blank
    downscale_max_px: int | None  # None when unset/blank/<=0 (feature disabled)
    announce_picks: bool  # default True
    # Optional header defaults (fallbacks below per-guild /konfig overrides). None = unset.
    group_number: str | None = None
    course: str | None = None
    tutorium: str | None = None
    authors: str | None = None


class ConfigError(Exception):
    """Raised when configuration is missing or invalid.

    The message lists *all* detected problems at once so the operator can fix them in
    a single edit of the ``.env`` file or environment.
    """


# Default relative path to the LaTeX project (the folder containing exercise.sty).
_DEFAULT_LATEX_PROJECT_DIR = "./latex-project"
# Default relative path to the SQLite database file.
_DEFAULT_DB_PATH = "./data/latexercise.sqlite3"
# File that must live inside LATEX_PROJECT_DIR for it to be a valid project.
_REQUIRED_PROJECT_FILE = "exercise.sty"
# Truthy spellings accepted for boolean env vars (case-insensitive after strip).
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off", ""})


def _get_clean(name: str) -> str | None:
    """Return the stripped value of env var ``name``, or ``None`` if unset/blank.

    Treating a whitespace-only value the same as "unset" keeps validation simple:
    a present-but-empty ``.env`` line (``DISCORD_TOKEN=``) is reported as missing.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def _parse_int(name: str, value: str, errors: list[str]) -> int | None:
    """Parse ``value`` as an int for env var ``name``.

    On failure, append an actionable message to ``errors`` and return ``None`` so the
    caller can continue collecting further problems instead of aborting early.
    """
    try:
        return int(value)
    except ValueError:
        errors.append(f"{name} must be an integer, got {value!r}.")
        return None


def _parse_bool(name: str, value: str | None, *, default: bool, errors: list[str]) -> bool:
    """Parse a truthy/falsy env var (case-insensitive).

    ``None`` (unset/blank) yields ``default``. Recognized truthy values are
    true/1/yes/on; recognized falsy values are false/0/no/off. Anything else is an
    error appended to ``errors`` (and ``default`` is returned as a placeholder).
    """
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        # Note: a blank string is handled upstream by _get_clean -> None, but we keep
        # "" in _FALSE_VALUES defensively in case a raw value reaches here.
        return False
    errors.append(
        f"{name} must be a boolean "
        f"(one of true/1/yes/on or false/0/no/off), got {value!r}."
    )
    return default


def load_settings(env_path: str | Path = ".env") -> Settings:
    """Load and validate configuration, returning an immutable :class:`Settings`.

    The ``.env`` file at ``env_path`` is loaded with ``override=False`` so the real
    process environment (systemd/launchd) takes precedence over file contents. Every
    detected problem — missing required vars, non-integer IDs, a missing LaTeX project
    or a project missing ``exercise.sty``, or an undirectory-able DB path — is collected
    and raised together in one :class:`ConfigError`.

    Args:
        env_path: Path to the ``.env`` file. Missing files are silently tolerated by
            python-dotenv (real env vars may supply everything).

    Returns:
        A fully validated :class:`Settings` with absolute paths.

    Raises:
        ConfigError: If any required variable is missing or any value is invalid. The
            message lists all problems at once.
    """
    # override=False: a value already present in the real environment wins over the
    # .env file, matching production deployment under systemd/launchd. A non-existent
    # env_path is a no-op (env vars alone may suffice), which is the desired behavior.
    load_dotenv(env_path, override=False)

    errors: list[str] = []

    # --- required string: DISCORD_TOKEN -------------------------------------------
    discord_token = _get_clean("DISCORD_TOKEN")
    if discord_token is None:
        errors.append("DISCORD_TOKEN is required but missing or empty.")

    # --- required ints: GUILD_ID, ALLOWED_CHANNEL_ID ------------------------------
    guild_id: int | None = None
    guild_id_raw = _get_clean("GUILD_ID")
    if guild_id_raw is None:
        errors.append("GUILD_ID is required but missing or empty.")
    else:
        guild_id = _parse_int("GUILD_ID", guild_id_raw, errors)

    # ALLOWED_CHANNEL_IDS is a comma/space-separated list (one operating channel per
    # submission group). The singular ALLOWED_CHANNEL_ID is still accepted as a
    # backward-compatible alias and merged in. At least one valid id is required.
    allowed_channel_ids: tuple[int, ...] = ()
    raw_list = _get_clean("ALLOWED_CHANNEL_IDS")
    raw_single = _get_clean("ALLOWED_CHANNEL_ID")
    if raw_list is None and raw_single is None:
        errors.append(
            "ALLOWED_CHANNEL_IDS is required but missing or empty "
            "(comma-separated channel IDs; ALLOWED_CHANNEL_ID is also accepted)."
        )
    else:
        # Split on commas and whitespace; parse each, preserving order, de-duplicating.
        tokens: list[str] = []
        for source in (raw_list, raw_single):
            if source:
                tokens.extend(t for t in re.split(r"[,\s]+", source) if t)
        parsed_ids: list[int] = []
        seen: set[int] = set()
        for tok in tokens:
            value = _parse_int("ALLOWED_CHANNEL_IDS", tok, errors)
            if value is not None and value not in seen:
                seen.add(value)
                parsed_ids.append(value)
        if not parsed_ids and not any("ALLOWED_CHANNEL_IDS" in e for e in errors):
            errors.append("ALLOWED_CHANNEL_IDS contained no valid channel IDs.")
        allowed_channel_ids = tuple(parsed_ids)

    # --- LATEX_PROJECT_DIR: resolve, must exist & contain exercise.sty ------------
    latex_dir_raw = _get_clean("LATEX_PROJECT_DIR") or _DEFAULT_LATEX_PROJECT_DIR
    # Resolve to an absolute path; resolve() also collapses ".." / "." segments.
    latex_project_dir = Path(latex_dir_raw).expanduser().resolve()
    if not latex_project_dir.exists():
        errors.append(
            f"LATEX_PROJECT_DIR does not exist: {latex_project_dir} "
            f"(from {latex_dir_raw!r}). Point it at the folder containing exercise.sty."
        )
    elif not latex_project_dir.is_dir():
        errors.append(
            f"LATEX_PROJECT_DIR is not a directory: {latex_project_dir} "
            f"(from {latex_dir_raw!r})."
        )
    elif not (latex_project_dir / _REQUIRED_PROJECT_FILE).is_file():
        errors.append(
            f"LATEX_PROJECT_DIR is missing {_REQUIRED_PROJECT_FILE}: "
            f"{latex_project_dir}. This does not look like a LaTeX exercise project."
        )

    # --- DB_PATH: resolve to absolute, create parent dir if missing ---------------
    db_path_raw = _get_clean("DB_PATH") or _DEFAULT_DB_PATH
    db_path = Path(db_path_raw).expanduser().resolve()
    db_parent = db_path.parent
    try:
        # Eagerly create the directory tree so store.init() never trips on it.
        db_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        errors.append(
            f"Could not create directory for DB_PATH parent {db_parent}: {exc}."
        )

    # --- optional: TEX_CMD --------------------------------------------------------
    # Blank/unset -> None; resolve_compiler() in latex.py then auto-detects a binary.
    tex_cmd = _get_clean("TEX_CMD")
    # Defense-in-depth: if someone copies .env.example and leaves the line as
    # `TEX_CMD=   # comment`, python-dotenv parses the trailing comment as the VALUE
    # (it only strips inline comments after a non-empty value). A real compiler path can
    # never start with '#', so treat such a value as unset rather than failing the build
    # with a confusing "compiler could not be resolved" error.
    if tex_cmd is not None and tex_cmd.startswith("#"):
        _log.warning(
            "Ignoring TEX_CMD=%r — it looks like a leftover comment from .env.example, "
            "not a compiler path. Falling back to auto-detection (latexmk/pdflatex on PATH).",
            tex_cmd,
        )
        tex_cmd = None

    # --- optional: DOWNSCALE_MAX_PX ----------------------------------------------
    # Unset/blank -> None (disabled). A non-positive value also disables the feature.
    # A non-integer value is a hard error (it is almost certainly a typo).
    downscale_max_px: int | None = None
    downscale_raw = _get_clean("DOWNSCALE_MAX_PX")
    if downscale_raw is not None:
        parsed = _parse_int("DOWNSCALE_MAX_PX", downscale_raw, errors)
        if parsed is not None and parsed > 0:
            downscale_max_px = parsed
        # parsed <= 0 intentionally leaves downscale_max_px as None (feature disabled).

    # --- optional: ANNOUNCE_PICKS (default True) ---------------------------------
    announce_picks = _parse_bool(
        "ANNOUNCE_PICKS", _get_clean("ANNOUNCE_PICKS"), default=True, errors=errors
    )

    # --- optional: header defaults (group/course/tutorium/authors) ----------------
    # Blank/unset -> None (fall back to exercise.sty). Per-guild /konfig overrides win
    # over these at build time. Stored as raw strings; AUTHORS is ';'-separated.
    group_number = _get_clean("GROUP_NUMBER")
    course = _get_clean("COURSE")
    tutorium = _get_clean("TUTORIUM")
    authors = _get_clean("AUTHORS")

    # --- raise everything at once -------------------------------------------------
    if errors:
        bullet_list = "\n".join(f"  - {message}" for message in errors)
        raise ConfigError(
            "Configuration is invalid. Fix the following and try again:\n"
            f"{bullet_list}\n"
            "See .env.example for the expected variables."
        )

    # All required values are guaranteed non-None here because any None would have
    # appended an error above and we would have raised. The assertions document that
    # invariant for type checkers and guard against future refactoring mistakes.
    assert discord_token is not None
    assert guild_id is not None
    assert allowed_channel_ids  # non-empty tuple guaranteed (else an error was appended)

    return Settings(
        discord_token=discord_token,
        guild_id=guild_id,
        allowed_channel_ids=allowed_channel_ids,
        latex_project_dir=latex_project_dir,
        db_path=db_path,
        tex_cmd=tex_cmd,
        downscale_max_px=downscale_max_px,
        announce_picks=announce_picks,
        group_number=group_number,
        course=course,
        tutorium=tutorium,
        authors=authors,
    )
