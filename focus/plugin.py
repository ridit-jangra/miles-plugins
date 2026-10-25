import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from miles.watchers.inbox import Alert


def data_file() -> Path:
    home = Path(os.environ.get("MILES_HOME") or Path.home() / ".miles")
    return home / "data" / "focus.json"


def clock(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%I:%M %p").lstrip("0")


def minutes_left(ends: float) -> int:
    return max(0, round((ends - time.time()) / 60))


def register(api):
    path = data_file()
    lock = threading.Lock()

    def load() -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def save(state: dict | None) -> None:
        if state is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    resumed = load()
    if resumed and resumed["ends"] > time.time():
        api.quiet(resumed["ends"] - time.time())

    @api.tool
    def start_focus(minutes: int = 25, label: str = "", break_minutes: int = 5) -> dict:
        """Start a focus session. Alerts are held back until it ends, except urgent ones like alarms. Use it for "focus for 25 minutes" or a pomodoro.

        Args:
            minutes: How long to focus, from 1 to 240.
            label: What the user is working on.
            break_minutes: The break to suggest afterwards.
        """
        minutes = max(1, min(minutes, 240))
        ends = time.time() + minutes * 60
        with lock:
            save({"ends": ends, "label": label.strip(), "break": max(0, break_minutes)})
        api.quiet(minutes * 60)
        return {"focusing_for": f"{minutes} minutes", "until": clock(ends), "label": label.strip(), "note": "Alarms and urgent alerts still come through."}

    @api.tool
    def stop_focus() -> dict:
        """End the focus session early and catch up on the alerts that were held back."""
        with lock:
            state = load()
            save(None)
        if not state:
            return {"error": "No focus session is running."}
        held = api.quiet_off()
        return {"stopped": True, "alerts_held_back": held}

    @api.tool
    def focus_status() -> dict:
        """Say whether a focus session is running and how long is left."""
        with lock:
            state = load()
        if not state or state["ends"] <= time.time():
            return {"focusing": False}
        return {"focusing": True, "label": state["label"], "minutes_left": minutes_left(state["ends"]), "until": clock(state["ends"])}

    @api.watch(every="2s")
    def finish() -> list[Alert]:
        with lock:
            state = load()
            if not state or state["ends"] > time.time():
                return []
            save(None)
        what = f" on {state['label']}" if state["label"] else ""
        advice = f" Take a {state['break']} minute break." if state["break"] else ""
        return [Alert(f"Focus time{what} is up.{advice}", "focus", "urgent", f"focus-{int(state['ends'])}")]
