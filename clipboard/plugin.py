import os
import shutil
import subprocess
import sys

from miles.context import SECRET_PATTERN

LIMIT = 3000
HINT = "No clipboard tool found. On Linux install wl-clipboard (Wayland) or xclip (X11)."


def commands() -> tuple[list[str], list[str]] | None:
    if sys.platform == "darwin":
        return ["pbpaste"], ["pbcopy"]
    if sys.platform == "win32":
        return (
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
        )
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste") and shutil.which("wl-copy"):
        return ["wl-paste", "--no-newline"], ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
    return None


def register(api):
    @api.tool
    def read_clipboard() -> dict:
        """Read the text the user last copied. Use it for "what did I copy" or "read my clipboard"."""
        found = commands()
        if not found:
            return {"error": HINT}
        try:
            result = subprocess.run(found[0], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"Couldn't read the clipboard: {e}"}
        text = result.stdout.strip()
        if result.returncode != 0 or not text:
            return {"error": "The clipboard is empty or holds something that isn't text."}
        if SECRET_PATTERN.search(text):
            return {"error": "The clipboard looks like it holds a key or secret, so I left it alone."}
        clipped = len(text) > LIMIT
        return {"text": text[:LIMIT], "truncated": clipped, "characters": len(text)}

    @api.tool
    def copy_to_clipboard(text: str) -> dict:
        """Put text on the user's clipboard so they can paste it.

        Args:
            text: The exact text to copy.
        """
        found = commands()
        if not found:
            return {"error": HINT}
        if not text:
            return {"error": "There was no text to copy."}
        try:
            subprocess.run(
                found[1], input=text, text=True, timeout=5, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"Couldn't copy: {e}"}
        return {"copied": len(text)}
