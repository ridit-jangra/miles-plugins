import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from miles.watchers.inbox import Alert

CLOCK = re.compile(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\s*")
WORDS = {"noon": (12, 0), "midnight": (0, 0)}


def data_file() -> Path:
    plugins = Path(os.environ.get("MILES_PLUGINS_DIR") or Path.home() / ".miles" / "plugins")
    return plugins.parent / "data" / "alarms.json"


def parse_clock(text: str) -> tuple[int, int] | None:
    text = text.strip().lower()
    if text in WORDS:
        return WORDS[text]
    m = CLOCK.fullmatch(text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def spoken_gap(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def spoken_time(due: float) -> str:
    when = datetime.fromtimestamp(due)
    days = (when.date() - datetime.now().date()).days
    clock = when.strftime("%I:%M %p").lstrip("0")
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} on {when.strftime('%A %d %B')}"


def register(api):
    path = data_file()
    lock = threading.Lock()

    def load() -> list[dict]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return []

    def save(items: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, indent=2))

    def add(kind: str, label: str, due: float, daily: bool = False) -> dict:
        with lock:
            items = load()
            item = {"id": max((i["id"] for i in items), default=0) + 1, "kind": kind, "label": label, "due": due, "daily": daily}
            items.append(item)
            save(items)
        return item

    def view(item: dict) -> dict:
        return {
            "id": item["id"],
            "kind": item["kind"],
            "label": item["label"],
            "rings": spoken_time(item["due"]),
            "in": spoken_gap(item["due"] - time.time()),
            "daily": item["daily"],
        }

    @api.tool
    def set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> dict:
        """Start a countdown timer. Use it for "set a timer for ten minutes".

        Args:
            minutes: Minutes to count down.
            seconds: Extra seconds to count down.
            label: What the timer is for, like "tea".
        """
        total = minutes * 60 + seconds
        if total <= 0:
            return {"error": "The timer needs a length above zero."}
        return view(add("timer", label.strip(), time.time() + total))

    @api.tool
    def set_alarm(at: str, label: str = "", date: str = "", daily: bool = False) -> dict:
        """Set an alarm or reminder for a clock time. It rings at the next time that clock reads, today or tomorrow.

        Args:
            at: The time of day, like "7:30", "19:30", "7 pm" or "noon".
            label: What it is for, like "stand-up" or "call mum". Put reminder text here.
            date: Optional day as YYYY-MM-DD, only when the user names a specific date.
            daily: True to repeat every day.
        """
        parsed = parse_clock(at)
        if not parsed:
            return {"error": f"Couldn't read {at!r} as a time. Try 7:30, 19:30 or 7 pm."}
        now = datetime.now()
        if date.strip():
            try:
                day = datetime.strptime(date.strip(), "%Y-%m-%d")
            except ValueError:
                return {"error": f"Couldn't read {date!r} as a date. Use YYYY-MM-DD."}
            ring = day.replace(hour=parsed[0], minute=parsed[1])
        else:
            ring = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
            if ring <= now:
                ring += timedelta(days=1)
        if ring <= now:
            return {"error": "That time has already passed."}
        return view(add("alarm", label.strip(), ring.timestamp(), daily))

    @api.tool
    def list_alarms() -> dict:
        """List every timer, alarm and reminder that is waiting to ring, soonest first."""
        with lock:
            items = sorted(load(), key=lambda i: i["due"])
        return {"alarms": [view(i) for i in items]}

    @api.tool
    def cancel_alarm(id: int = 0, label: str = "") -> dict:
        """Cancel a timer, alarm or reminder by its id, or by its label when the user didn't say a number.

        Args:
            id: The id from set_timer, set_alarm or list_alarms.
            label: Part of the label, used when no id is given.
        """
        with lock:
            items = load()
            keep = [
                i for i in items
                if not (i["id"] == id if id else bool(label) and label.lower() in i["label"].lower())
            ]
            removed = len(items) - len(keep)
            if removed:
                save(keep)
        return {"cancelled": removed} if removed else {"error": "Nothing matched."}

    @api.watch(every="2s")
    def ring() -> list[Alert]:
        alerts = []
        now = time.time()
        with lock:
            items = load()
            if not any(i["due"] <= now for i in items):
                return []
            keep = []
            for item in items:
                if item["due"] > now:
                    keep.append(item)
                    continue
                late = now - item["due"]
                name = item["label"] or item["kind"]
                if late > 120:
                    text = f"Missed {item['kind']} {name!r}, it was due at {spoken_time(item['due'])}"
                elif item["kind"] == "timer":
                    text = f"Timer done: {name}" if item["label"] else "Timer done"
                else:
                    text = f"Alarm: {name}" if item["label"] else "Alarm ringing"
                alerts.append(Alert(text=text, source="alarms", priority="urgent", key=f"alarm-{item['id']}-{int(item['due'])}"))
                if item["daily"]:
                    step = timedelta(days=1).total_seconds()
                    item["due"] += step * (int(late // step) + 1)
                    keep.append(item)
            save(keep)
        return alerts
