# claude-garmin-coach

A personal training coach in Telegram. After each workout syncs to Garmin Connect, it sends
you an analysis. You can chat with it about the workout and the plan, and it updates planned
workouts in your Garmin calendar when you agree.

Under the hood: one small Python process (standard library only, plus the Garmin library) that
runs Claude Code headless (`claude -p`) with the Garmin MCP server whenever there's something
to do.

## How it works

```
bot.py (systemd service, idle most of the time)
 ├─ Telegram long polling: one request waits up to 25 s; replies come instantly
 ├─ every 15 min: Garmin "any new activity?" (1 request)
 │    └─ new → claude -p "[auto-analysis] ..." in coach/ → analysis to Telegram
 └─ your message → claude -p (continues the conversation) → answer to Telegram
```

- **Conversations:** a new analysis starts a new conversation. Plain messages continue the
  latest one. *Replying* to an older bot message continues that message's conversation.
  `/new` starts fresh.
- **Coach context** (see [Personalizing](#personalizing)): [coach/CLAUDE.md](coach/CLAUDE.md)
  holds the generic role, style and analysis procedure. Your own additions go in
  `coach/CLAUDE.local.md`, and `coach/notes/` holds what the coach knows about you.
- **Which activities get analyzed:** everything except light activities (walks by default)
  shorter than 3 km. Longer light activities get a brief 2–4 line summary. Configure this with
  `LIGHT_ACTIVITY_TYPES`, `LIGHT_ACTIVITY_MIN_KM` and `SKIP_ACTIVITY_TYPES` in `config.env`.
- **Weather:** Garmin has none for your activities, so [weather.py](weather.py) gets hourly
  Open-Meteo data for the activity's GPS start point (rounded to ~1 km). Free, no API key.
  Activities without GPS use `HOME_LATITUDE`/`HOME_LONGITUDE` from `config.env`.
- **Permissions** ([permissions.json](permissions.json)): all Garmin tools, *except* deleting
  activities, weigh-ins, food logs, custom foods and courses. Planned workouts can be created,
  changed and deleted. File edits are allowed only in `coach/notes/`, and the only shell
  command it may run is the weather script. No other settings on the machine apply.
- **Sandbox:** the systemd service makes the whole filesystem read-only for the bot, Claude Code
  and the Garmin MCP server. They can write only to this folder, Claude Code's state
  (`~/.claude`, `~/.claude.json`) and the Garmin tokens (`~/.garminconnect`). `/tmp` is private.
- **Calendar changes** happen only in chat. Automatic analyses only *propose* changes.

## Cost on the server

- Idle: one Python process (~30 MB RAM), waking every 25 s for a Telegram request.
- Per workout or message: Claude Code runs for ~1 min, peaks at ~260 MB RAM, and uses ~8 s of
  CPU in total. The model itself runs in Anthropic's cloud. The service runs with `Nice=10` and
  low CPU/IO priority.
- Usage counts against your Claude subscription. No API key needed.

## Setup on the server (Ubuntu, headless)

1. Clone and install:
   ```bash
   git clone https://github.com/alivespirit/claude-garmin-coach.git ~/claude-garmin-coach
   cd ~/claude-garmin-coach && ./install.sh
   ```
   `install.sh` creates `.venv` with garmin-mcp, installs Claude Code (single binary, no
   Node.js) if missing, creates `config.env` and `coach/notes/` from templates, and writes
   `claude-garmin-coach.service`.
2. Private files are not in git. Copy them from a machine that has them (run on that machine):
   ```bash
   rsync -a ~/.garminconnect/ SERVER:~/.garminconnect/                  # Garmin tokens
   rsync -a ~/claude-garmin-coach/config.env SERVER:~/claude-garmin-coach/
   rsync -a ~/claude-garmin-coach/coach/notes/ SERVER:~/claude-garmin-coach/coach/notes/
   rsync -a ~/claude-garmin-coach/coach/CLAUDE.local.md SERVER:~/claude-garmin-coach/coach/
   ```
   With no tokens to copy, run `.venv/bin/garmin-mcp-auth` on the server instead.
3. On the server, remove any `CLAUDE_BIN=` line from `config.env` that points at a laptop path
   (the installed `~/.local/bin/claude` is found automatically).
4. Claude login: run `~/.local/bin/claude setup-token` on the server. Open the link it prints on
   any device, paste the code back, and put the resulting token in `config.env` as
   `CLAUDE_CODE_OAUTH_TOKEN=...`.
5. Fill in `TELEGRAM_BOT_TOKEN` (and `TELEGRAM_CHAT_ID`, see below) in `config.env`.
6. Test without Telegram:
   ```bash
   .venv/bin/python bot.py --ask "what's my next planned workout?"
   .venv/bin/python bot.py --analyze <activity_id>
   ```
7. Start it:
   ```bash
   sudo cp claude-garmin-coach.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now claude-garmin-coach
   journalctl -u claude-garmin-coach -f
   ```

**Chat id:** leave `TELEGRAM_CHAT_ID` empty, start the bot, and send it any message. It replies
with your id. Put the id in `config.env` and restart. From then on, the bot ignores every other
chat.

Run the bot on **one machine only**. Telegram gives updates to a single poller, and two
copies would also analyze every workout twice.

## Telegram commands

- `/analyze`: analyze the latest workout now (`/analyze <id>` for a specific one)
- `/new`: start a new conversation
- `/status`: last Garmin check, settings, current conversation

## Personalizing

Everything personal is gitignored, so `git pull` never touches it:

- **`coach/notes/`**: what the coach knows about you. `install.sh` creates it from
  [coach/notes-template/](coach/notes-template/), which shows what's worth writing down:
  - `athlete.md`: profile, fitness, zones, goals, limitations, schedule, feedback preferences;
  - `plan.md`: the plan and *why* it looks this way, agreed adjustment rules, open questions;
  - `log.md`: analyzed workouts.

  Fill the files in yourself, or have Claude write them from an earlier conversation about your
  training. The coach keeps them up to date afterwards. `log.md` is loaded on every run, so the
  coach condenses it once it grows past ~150 lines.
- **`coach/CLAUDE.local.md`** (optional): your own instructions, loaded on top of the generic
  [coach/CLAUDE.md](coach/CLAUDE.md) and taking precedence over it. Use it for the language and
  tone you want, data quirks (sensor changes, GPS problems where you run), or Garmin settings
  the coach should respect. For example:
  ```markdown
  # Personal instructions
  - Always write in German, informally.
  - HR: chest strap since 2026-03-01; earlier data is from the wrist.
  ```

No restart needed: every Claude run reads these files fresh.

## Maintenance

- Update Claude Code: `~/.local/bin/claude update` (auto-update is disabled in `config.env`).
- Update garmin-mcp: `.venv/bin/pip install --upgrade git+https://github.com/Taxuspt/garmin_mcp`.
- Garmin tokens last ~6 months. When they expire, run `.venv/bin/garmin-mcp-auth` again.
