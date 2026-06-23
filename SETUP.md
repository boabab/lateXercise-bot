# Setting up the lateXercise bot on your own server

This guide is for friends who want to run their **own** copy of the bot for their **own**
study group — different group number, course, tutorial, and names — without touching any
code. You can reuse the included LaTeX style (`latex-project/exercise.sty`) as-is and just
override the group/course/tutorial/authors (see [step 6](#6-customize-it-for-your-group)).

> **You need a machine that stays on** to host the bot (your laptop while testing, or a
> small Linux VPS / Raspberry Pi for 24/7). `/build` runs LaTeX locally, so LaTeX must be
> installed on that same machine.

---

## 1. Get the code

```bash
git clone <this-repo-url> lateXercise-bot
cd lateXercise-bot
```

The repo ships an example LaTeX project in `latex-project/` (the `exercise.sty` style + a
few sample sheets `ex1`…`ex5`). You can keep it and just override the header values in step 6,
or delete the sample `ex1`…`ex5` folders — the bot only ever writes its own `ex06/`, `ex07/`, …

---

## 2. Install prerequisites

**Python ≥ 3.10**, then the bot's dependencies in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

**LaTeX** (needed only for `/build`). The bot looks for `latexmk` (preferred) or `pdflatex`
on `PATH`.

- **Debian / Ubuntu / Mint:**
  ```bash
  sudo apt update
  sudo apt install texlive-latex-recommended texlive-latex-extra \
                   texlive-lang-german latexmk
  ```
  That covers everything `exercise.sty` uses (KOMA-Script, hyperref, TikZ, German babel,
  mathtools, …). If you'd rather not think about packages: `sudo apt install texlive-full`
  (~5 GB, has everything).
- **Fedora:** `sudo dnf install texlive-scheme-medium latexmk`
- **Arch:** `sudo pacman -S texlive-basic texlive-latexrecommended texlive-latexextra texlive-langgerman texlive-binextra`
- **macOS:** `brew install texlive` (or install MacTeX from https://tug.org/mactex/).

Verify:
```bash
latexmk --version    # should print a version
```

---

## 3. Create your Discord bot

1. Go to **https://discord.com/developers/applications** → **New Application**, give it a name.
2. **Bot** (left sidebar):
   - **Reset Token** → **Copy** it. This is your `DISCORD_TOKEN` — keep it secret, never commit it.
   - Under **Privileged Gateway Intents**, turn **MESSAGE CONTENT INTENT** **ON**.
     Leave *Server Members* and *Presence* **off**.
     > ⚠️ **Required.** `/pick` reads the photos other members upload; without this intent
     > Discord hides their attachments and `/pick` finds nothing.
3. **OAuth2 → URL Generator**:
   - **Scopes:** `bot` **and** `applications.commands`.
   - **Bot Permissions:** *View Channels, Send Messages, Create Public Threads,
     Send Messages in Threads, Read Message History, Attach Files, Embed Links*.
   - Copy the generated URL, open it, pick your server, **Authorize**.

   (Shortcut — replace the client id with your Application ID:
   `https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=309237763072&scope=bot%20applications.commands`)

---

## 4. Get the IDs

In Discord: **User Settings → Advanced → Developer Mode → ON**, then:
- Right-click your **server** icon → **Copy Server ID** → this is `GUILD_ID`.
- Make (or pick) one or more channels for the bot, right-click each → **Copy Channel ID**.
  These go into `ALLOWED_CHANNEL_IDS` as a comma-separated list (one channel per submission
  group). The bot only operates in these channels and their threads. A single channel is fine;
  see [Running several groups](#running-several-groups) if you want more than one.

---

## 5. Configure and run

```bash
cp .env.example .env
```
Edit `.env` and fill in at least the three required values:
```
DISCORD_TOKEN=your-token
GUILD_ID=your-server-id
ALLOWED_CHANNEL_IDS=your-channel-id          # comma-separated for several groups, e.g. 111,222,333
```
(The old singular `ALLOWED_CHANNEL_ID` still works as an alias if you already had it set.)
Then start it:
```bash
.venv/bin/python bot.py
```
You should see `Synced N application command(s) to guild …` and `Connected as …`. The slash
commands appear in your server within a few seconds. Run `/help` in the channel to see the
workflow. Stop the bot with Ctrl-C.

### Running several groups
Each allowed channel is an independent submission group with its own `/sheet` sheets, `/pick`
selections, and `/config` config. To run more than one group, put each group's channel id in
`ALLOWED_CHANNEL_IDS` (e.g. `ALLOWED_CHANNEL_IDS=111,222,333`) and run `/config` *in each
channel* to set that group's number, course, tutorial, and authors. Builds are serialized and
PDFs are named per group (`Group_<group>_Sheet_NN.pdf`), so two channels building the same
sheet number won't collide. If you're upgrading from a single-channel setup, the bot
auto-migrates your existing database to the multi-channel schema on first startup — no action
needed.

---

## 6. Customize it for your group

You do **not** edit any code or `exercise.sty`. There are two ways to set the group number,
course, tutorial label, and author list (precedence: `/config` > `.env` > `exercise.sty`):

### Live, from Discord — `/config`
Run it **in the channel** you want to configure (each channel keeps its own values, so groups
in different channels can have different numbers and names):
```
/config group:142 course:"My Course 2026" tutorial:"Tutorial 12" authors:"Anna Sample, 111111; Ben Example, 222222"
```
- `group` ends up in the header **and** the filename → `Group_142_Sheet_06.pdf`.
- `authors` is one string with entries separated by `;` — each becomes its own line.
- Run `/config` with no options to see the current values; `/config reset:true` clears them.
- Each option is independent — set only what you want to change.

### Or as defaults in `.env`
```
GROUP_NUMBER=142
COURSE=My Course 2026
TUTORIUM=Tutorial 12
AUTHORS=Anna Sample, 111111; Ben Example, 222222
```
`/config` values (stored per channel in the database) override these at build time. The `.env`
defaults are global — they apply to every channel that hasn't set its own `/config` value.

If you set none of them, the values baked into `latex-project/exercise.sty` are used. (The
bot already works around a quirk in that file, so `/build` compiles cleanly out of the box.)

---

## 7. Keep it running 24/7 (Linux, systemd)

Create `/etc/systemd/system/latexercise-bot.service` (adjust the paths and `User`):

```ini
[Unit]
Description=lateXercise bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/lateXercise-bot
# Ensure latexmk is reachable; add your TeX bin dir if it isn't on the default PATH.
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/home/youruser/lateXercise-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now latexercise-bot
journalctl -u latexercise-bot -f      # watch the logs
```
If the logs show "No LaTeX compiler found", the service's `PATH` doesn't include your TeX
binaries — either fix `Environment=PATH=…` above, or set `TEX_CMD=/usr/bin/latexmk` in `.env`
(find the path with `which latexmk`).

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Slash commands don't appear | Check `GUILD_ID`; re-invite with the `applications.commands` scope; wait a few seconds. |
| `/pick` finds no images | Message Content intent isn't enabled in the Developer Portal. |
| A command is refused | You're not in one of the `ALLOWED_CHANNEL_IDS` channels (or `/pick` outside a bot thread). |
| `/build` says "No LaTeX compiler found" | LaTeX isn't installed or isn't on the service `PATH`; install it (step 2) or set `TEX_CMD`. |
| HEIC/WEBP photo rejected | Re-upload as PNG or JPEG (iPhones can shoot "Most Compatible" JPEG). |
| Reset everything | Stop the bot and delete `data/latexercise.sqlite3*` to wipe all sheets, picks, and `/config` config. |

For the day-to-day workflow (`/sheet` → upload → `/pick` → `/build`), see [README.md](README.md)
or run `/help` in Discord.
