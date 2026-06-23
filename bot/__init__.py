"""The ``bot`` package: configuration, persistence, LaTeX, images, UI, and cogs.

This package groups every runtime module of the lateXercise bot. It intentionally
contains no import-time side effects so that the pure, framework-free modules
(``config``, ``latex``, ``store``, ``ui``'s parser) can be imported and unit-tested
without pulling in ``discord`` or a TeX/Pillow runtime.

The console entrypoint lives in the top-level ``bot.py`` module (one directory up),
not here, so that ``python bot.py`` works from the repository root while ``from bot.*
import ...`` continues to address this package.
"""

from __future__ import annotations
