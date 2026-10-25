import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from miles.context import SECRET_PATTERN


def data_file() -> Path:
    home = Path(os.environ.get("MILES_HOME") or Path.home() / ".miles")
    return home / "data" / "journal.json"


def spoken_day(day: date) -> str:
    gap = (date.today() - day).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "yesterday"
    if gap < 7:
        return day.strftime("%A")
    return day.strftime("%d %B")


def streak(days: set[date]) -> int:
    today = date.today()
    cursor = today if today in days else today - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def register(api):
    path = data_file()
    lock = threading.Lock()

    def load() -> list[dict]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return []

    def save(entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2))

    def add(entry: dict) -> dict:
        with lock:
            entries = load()
            entry["id"] = max((e["id"] for e in entries), default=0) + 1
            entry["at"] = datetime.now().isoformat(timespec="minutes")
            entries.append(entry)
            save(entries)
        return entry

    def view(entry: dict) -> dict:
        when = datetime.fromisoformat(entry["at"])
        out = {"id": entry["id"], "when": f"{spoken_day(when.date())} at {when.strftime('%I:%M %p').lstrip('0')}", "text": entry["text"]}
        if entry.get("habit"):
            out["habit"] = entry["habit"]
            if entry.get("amount"):
                out["amount"] = f"{entry['amount']:g} {entry.get('unit', '')}".strip()
        return out

    @api.tool
    def log_entry(text: str) -> dict:
        """Write something in the user's journal, like how their day went or a thought they want to keep.

        Args:
            text: What to write, in the user's own words.
        """
        text = " ".join(text.split())
        if not text:
            return {"error": "There was nothing to write."}
        if SECRET_PATTERN.search(text):
            return {"error": "That looks like it contains a key or secret, so I didn't save it."}
        return {"saved": view(add({"text": text}))}

    @api.tool
    def log_habit(habit: str, amount: float = 0, unit: str = "", note: str = "") -> dict:
        """Log that the user did a habit or activity, like a run, studying, reading or a workout.

        Args:
            habit: Short name of the habit, like "run" or "reading".
            amount: How much, like 30. Leave at 0 if there is no amount.
            unit: The unit for the amount, like "minutes", "pages" or "km".
            note: Any extra detail.
        """
        habit = habit.strip().lower()
        if not habit:
            return {"error": "Which habit?"}
        text = note.strip() or f"{habit}" + (f" {amount:g} {unit}".rstrip() if amount else "")
        entry = {"text": text, "habit": habit, "amount": amount or None, "unit": unit.strip()}
        return {"saved": view(add(entry))}

    @api.tool
    def recent_entries(days: int = 7, limit: int = 10) -> dict:
        """Read back the latest journal entries and habit logs, newest first.

        Args:
            days: How many days back to look.
            limit: Max number of entries.
        """
        cutoff = datetime.now() - timedelta(days=max(1, days))
        with lock:
            entries = [e for e in load() if datetime.fromisoformat(e["at"]) >= cutoff]
        entries.sort(key=lambda e: e["at"], reverse=True)
        shown = entries[: max(1, min(limit, 30))]
        return {"entries": [view(e) for e in shown]} if shown else {"error": "Nothing in the journal for that time."}

    @api.tool
    def search_journal(query: str, limit: int = 8) -> dict:
        """Search the journal for entries that mention some words.

        Args:
            query: Words to look for.
            limit: Max number of entries.
        """
        words = [w for w in query.lower().split() if w]
        if not words:
            return {"error": "What should I look for?"}
        with lock:
            entries = load()
        hits = [e for e in entries if all(w in (e["text"] + " " + e.get("habit", "")).lower() for w in words)]
        hits.sort(key=lambda e: e["at"], reverse=True)
        return {"entries": [view(e) for e in hits[: max(1, min(limit, 30))]]} if hits else {"error": f"Nothing matches {query!r}."}

    @api.tool
    def habit_summary(habit: str = "", days: int = 7) -> dict:
        """Summarise habits over the last few days: how often, the total amount and the current streak.

        Args:
            habit: One habit name, or empty for all of them.
            days: How many days to cover.
        """
        wanted = habit.strip().lower()
        cutoff = datetime.now() - timedelta(days=max(1, days))
        with lock:
            entries = [e for e in load() if e.get("habit") and (not wanted or wanted in e["habit"])]
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e["habit"], []).append(e)
        summary = []
        for name, items in sorted(groups.items()):
            recent = [e for e in items if datetime.fromisoformat(e["at"]) >= cutoff]
            units = {e.get("unit", "") for e in recent if e.get("amount")}
            total = sum(e["amount"] for e in recent if e.get("amount"))
            row = {
                "habit": name,
                "times": len(recent),
                "days_active": len({datetime.fromisoformat(e["at"]).date() for e in recent}),
                "streak_days": streak({datetime.fromisoformat(e["at"]).date() for e in items}),
            }
            if total and len(units) == 1:
                row["total"] = f"{total:g} {next(iter(units))}".strip()
            summary.append(row)
        return {"days": days, "habits": summary} if summary else {"error": "No habits logged for that."}

    @api.tool
    def delete_entry(id: int) -> dict:
        """Delete one journal entry by its id, after the user says yes.

        Args:
            id: The id of the entry, from recent_entries or search_journal.
        """
        with lock:
            entry = next((e for e in load() if e["id"] == id), None)
        if not entry:
            return {"error": "No entry with that id."}

        def remove() -> dict:
            with lock:
                save([e for e in load() if e["id"] != id])
            return {"deleted": id}

        return api.confirm(f"Delete the entry '{entry['text'][:60]}'?", remove)
