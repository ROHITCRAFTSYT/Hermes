"""
Timers, reminders and alarms for Hermes.

Kept in timers.json (git-ignored) so they survive restarts. A background thread sleeps until the
next one is due and then calls the announce callback the bridge registers (chime + voice on the PC).
The model only passes what the user said ("8 minutes", "6 pm"); all time arithmetic happens here.
"""

import io
import json
import math
import re
import struct
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path(__file__).parent / "timers.json"
_LOCK = threading.Lock()
_WAKE = threading.Event()
_STATE = {"announce": None, "thread": None, "chime": None}
MISSED_GRACE_S = 12 * 3600      # announce things that came due while the bridge was off, up to this late
MAX_AHEAD_S = 366 * 86400


# ── storage ──────────────────────────────────────────────────────────────────

def _load() -> list:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list):
    _PATH.write_text(json.dumps(sorted(items, key=lambda i: i["due"]), ensure_ascii=False, indent=2),
                     encoding="utf-8")


# ── understanding what the user said ─────────────────────────────────────────

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
          "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "day": 86400, "days": 86400}


def _parse_duration(text: str) -> float:
    """'1 hour 30 minutes', '90 min', '1.5 hours' -> seconds (0 if none found)."""
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", (text or "").lower()):
        total += float(num) * _UNITS.get(unit, 0)
    return total


def _parse_at(at: str, now: datetime) -> datetime | None:
    """'18:00', '6 pm', '6:30pm', 'tomorrow 9:00', '2026-10-20 09:00' -> datetime (next occurrence)."""
    s = " ".join((at or "").lower().replace(".", "").split())
    day = None
    for word, offset in (("tomorrow", 1), ("today", 0), ("tonight", 0)):
        if s.startswith(word):
            day, s = (now + timedelta(days=offset)).date(), s[len(word):].strip(" ,")
            s = s[3:] if s.startswith("at ") else s
            break
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ t]?(.*)", s)
    if m:   # explicit date
        try:
            day = datetime.fromisoformat(m.group(1)).date()
        except ValueError:
            return None
        s = m.group(2).strip() or "09:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    dt = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=h, minute=mi)
    if day is None and dt <= now:
        dt += timedelta(days=1)   # "6 pm" when it's already 7 pm means tomorrow
    return dt


# ── speaking times ───────────────────────────────────────────────────────────

def _say_duration(seconds: float) -> str:
    s = max(1, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = ([f"{h} hour{'s' if h != 1 else ''}"] if h else []) + \
            ([f"{m} minute{'s' if m != 1 else ''}"] if m else []) + \
            ([f"{sec} second{'s' if sec != 1 else ''}"] if sec and not h else [])
    return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _say_when(ts: float, now: float) -> str:
    dt, n = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    t = dt.strftime("%I:%M %p").lstrip("0")
    days = (dt.date() - n.date()).days
    if days == 0:
        return f"{t} today"
    if days == 1:
        return f"{t} tomorrow"
    if days < 7:
        return f"{t} on {dt:%A}"
    return f"{t} on {dt:%B} {dt.day}"


def _describe(i: dict, now: float) -> str:
    label, kind = i.get("label") or "", i["kind"]
    every = ", every day" if i.get("repeat") == "daily" else ""
    if kind == "timer":
        name = f"{label} timer" if label else "Timer"
        return f"{name[0].upper()}{name[1:]}: {_say_duration(i['due'] - now)} left"
    if kind == "alarm":
        return f"Alarm at {_say_when(i['due'], now)}{every}" + (f" ({label})" if label else "")
    return f"Reminder to {label or 'check in'} at {_say_when(i['due'], now)}{every}"


def _message(i: dict, late: float) -> str:
    label, kind = i.get("label") or "", i["kind"]
    missed = late > 120
    if kind == "timer":
        name = f"{label} timer" if label else "timer"
        return f"While I was off, your {name} finished." if missed else f"Your {name} is done."
    if kind == "alarm":
        now_t = datetime.now().strftime("%I:%M %p").lstrip("0")
        return ("While I was off, your alarm went off. " if missed else f"It's {now_t}. ") + \
               (f"{label[0].upper()}{label[1:]}." if label else "Time to wake up!")
    return (f"While I was off, you had a reminder: {label}." if missed else f"Reminder: {label}.") \
        if label else "This is your reminder."


# ── the tool ─────────────────────────────────────────────────────────────────

def handle(action: str, kind: str = "", label: str = "", hours: float = 0, minutes: float = 0,
           seconds: float = 0, at: str = "", repeat: str = "", duration: str = "") -> str:
    a = (action or "set").lower().strip()
    a = {"add": "set", "create": "set", "start": "set", "new": "set", "show": "list", "status": "list",
         "check": "list", "remaining": "list", "get": "list", "delete": "cancel", "remove": "cancel",
         "stop": "cancel", "clear": "cancel"}.get(a, a)
    now = time.time()
    label = (label or "").strip().rstrip(".")
    kind = (kind or "").lower().strip()

    if a == "set":
        delay = float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0) + _parse_duration(duration)
        if not delay and at and not re.search(r"\d\s*(am|pm|:)|\d{4}-|tomorrow|today|tonight", at.lower()):
            delay = _parse_duration(at)   # the model put "10 minutes" in `at`
        if delay:
            due = now + delay
            if kind not in ("timer", "reminder", "alarm"):   # "remind me in 20 min to X" vs "a 20 min timer"
                kind = "reminder" if label and "timer" not in label.lower() else "timer"
        elif at:
            when = _parse_at(at, datetime.fromtimestamp(now))
            if not when:
                return f"I couldn't understand the time '{at}'. Try something like 6 pm or 18:30."
            due = when.timestamp()
            if due <= now:
                return "That time has already passed."
            kind = kind if kind in ("timer", "reminder", "alarm") else "reminder"
        else:
            return "For how long, or at what time?"
        if due - now > MAX_AHEAD_S:
            return "That's too far ahead for me to keep track of."
        if kind == "timer":
            label = re.sub(r"\s*timer$", "", label, flags=re.I)
            if re.fullmatch(r"[\d.\s]*(sec|second|min|minute|hr|hour)s?", label, flags=re.I):
                label = ""   # "5 second" just restates the duration: "Your timer is done."
        item = {"id": uuid.uuid4().hex[:6], "kind": kind, "label": label, "due": due,
                "repeat": "daily" if str(repeat).lower() in ("daily", "every day", "everyday", "true", "yes") else "",
                "created": now}
        with _LOCK:
            items = _load()
            items.append(item)
            _save(items)
        _WAKE.set()
        every = ", every day" if item["repeat"] else ""
        if kind == "timer":
            name = f"{label} timer" if label else "timer"
            return f"{name[0].upper()}{name[1:]} set for {_say_duration(delay)}."
        if kind == "alarm":
            return f"Alarm set for {_say_when(due, now)}{every}."
        return f"Okay, I'll remind you to {label or 'check in'} " + \
               (f"in {_say_duration(delay)}." if delay else f"at {_say_when(due, now)}{every}.")

    with _LOCK:
        items = _load()
    if a == "list":
        want = [i for i in items if not kind or i["kind"] == kind or kind in ("all", "")]
        if label:
            want = [i for i in want if label.lower() in (i.get("label") or "").lower()] or want
        if not want:
            return f"You don't have any {kind + 's' if kind in ('timer', 'reminder', 'alarm') else 'timers or reminders'} set."
        return "; ".join(_describe(i, now) for i in want) + "."

    if a == "cancel":
        def hit(i):
            if label and label.lower() not in ("all", "everything"):
                return label.lower() in (i.get("label") or "").lower()
            return not kind or kind == "all" or i["kind"] == kind
        gone = [i for i in items if hit(i)]
        if not gone:
            what = f"a {kind or 'timer or reminder'} called {label}" if label else f"any {kind or 'timer'}s"
            return f"You don't have {what}."
        with _LOCK:
            _save([i for i in _load() if i["id"] not in {g["id"] for g in gone}])
        _WAKE.set()
        if len(gone) == 1:
            g = gone[0]
            name = (f"the {g['label']} timer" if g["label"] else "the timer") if g["kind"] == "timer" else \
                   (f"the reminder to {g['label']}" if g["kind"] == "reminder" and g["label"] else f"the {g['kind']}")
            return f"Cancelled {name}."
        return f"Cancelled {len(gone)} {kind + 's' if kind in ('timer', 'reminder', 'alarm') else 'timers and reminders'}."

    return "I can set, list, or cancel timers, reminders and alarms."


# ── the scheduler ────────────────────────────────────────────────────────────

def _run():
    while True:
        _WAKE.clear()   # before reading, so an add() during this pass wakes the next wait
        now = time.time()
        with _LOCK:
            items = _load()
            due = [i for i in items if i["due"] <= now]
            if due:
                keep = [i for i in items if i["due"] > now]
                for i in due:
                    if i.get("repeat") == "daily":
                        nxt = i["due"]
                        while nxt <= now:
                            nxt += 86400
                        keep.append({**i, "due": nxt})
                _save(keep)
                items = keep
        for i in due:
            late = now - i["due"]
            if late > MISSED_GRACE_S:
                print(f"   [timer] dropped one missed by {late / 3600:.0f} h: {i.get('label') or i['kind']}")
                continue
            announce = _STATE["announce"]
            if announce:
                threading.Thread(target=announce, args=(_message(i, late), i["kind"]), daemon=True).start()
        nxt = min((i["due"] for i in items), default=None)
        _WAKE.wait(timeout=min(60.0, max(0.2, nxt - time.time())) if nxt else 60.0)


def start(announce):
    """Begin firing timers (once per process). announce(text, kind) speaks it."""
    _STATE["announce"] = announce
    if not _STATE["thread"]:
        _STATE["thread"] = threading.Thread(target=_run, daemon=True, name="hermes-timers")
        _STATE["thread"].start()


def chime_path() -> str:
    """A soft three-note chime, generated once (no sound files to ship)."""
    if not _STATE["chime"]:
        rate, notes = 22050, (659.25, 783.99, 1046.5)
        frames = bytearray()
        for k, f in enumerate(notes):
            n = int(rate * (0.55 if k == len(notes) - 1 else 0.22))
            for t in range(n):
                env = math.exp(-4.0 * t / n) * min(1.0, t / 200)
                frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * t / rate)))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        path = Path(tempfile.gettempdir()) / "hermes_chime.wav"
        path.write_bytes(buf.getvalue())
        _STATE["chime"] = str(path)
    return _STATE["chime"]
