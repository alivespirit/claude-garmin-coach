#!/usr/bin/env python3
"""Garmin coach: a Telegram bot that runs Claude Code on new Garmin workouts and on your messages.

One process, one loop:
  * Telegram long polling: one request waits up to TELEGRAM_POLL_SECONDS, idle in between.
  * Every GARMIN_POLL_MINUTES: check Garmin for new activities and analyze them.
  * Claude runs one at a time, so messages simply queue while it is busy.

Test without Telegram:
  bot.py --ask "яке в мене наступне тренування?"
  bot.py --analyze <activity_id>
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COACH_DIR = ROOT / "coach"
STATE_FILE = ROOT / "state.json"
MCP_CONFIG = ROOT / ".mcp-runtime.json"


def load_config_file():
    """Read config.env (KEY=VALUE lines) into the environment; real env vars win."""
    path = ROOT / "config.env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


load_config_file()


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


TOKEN = env("TELEGRAM_BOT_TOKEN")
CHAT_ID = env("TELEGRAM_CHAT_ID")
CLAUDE_BIN = env("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_MODEL = env("CLAUDE_MODEL")
CLAUDE_TIMEOUT = int(env("CLAUDE_TIMEOUT_MINUTES", "10")) * 60
TELEGRAM_POLL = int(env("TELEGRAM_POLL_SECONDS", "25"))
GARMIN_POLL = int(env("GARMIN_POLL_MINUTES", "15")) * 60
SKIP_TYPES = {t.strip() for t in env("SKIP_ACTIVITY_TYPES", "walking").split(",") if t.strip()}

HELP = (
    "Привіт! Я твій тренер 🏃\n\n"
    "Після кожного тренування, що синхронізувалось у Garmin, я надішлю розбір.\n"
    "Пиши мені будь-що про тренування й план — відповім і, якщо домовимось, оновлю календар.\n\n"
    "Відповідь (reply) на мій розбір продовжує розмову саме про те тренування.\n\n"
    "/new — почати нову розмову\n"
    "/analyze — розібрати останнє тренування (або /analyze <id>)\n"
    "/status — стан бота"
)
ANALYSIS_PROMPT = (
    "[auto-analysis] New activity synced from Garmin: id {id}, \"{name}\" ({type}), "
    "started {start}. Analyze it as described in CLAUDE.md."
)

log = logging.getLogger("coach")


# ---------- state ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {"offset": 0, "seen": [], "session": None, "msg_sessions": {}, "last_garmin_check": None}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE_FILE)


def remember(state, message_ids, session_id):
    """Map bot messages to Claude sessions, so a reply continues the right conversation."""
    if not session_id:
        return
    sessions = state["msg_sessions"]
    for mid in message_ids:
        sessions[str(mid)] = session_id
    for key in list(sessions)[:-500]:
        del sessions[key]


# ---------- Telegram ----------

def tg(method, **params):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=params.get("timeout", 0) + 20) as response:
            body = json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {method}: HTTP {e.code} {e.read()[:300]!r}") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body}")
    return body["result"]


def split_message(text, limit=4000):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return [p for p in parts if p.strip()] or ["(порожня відповідь)"]


def send(text, reply_to=None, chat_id=None):
    ids = []
    for i, chunk in enumerate(split_message(text)):
        params = {"chat_id": chat_id or CHAT_ID, "text": chunk, "link_preview_options": {"is_disabled": True}}
        if reply_to and i == 0:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        ids.append(tg("sendMessage", **params)["message_id"])
    return ids


def typing():
    try:
        tg("sendChatAction", chat_id=CHAT_ID, action="typing")
    except Exception:
        pass


# ---------- Claude ----------

def write_mcp_config():
    """Point Claude at the garmin-mcp server installed in this venv."""
    server = {"command": str(Path(sys.executable).parent / "garmin-mcp")}
    if env("GARMINTOKENS"):
        server["env"] = {"GARMINTOKENS": env("GARMINTOKENS")}
    MCP_CONFIG.write_text(json.dumps({"mcpServers": {"garmin": server}}, indent=1))


def run_claude(prompt, session_id=None, show_typing=False):
    """Run one headless Claude Code turn in coach/. Returns (reply_text, session_id)."""
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
        # Permissions come only from permissions.json: no user/project settings, so nothing
        # else on the machine can widen what the unattended agent may do.
        "--setting-sources", "", "--settings", str(ROOT / "permissions.json"),
    ]
    if session_id:
        cmd += ["--resume", session_id]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]

    log.info("claude: %s%s", prompt[:80].replace("\n", " "), f" (resume {session_id})" if session_id else "")
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=COACH_DIR, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    while True:
        try:
            out, err = proc.communicate(timeout=4)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - started > CLAUDE_TIMEOUT:
                proc.kill()
                proc.communicate()
                return f"⚠️ Claude не встиг за {CLAUDE_TIMEOUT // 60} хв, спробуй ще раз.", session_id
            if show_typing:
                typing()

    log.info("claude finished in %.0fs (exit %s)", time.monotonic() - started, proc.returncode)
    try:
        data = json.loads(out)
    except ValueError:
        detail = (err or out or "").strip()[-600:]
        return f"⚠️ Claude завершився з помилкою (код {proc.returncode}):\n{detail}", session_id
    text = (data.get("result") or "").strip()
    if data.get("is_error"):
        text = "⚠️ " + (text or data.get("subtype") or "помилка Claude")
    return text or "(порожня відповідь)", data.get("session_id") or session_id


# ---------- Garmin ----------

def garmin():
    from garminconnect import Garmin

    client = Garmin()
    client.login(os.path.expanduser(env("GARMINTOKENS", "~/.garminconnect")))
    return client


def activity_type(activity):
    return (activity.get("activityType") or {}).get("typeKey", "")


def analyze(state, activity):
    prompt = ANALYSIS_PROMPT.format(
        id=activity["activityId"], name=activity.get("activityName", ""),
        type=activity_type(activity), start=activity.get("startTimeLocal", ""),
    )
    text, session_id = run_claude(prompt, show_typing=True)
    state["session"] = session_id  # a plain message after the analysis continues this conversation
    remember(state, send(text), session_id)
    save_state(state)


def check_garmin(state):
    activities = garmin().get_activities(0, 10)
    state["last_garmin_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    seen = set(state["seen"])
    new = [a for a in reversed(activities) if a["activityId"] not in seen]  # oldest first
    first_run = not state["seen"]
    state["seen"] = (state["seen"] + [a["activityId"] for a in new])[-300:]
    save_state(state)
    if first_run:
        log.info("first Garmin check: marked %d existing activities as seen", len(new))
        return
    for activity in new:
        if activity_type(activity) in SKIP_TYPES:
            log.info("skipping %s %s", activity_type(activity), activity["activityId"])
            continue
        log.info("new activity %s, analyzing", activity["activityId"])
        analyze(state, activity)


# ---------- message handling ----------

def handle_message(state, msg):
    chat = msg["chat"]["id"]
    if not CHAT_ID:
        send(f"Твій chat id: {chat}\nДодай TELEGRAM_CHAT_ID={chat} у config.env і перезапусти бота.", chat_id=chat)
        return
    if str(chat) != CHAT_ID:
        log.warning("ignoring message from unknown chat %s", chat)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        send("Поки що розумію тільки текст 🙂", reply_to=msg["message_id"])
        return
    words = text.split()
    command = words[0].split("@")[0].lower() if text.startswith("/") else ""

    if command in ("/start", "/help"):
        send(HELP)
    elif command == "/new":
        state["session"] = None
        save_state(state)
        send("Ок, починаємо нову розмову.")
    elif command == "/status":
        send(
            f"Остання перевірка Garmin: {state.get('last_garmin_check') or 'ще не було'}\n"
            f"Перевірка кожні {GARMIN_POLL // 60} хв, пропускаю: {', '.join(sorted(SKIP_TYPES)) or '—'}\n"
            f"Поточна розмова: {state.get('session') or 'нова'}"
        )
    elif command == "/analyze":
        if len(words) > 1 and words[1].isdigit():
            activity = garmin().get_activity(int(words[1]))
            summary = activity.get("summaryDTO") or {}
            activity = {
                "activityId": activity["activityId"], "activityName": activity.get("activityName"),
                "activityType": activity.get("activityTypeDTO"), "startTimeLocal": summary.get("startTimeLocal"),
            }
        else:
            recent = [a for a in garmin().get_activities(0, 10) if activity_type(a) not in SKIP_TYPES]
            if not recent:
                send("Не знайшов недавніх тренувань.")
                return
            activity = recent[0]
        analyze(state, activity)
    else:
        reply = msg.get("reply_to_message")
        session_id = state["msg_sessions"].get(str(reply["message_id"])) if reply else None
        answer, session_id = run_claude(text, session_id or state.get("session"), show_typing=True)
        state["session"] = session_id
        remember(state, send(answer, reply_to=msg["message_id"]), session_id)
        save_state(state)


# ---------- main ----------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    write_mcp_config()

    if len(sys.argv) == 3 and sys.argv[1] in ("--ask", "--analyze"):
        if sys.argv[1] == "--ask":
            print(run_claude(sys.argv[2])[0])
        else:
            print(run_claude(ANALYSIS_PROMPT.format(id=sys.argv[2], name="?", type="?", start="?"))[0])
        return
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (config.env)")
    if not CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set: send any message to the bot to get your chat id")

    state = load_state()
    next_garmin_check = 0.0
    log.info("started: Telegram poll %ss, Garmin poll %s min", TELEGRAM_POLL, GARMIN_POLL // 60)
    while True:
        loop_start = time.monotonic()
        try:
            updates = tg("getUpdates", offset=state["offset"], timeout=TELEGRAM_POLL, allowed_updates=["message"])
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            updates = []
        for update in updates:
            state["offset"] = update["update_id"] + 1
            save_state(state)  # saved before handling: a crashing message is not retried forever
            if "message" not in update:
                continue
            try:
                handle_message(state, update["message"])
            except Exception as e:
                log.exception("failed to handle message")
                try:
                    send(f"⚠️ Помилка: {e}")
                except Exception:
                    pass

        if CHAT_ID and time.monotonic() >= next_garmin_check:
            next_garmin_check = time.monotonic() + GARMIN_POLL
            try:
                check_garmin(state)
            except Exception as e:
                log.warning("Garmin check failed: %s", e)

        # Long polling normally blocks for TELEGRAM_POLL seconds. If it returned quickly with
        # nothing (network down, errors), wait instead of spinning.
        if not updates and time.monotonic() - loop_start < 5:
            time.sleep(TELEGRAM_POLL)


if __name__ == "__main__":
    main()
