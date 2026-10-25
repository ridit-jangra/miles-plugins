import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import holidays

from miles.watchers.inbox import Alert

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CLOCK = re.compile(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\s*")


def data_file() -> Path:
    home = Path(os.environ.get("MILES_HOME") or Path.home() / ".miles")
    return home / "data" / "calendar.json"


def parse_day(text: str) -> date | None:
    text = text.strip().lower()
    today = date.today()
    if text in ("", "today"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text in WEEKDAYS:
        ahead = (WEEKDAYS.index(text) - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_clock(text: str) -> str | None:
    m = CLOCK.fullmatch(text.strip().lower())
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def spoken_day(day: date) -> str:
    gap = (day - date.today()).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    if 0 < gap < 7:
        return day.strftime("%A")
    return day.strftime("%A %d %B")


def register(api):
    country = str(api.config.get("country", "")).upper()
    subdiv = str(api.config.get("subdivision", "")) or None
    remind_minutes = int(api.config.get("remind_minutes", 15))
    all_day_hour = int(api.config.get("all_day_hour", 9))
    path = data_file()
    lock = threading.Lock()

    def load() -> dict:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        return {"events": data.get("events", []), "notified": data.get("notified", [])}

    def save(data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def holiday_calendar(code: str, years: list[int]):
        return holidays.country_holidays(code, subdiv=subdiv if code == country else None, years=years)

    def view(event: dict) -> dict:
        day = date.fromisoformat(event["date"])
        return {"id": event["id"], "title": event["title"], "when": spoken_day(day), "date": event["date"], "time": event["time"] or "all day"}

    @api.tool
    def calendar_today() -> dict:
        """Get today's date, the weekday and whether today is a public holiday. Use it whenever you need to know the date."""
        today = date.today()
        result = {"date": today.isoformat(), "weekday": today.strftime("%A"), "spoken": today.strftime("%A %d %B %Y")}
        if country:
            name = holiday_calendar(country, [today.year]).get(today)
            result["holiday"] = name or None
        return result

    @api.tool
    def upcoming_holidays(days: int = 60, country_code: str = "") -> dict:
        """List public holidays coming up, soonest first.

        Args:
            days: How many days ahead to look.
            country_code: Two letter country code like IN or US. Leave empty for the user's own country.
        """
        code = (country_code or country).upper()
        if not code:
            return {"error": "No country set. Ask the user, or set country under [plugin_settings.calendar] in config.toml."}
        today = date.today()
        try:
            found = holiday_calendar(code, [today.year, today.year + 1])
        except NotImplementedError:
            return {"error": f"I don't have holidays for {code!r}. Use a two letter country code like IN or US."}
        end = today + timedelta(days=max(1, min(days, 400)))
        coming = sorted((d, n) for d, n in found.items() if today <= d <= end)
        return {"country": code, "holidays": [{"name": n, "date": d.isoformat(), "when": spoken_day(d), "in_days": (d - today).days} for d, n in coming]}

    @api.tool
    def add_event(title: str, day: str, time: str = "") -> dict:
        """Put an event on the user's calendar. They get a reminder shortly before it starts.

        Args:
            title: What the event is, like "dentist".
            day: The day as "today", "tomorrow", a weekday name like "friday", or YYYY-MM-DD.
            time: Optional start time like "15:30" or "3 pm". Leave empty for an all-day event.
        """
        when = parse_day(day)
        if when is None:
            return {"error": f"Couldn't read {day!r} as a day. Use today, tomorrow, a weekday or YYYY-MM-DD."}
        clock = ""
        if time.strip():
            clock = parse_clock(time)
            if clock is None:
                return {"error": f"Couldn't read {time!r} as a time."}
        with lock:
            data = load()
            event = {"id": max((e["id"] for e in data["events"]), default=0) + 1, "title": title.strip(), "date": when.isoformat(), "time": clock}
            data["events"].append(event)
            save(data)
        return view(event)

    @api.tool
    def list_events(days: int = 14) -> dict:
        """List the user's own upcoming events, soonest first.

        Args:
            days: How many days ahead to look.
        """
        today = date.today()
        end = today + timedelta(days=max(1, min(days, 365)))
        with lock:
            events = [e for e in load()["events"] if today <= date.fromisoformat(e["date"]) <= end]
        events.sort(key=lambda e: (e["date"], e["time"]))
        return {"events": [view(e) for e in events]}

    @api.tool
    def remove_event(id: int = 0, title: str = "") -> dict:
        """Remove an event from the calendar by its id, or by part of its title.

        Args:
            id: The id from add_event or list_events.
            title: Part of the title, used when no id is given.
        """
        with lock:
            data = load()
            keep = [e for e in data["events"] if not (e["id"] == id if id else bool(title) and title.lower() in e["title"].lower())]
            removed = len(data["events"]) - len(keep)
            if removed:
                data["events"] = keep
                save(data)
        return {"removed": removed} if removed else {"error": "Nothing matched."}

    @api.watch(every="1m", first_run="now")
    def remind() -> list[Alert]:
        alerts = []
        now = datetime.now()
        with lock:
            data = load()
            for event in data["events"]:
                day = date.fromisoformat(event["date"])
                if event["time"]:
                    start = datetime.combine(day, datetime.strptime(event["time"], "%H:%M").time())
                    ready = start - timedelta(minutes=remind_minutes) <= now <= start
                    text = f"{event['title']} starts at {start.strftime('%I:%M %p').lstrip('0')}"
                else:
                    ready = day == now.date() and now.hour >= all_day_hour
                    text = f"{event['title']} is today"
                token = f"{event['id']}-{event['date']}-{event['time']}"
                if ready and token not in data["notified"]:
                    data["notified"].append(token)
                    alerts.append(Alert(text, "calendar", "normal", f"event-{token}"))
            if alerts:
                save(data)
        return alerts
