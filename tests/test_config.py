"""Tests for :mod:`bot.config` — environment loading and validation.

These pin the channel-list parsing contract introduced when the bot moved from a single
``ALLOWED_CHANNEL_ID`` to a list of operating channels (one per submission group):

    * ``ALLOWED_CHANNEL_IDS`` is a comma/space-separated list parsed in order, de-duplicated;
    * the singular ``ALLOWED_CHANNEL_ID`` is still accepted as a backward-compatible alias;
    * both sources merge (list first, then the alias), preserving order and de-duplicating;
    * if *neither* is set, :class:`ConfigError` is raised and mentions ``ALLOWED_CHANNEL_IDS``.

They also cover the optional header defaults (``GROUP_NUMBER`` / ``COURSE`` / ``TUTORIUM`` /
``AUTHORS``) parsing into the matching :class:`Settings` fields, or ``None`` when absent.

Isolation strategy (mirrors the other suites)
----------------------------------------------
``load_settings`` reads the real process environment with ``override=False``, so a stray
``DISCORD_TOKEN`` (etc.) in the developer's shell would leak into these tests. Each test runs
through :func:`_load` which writes a throwaway ``.env`` under pytest's ``tmp_path`` and clears
every env var the loader reads from ``os.environ`` first, so only the values we pass take
effect. ``LATEX_PROJECT_DIR`` points at the bundled template (``./latex-project``) so the
up-front directory check passes; pytest runs with the repo root as cwd, so the relative path
resolves.

``sys.path`` is prepended with the repository root so ``import bot.config`` resolves when
pytest is invoked from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --- make ``import bot.config`` resolve from the repo root -------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.config import ConfigError, Settings, load_settings  # noqa: E402

# Every environment variable the loader inspects. Cleared before each load so the host
# shell can't leak values into the test, and so a value omitted from a test's ``env`` dict
# is genuinely *unset* (not inherited from a previous test or the real environment).
_ENV_KEYS = (
    "DISCORD_TOKEN",
    "GUILD_ID",
    "ALLOWED_CHANNEL_IDS",
    "ALLOWED_CHANNEL_ID",
    "LATEX_PROJECT_DIR",
    "DB_PATH",
    "TEX_CMD",
    "DOWNSCALE_MAX_PX",
    "ANNOUNCE_PICKS",
    "GROUP_NUMBER",
    "COURSE",
    "TUTORIUM",
    "AUTHORS",
    "LANGUAGE",
)


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Write a throwaway ``.env`` with ``env`` and load it with a clean environment.

    Clears every loader-read key from ``os.environ`` (so the host shell and prior tests can't
    leak in), points the DB at ``tmp_path`` and the LaTeX project at the real repo project
    (unless overridden), writes the ``.env`` file, and returns the loaded :class:`Settings`.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    values: dict[str, str] = {
        # Sensible defaults so a test only needs to specify the vars it cares about.
        "LATEX_PROJECT_DIR": "./latex-project",
        "DB_PATH": str(tmp_path / "latexercise.sqlite3"),
    }
    values.update(env)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return load_settings(env_path=env_file)


# A complete, valid baseline so individual tests can override just one variable.
_BASE = {
    "DISCORD_TOKEN": "token-xyz",
    "GUILD_ID": "424242",
    "ALLOWED_CHANNEL_IDS": "111",
}


# ---------------------------------------------------------------------------
# ALLOWED_CHANNEL_IDS list parsing
# ---------------------------------------------------------------------------


def test_allowed_channel_ids_list_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma- and space-separated IDs parse into an ordered tuple of ints."""
    settings = _load(
        tmp_path,
        monkeypatch,
        DISCORD_TOKEN="token-xyz",
        GUILD_ID="424242",
        ALLOWED_CHANNEL_IDS="111, 222 333",
    )
    assert settings.allowed_channel_ids == (111, 222, 333)


def test_allowed_channel_ids_dedup_preserves_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate IDs collapse to one, keeping first-seen order."""
    settings = _load(
        tmp_path,
        monkeypatch,
        **{**_BASE, "ALLOWED_CHANNEL_IDS": "111, 222, 111, 333, 222"},
    )
    assert settings.allowed_channel_ids == (111, 222, 333)


def test_allowed_channel_id_singular_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The singular ``ALLOWED_CHANNEL_ID`` is accepted on its own (backward compat)."""
    settings = _load(
        tmp_path,
        monkeypatch,
        DISCORD_TOKEN="token-xyz",
        GUILD_ID="424242",
        ALLOWED_CHANNEL_ID="777",
        # ALLOWED_CHANNEL_IDS deliberately absent.
    )
    assert settings.allowed_channel_ids == (777,)


def test_allowed_channel_ids_merges_plural_and_singular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sources merge — plural first, then the alias — with de-duplication."""
    settings = _load(
        tmp_path,
        monkeypatch,
        DISCORD_TOKEN="token-xyz",
        GUILD_ID="424242",
        ALLOWED_CHANNEL_IDS="111, 222",
        ALLOWED_CHANNEL_ID="333 111",  # 333 is new, 111 already seen -> dropped
    )
    assert settings.allowed_channel_ids == (111, 222, 333)


def test_missing_both_channel_vars_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If neither channel var is set, ConfigError is raised and names ALLOWED_CHANNEL_IDS."""
    with pytest.raises(ConfigError) as excinfo:
        _load(
            tmp_path,
            monkeypatch,
            DISCORD_TOKEN="token-xyz",
            GUILD_ID="424242",
            # Both ALLOWED_CHANNEL_IDS and ALLOWED_CHANNEL_ID absent.
        )
    assert "ALLOWED_CHANNEL_IDS" in str(excinfo.value)


# ---------------------------------------------------------------------------
# optional header defaults (group/course/tutorium/authors)
# ---------------------------------------------------------------------------


def test_header_defaults_parsed_into_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GROUP_NUMBER/COURSE/TUTORIUM/AUTHORS populate the matching Settings fields."""
    settings = _load(
        tmp_path,
        monkeypatch,
        **{
            **_BASE,
            "GROUP_NUMBER": "017",
            "COURSE": "GLOIN",
            "TUTORIUM": "Tut 3",
            "AUTHORS": "Anna, 1; Ben, 2",
        },
    )
    assert settings.group_number == "017"
    assert settings.course == "GLOIN"
    assert settings.tutorium == "Tut 3"
    assert settings.authors == "Anna, 1; Ben, 2"


def test_header_defaults_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the header vars are unset, the Settings fields default to None."""
    settings = _load(tmp_path, monkeypatch, **_BASE)
    assert settings.group_number is None
    assert settings.course is None
    assert settings.tutorium is None
    assert settings.authors is None


# ---------------------------------------------------------------------------
# LANGUAGE (output language default)
# ---------------------------------------------------------------------------


def test_language_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset LANGUAGE leaves Settings.language as None (bot default, English)."""
    settings = _load(tmp_path, monkeypatch, **_BASE)
    assert settings.language is None


def test_language_canonical_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LANGUAGE=de / en are stored as the canonical code."""
    assert _load(tmp_path, monkeypatch, **{**_BASE, "LANGUAGE": "de"}).language == "de"
    assert _load(tmp_path, monkeypatch, **{**_BASE, "LANGUAGE": "en"}).language == "en"


def test_language_aliases_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Friendly spellings are normalized to the canonical code at load time."""
    assert _load(tmp_path, monkeypatch, **{**_BASE, "LANGUAGE": "Deutsch"}).language == "de"
    assert _load(tmp_path, monkeypatch, **{**_BASE, "LANGUAGE": "English"}).language == "en"


def test_language_invalid_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized LANGUAGE is a hard, actionable error (not a silent fallback)."""
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, monkeypatch, **{**_BASE, "LANGUAGE": "klingon"})
    assert "LANGUAGE" in str(excinfo.value)
