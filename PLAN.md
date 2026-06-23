# lateXercise-bot — Implementation Plan

## Context

A study group (e.g. Group **000**, Tutorial 00, a course like *My Course 2026*) hand-writes exercise
solutions, photographs them, and assembles them into a LaTeX PDF submitted as
`Group_000_Sheet_YY.pdf`. Today that assembly is manual. This bot automates the
collect → decide → build → submit loop inside one Discord channel.

The LaTeX project already exists at `latex-project/` (sheets `ex1/`…`ex5/` plus
`exercise.sty`). The bot **reuses** it as-is — it never edits `exercise.sty`; it only
drops new `ex<NN>/` folders and a generated `.tex` into the project and compiles.

## User-facing workflow

1. **`/sheet <sheet> <num_exercises>`** — e.g. `/sheet 6 3`. Creates `num_exercises`
   public threads in the channel named `Sheet 06 · Exercise 1 … 3` and posts a hub
   message linking them. Everyone uploads candidate solution photos into the relevant
   exercise thread and discusses there.
2. **`/pick`** — run *inside* an exercise thread. Assemble the exercise **part by part**:
   for each sub-part (a, b, c, … — or a combined "a+b", or no sub-part at all) choose its
   winning image(s). The model is deliberately flexible (see below): a part may hold
   several photos (multi-page), one photo may cover several parts, and different people's
   photos can fill different parts. Part order and page order are preserved; the
   selection is saved.
3. **`/build <sheet>`** — e.g. `/build 6`. Gathers each exercise thread's picked
   parts/images in order, generates `ex06/ex06.tex` (with `\paragraph{(a)}` labels +
   figures), sets `\setExerciseSheet{06}`, compiles from the project root, and posts
   **`Group_000_Sheet_06.pdf`** into the channel as the reply.

## The sub-part model (core data shape)

Each exercise's selection is an **ordered list of parts**; each part has:

- an **optional label** — `"a"`, `"b"`, `"a) b)"` (combined photo), or `""` (the photo is
  the whole exercise, no sub-part);
- **one or more ordered images** (pages).

This single shape covers every real case seen in `ex1`–`ex5`: one photo for the whole
exercise; one photo per part (`ext2_q2a` + `ext2_q2b`); a photo spanning parts
(`ext3_q1ab`) plus a separate one (`ext3_q1c`); and multi-page parts (`a3-1` + `a3-2`).

## Confirmed decisions

| Decision | Choice |
|---|---|
| Language / lib | **Python + discord.py 2.x** (app commands / `discord.ui`) |
| Image → exercise mapping | **One thread per exercise** — the thread *is* the Exercise, no tagging |
| Winner selection | **Explicit `/pick`**, part-by-part, multiple winners allowed |
| Sub-part model | Exercise = ordered list of parts; part = optional label + 1+ ordered images |
| Image rendering | `\section*{Exercise N}`, then per labelled part `\paragraph{(a)}` + stacked figure(s) |
| Exercise count | **Given at creation** (`/sheet 6 3`) |
| Sheet number | Set at thread creation; zero-padded (`6` → `06`) |
| Runtime | **Portable** macOS (MacTeX) / Linux (TeX Live) — binaries resolved from `PATH` |

## Repo layout (new code alongside `latex-project/`)

```
bot/
  config.py        # .env loading + validation (Settings dataclass)
  store.py         # SQLite: sheets, threads (mapping), selections (parts+images)
  latex.py         # .tex generation + compile + log parsing (framework-free)
  images.py        # attachment download, format validation, naming, downscale
  ui.py            # discord.ui pick widgets (part-by-part builder)
  cogs/exercises.py# /sheet, /pick, /build app commands
bot.py             # entrypoint: client, load cog, guild-scoped sync, run
requirements.txt
.env.example
.gitignore         # .env, data/, build artifacts, generated latex-project/ex[0-9][0-9]/
data/              # runtime; data/latexercise.sqlite3
PLAN.md
```

## Load-bearing implementation notes

**Discord intents — none privileged.** We use slash commands + `Message.attachments`
+ `thread.history()`, never message *text* or reactions. `Message.attachments` is
populated without the Message Content intent, and reading history is a *permission*,
not an intent. So: `intents = discord.Intents.none(); intents.guilds = True`. In the
Developer Portal, **all three privileged intents stay OFF**.

**Bot permissions:** View Channel, Send Messages, Create Public Threads, Send Messages
in Threads, Read Message History, Attach Files, Embed Links, (optional) Manage Threads.
Enforce the single-channel restriction with a hardcoded `ALLOWED_CHANNEL_ID` guard
**and** Discord channel permission overwrites.

**Command sync:** guild-scoped (`tree.sync(guild=...)`) for instant updates.

**`/pick` UI** (`discord.ui.View`, ephemeral) — captures the part list:
1. The bot lists the thread's candidate images as a **numbered gallery** (thumbnails as
   embeds, ≤25; show 25 most recent if more).
2. Capture the mapping *part → image numbers, in order*. Recommended primary input: a
   Modal text spec, one line per part —
   ```
   a: 2
   b: 5, 6
   c: 7
   ```
   where the label precedes `:`, then candidate numbers in page order; a blank label
   (`: 2`) = whole exercise, and `a b: 3` = one photo covering a+b. A button/select
   "add a part" builder is the fallback for non-typers.
3. The bot **validates** (every number exists, no empty part), echoes a preview
   ("Teil (a): #2 · Teil (b): #5, #6 · …"), and persists on Confirm. Re-running `/pick`
   atomically replaces that exercise's selection.

**Persistence — SQLite** (`data/latexercise.sqlite3`, via `aiosqlite`; stdlib `sqlite3` +
`asyncio.to_thread` is the zero-dep fallback). Multi-writer safe (several people
`/pick`-ing at once). Tables:
- `sheets(guild_id, sheet, num_exercises, hub_message_id, created_at)`
- `threads(guild_id, sheet, exercise_index, thread_id, thread_name)` — **mapping stored
  at creation**, so `/build` never parses thread names (users can rename them).
- `selections(guild_id, sheet, exercise_index, part_index, part_label, img_position,
  message_id, attachment_id, url, filename, content_type)` — **one row per image**, keyed
  by its (part, page). `part_label` is `"a"` / `"a) b)"` / `""`. `/pick` replaces per
  exercise; `/build` reads ordered by `exercise_index`, `part_index`, `img_position`.

**LaTeX generation** (`latex.py`) — emit the existing skeleton, now with part labels:
```latex
\documentclass[a4paper,11pt]{scrartcl}

\usepackage{exercise}
\setExerciseSheet{06}
\exerciseMakeHeaders

\begin{document}
\section*{Exercise 1}

\paragraph{(a)}
\begin{figure}[!htb]
    \centering
    \includegraphics[width=0.95\linewidth]{ex06/aufgabe1_a_1.png}
\end{figure}

\paragraph{(b)}
\begin{figure}[!htb]
    \centering
    \includegraphics[width=0.95\linewidth]{ex06/aufgabe1_b_1.jpeg}
\end{figure}

\newpage
...
\end{document}
```
- Per part the bot emits `\paragraph{(<label>)}` (e.g. `\paragraph{(a)}`, or
  `\paragraph{(a) (b)}` for a combined photo) **only when the part has a label**; an
  unlabelled whole-exercise part is just the bare figure(s), like `ex1`. Multiple images
  in one part = multiple stacked `figure` blocks under that one `\paragraph`. `\newpage`
  after each Exercise (last one omitted).
- `\includegraphics` paths are **root-relative** (`ex06/...`) — same convention as
  `ex1/robin/...`. Images saved to `latex-project/ex06/aufgabe{N}_{label}_{k}.{ext}`
  (label sanitized to ASCII: `aufgabe1_a_1.png`, `aufgabe1_ab_1.jpeg`, or
  `aufgabe1_1.png` when unlabelled) — renamed so phone names like `IMG_2026 (1).JPG`
  don't break `\includegraphics`.

**Compile** — CWD = project root (`exercise.sty` + image paths resolve from there).
Resolve compiler via `shutil.which`. `pdflatex` does **not** auto-use `\ExercisePdfName`,
so force the output name with `-jobname`:
```
latexmk -pdf -interaction=nonstopmode -halt-on-error \
        -jobname=Group_000_Sheet_06 ex06/ex06.tex        # preferred (auto multi-pass)
# fallback: pdflatex -jobname=Group_000_Sheet_06 ex06/ex06.tex   (run twice for hyperref/headers)
```
Run via `asyncio.create_subprocess_exec` (never `shell=True` — the path has a space),
~120 s timeout. On success post `Group_000_Sheet_06.pdf`; on failure parse the `.log`
(`^!` lines + tail) and post a trimmed excerpt.

> Note: generated folders are zero-padded `ex06/` while the legacy hand-made ones are
> `ex1/`…`ex5/`. They don't collide; the bot only ever wipes/writes its own `ex<NN>/`.

## Edge cases

- **Flexible sub-parts** — `/pick` covers every shape (whole-exercise / one-per-part /
  multi-page / combined `a+b`) and validates that referenced candidate numbers exist and
  no part is empty before saving.
- **No pick for an exercise** → `/build` aborts listing the gaps (override:
  `skip_missing=True`).
- **Expiring CDN URLs** — Discord attachment URLs are signed/expiring, so `/build`
  **re-fetches** each message by `message_id`/`attachment_id` and uses
  `Attachment.save()` rather than the stored URL.
- **Thread archived before build** → `await bot.fetch_channel(id)` reads it; unarchive
  via `thread.edit(archived=False)` if needed. Mitigated with
  `auto_archive_duration=10080` (7 d) at creation.
- **HEIC / webp** → pdflatex can't read them. Expected to be rare (Discord/iOS usually
  serve JPEG), so the bot just **detects by magic bytes and reports a clear per-image
  message** rather than carrying a conversion dependency up front. If HEIC actually shows
  up, add `pillow-heif` and convert to JPEG — small, isolated change in `images.py`.
- **Large photos / Discord 8 MB upload cap** → optional Pillow downscale
  (`DOWNSCALE_MAX_PX`, longest side ~2000px) before placing; catch 413 and inform.
- **Duplicate `/sheet`** for a sheet → refused (PK guard), points to existing threads.
- **LaTeX not installed** → `/build` preflight aborts early with install hints.
- **Wrong channel/thread** → ephemeral refusal via the channel/thread guards.

## Setup & config

`.env` (gitignored; documented in `.env.example`):
```
DISCORD_TOKEN=          # secret
GUILD_ID=               # int — guild-scoped command sync
ALLOWED_CHANNEL_ID=     # int — the one operating channel
LATEX_PROJECT_DIR=./latex-project
TEX_CMD=                # optional pdflatex/latexmk override (e.g. launchd minimal PATH)
DB_PATH=./data/latexercise.sqlite3
DOWNSCALE_MAX_PX=2000   # optional
ANNOUNCE_PICKS=true     # optional: public confirmation in thread on /pick
```

Developer Portal: create app → add bot → copy token → **leave all privileged intents
OFF** → OAuth2 URL with scopes `bot`+`applications.commands` and the permissions above
→ invite → copy `GUILD_ID` and channel ID (Developer Mode → Copy ID).

## Deploy (portable)

- **macOS:** `python3 -m venv .venv && pip install -r requirements.txt && python bot.py`.
  MacTeX adds `/Library/TeX/texbin` to PATH; under launchd, set `TEX_CMD` or the
  service PATH explicitly.
- **Linux:** `apt install texlive-latex-recommended texlive-latex-extra
  texlive-lang-german latexmk` (or `texlive-full`); run under a `systemd` unit with
  `EnvironmentFile=.env`, `Restart=on-failure`. Same code, same `.env`.

`requirements.txt`: `discord.py>=2.4,<3`, `python-dotenv>=1.0`, `aiosqlite>=0.20`,
`Pillow>=10` (optional but recommended). Python ≥ 3.10.

## Verification (end to end)

1. Invite the bot to a test guild/channel; `python bot.py` and confirm commands appear.
2. `/sheet 6 2` → two threads + hub message created; rows in `threads`.
3. Upload several photos in a thread (e.g. one for part a, two pages for part b);
   `/pick` and map `a: <n>` / `b: <n>, <m>`; confirm the preview and save.
4. `/build 6` → bot posts `Group_000_Sheet_06.pdf`; open it and check headers
   ("Sheet 06"), `(a)`/`(b)` labels above the right images, correct part/page order,
   page breaks between Exercisen.
5. Failure paths: `/build` with a missing pick (aborts), an HEIC upload (rejected),
   `/sheet 6 2` again (refused), a command in another channel (refused), a `/pick` spec
   referencing a non-existent image number (validation error).
6. Unit-test `latex.py` (part-list → `.tex`, including combined/unlabelled parts) and
   `store.py` (selection replace + ordered read) without Discord.
```
