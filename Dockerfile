# syntax=docker/dockerfile:1
#
# lateXercise bot — runtime image.
#
#   Build:  docker build -t latexercise-bot .
#   Run:    docker run -d --name latexercise --restart unless-stopped \
#             -e DISCORD_TOKEN=...           \
#             -e GUILD_ID=...                \
#             -e ALLOWED_CHANNEL_IDS=111,222 \
#             -v latexercise-data:/data      \
#             latexercise-bot
#
# Configuration is read entirely from environment variables (see .env.example for the
# full list); no .env file is baked into the image. The SQLite database is written to
# the /data volume so it survives container restarts. The LaTeX project directory holds
# only throwaway per-build scratch (the ex<NN>/ folders are wiped and regenerated on
# every /build), so it deliberately does NOT need a persistent volume.

# Pin the Debian suite (bookworm) so a future retag of python:3.13-slim to a newer
# Debian release can't silently change the available TeX Live package set.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/latexercise.sqlite3 \
    LATEX_PROJECT_DIR=/app/latex-project

# --- LaTeX toolchain ----------------------------------------------------------------
# The bot shells out to `latexmk` (preferred), falling back to `pdflatex`, to compile
# the generated sheet. The bundled exercise.sty requires: KOMA-Script (scrartcl,
# scrlayer-scrpage), geometry, hyperref, graphicx, amsmath/mathtools/amssymb, tikz/pgf,
# microtype, enumitem, placeins, booktabs, multicol, xcolor, xspace.
#
# `texlive-latex-extra` pulls that entire chain in transitively:
#   texlive-latex-extra -> texlive-pictures            (tikz / pgf)
#                       -> texlive-latex-recommended   (KOMA-Script, mathtools, microtype, booktabs, xcolor)
#                       -> texlive-latex-base          (geometry, hyperref, graphicx, amsmath, babel+english, multicol, xspace, inputenc)
#                       -> texlive-base                (amsfonts/amssymb, Computer Modern fonts)
#
# Deliberately NOT installed (verified against the template's preamble):
#   * texlive-fonts-recommended — the template loads no font package, so the default
#     Computer Modern fonts (already in texlive-base) suffice.
#   * texlive-lang-english (~206 MB) — only adds English *hyphenation* patterns. The
#     bot's sheets are image figures plus section/paragraph headers with no flowing
#     body prose, so hyphenation is moot; babel just logs a harmless
#     "no hyphenation patterns were preloaded" warning.
#
# `texlive-lang-german` (~33 MB) IS included so the German babel toggle documented in
# exercise.sty (\RequirePackage[ngerman]{babel}) works out of the box, no rebuild.
#
# `--no-install-recommends` keeps apt from dragging in texlive-latex-extra's large
# *recommended* (non-dependency) siblings; the doc trees are then stripped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        texlive-latex-extra \
        texlive-lang-german \
        latexmk \
        tini \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
              /usr/share/doc/texlive-* \
              /usr/share/texlive/texmf-dist/doc

# --- application --------------------------------------------------------------------
# Dedicated non-root user. Created before COPY so the app tree can be owned by it — the
# bot writes per-build scratch (ex<NN>/, ex<NN>.tex, the compiled PDF) under
# /app/latex-project at runtime, and the SQLite DB under /data.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

WORKDIR /app

# Dependencies first, so the (large) TeX layer and the pip layer stay cached when only
# application code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=bot:bot . .

USER bot
VOLUME ["/data"]

LABEL org.opencontainers.image.source="https://github.com/boabab/lateXercise-bot" \
      org.opencontainers.image.description="Discord bot that builds LaTeX exercise-sheet PDFs from photos"

# tini as PID 1 forwards SIGTERM/SIGINT to Python (so `docker stop` shuts the gateway
# client down) and reaps the latexmk/pdflatex children the bot spawns. Exec-form CMD
# keeps Python as the direct child so it receives those signals.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
