import asyncio
import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from miles.context import SECRET_PATTERN

TEXT_LIMIT = 4000
NO_SHOT = "Couldn't take a screenshot. On Linux install spectacle (KDE), grim (Sway or Hyprland), gnome-screenshot or scrot."
WINDOWS_SHOT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
    "$g=[System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); $bmp.Save('{path}')"
)


def screenshot_command(path: Path, window: bool) -> list[str] | None:
    if sys.platform == "darwin":
        return ["screencapture", "-x", *(["-w"] if window else []), str(path)]
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", WINDOWS_SHOT.format(path=path)]
    if shutil.which("spectacle"):
        return ["spectacle", "-b", "-n", "-a" if window else "-f", "-o", str(path)]
    if shutil.which("grim"):
        return ["grim", str(path)]
    if shutil.which("gnome-screenshot"):
        return ["gnome-screenshot", *(["-w"] if window else []), "-f", str(path)]
    if shutil.which("scrot"):
        return ["scrot", *(["-u"] if window else []), "-o", str(path)]
    return None


async def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def capture(window: bool) -> Path:
    fd, name = tempfile.mkstemp(suffix=".png", prefix="miles-screen-")
    os.close(fd)
    shot_path = Path(name)
    cmd = screenshot_command(shot_path, window)
    if not cmd:
        shot_path.unlink(missing_ok=True)
        raise RuntimeError(NO_SHOT)
    try:
        await run(cmd, 20)
    except (OSError, asyncio.TimeoutError) as e:
        shot_path.unlink(missing_ok=True)
        raise RuntimeError(f"The screenshot tool failed: {e!r}") from e
    if not shot_path.exists() or shot_path.stat().st_size == 0:
        shot_path.unlink(missing_ok=True)
        raise RuntimeError("The screenshot came out empty. The screen may need to be unlocked or screen capture allowed.")
    return shot_path


def hide_secrets(text: str) -> str:
    return "\n".join("[hidden: looks like a secret]" if SECRET_PATTERN.search(line) else line for line in text.splitlines())


def register(api):
    ask_first = api.config.get("ask_first", True) is not False
    model = str(api.config.get("model", "qwen2.5vl:7b"))
    ollama = str(api.config.get("ollama_url", "http://localhost:11434")).rstrip("/")
    language = str(api.config.get("ocr_language", "eng"))

    async def read_text(window: bool) -> dict:
        if not shutil.which("tesseract"):
            return {"error": "tesseract isn't installed. Install it (on Arch: sudo pacman -S tesseract tesseract-data-eng) to read screen text."}
        try:
            shot = await capture(window)
        except RuntimeError as e:
            return {"error": str(e)}
        try:
            code, text = await run(["tesseract", str(shot), "stdout", "-l", language], 60)
        except (OSError, asyncio.TimeoutError) as e:
            return {"error": f"Reading the screen failed: {e!r}"}
        finally:
            shot.unlink(missing_ok=True)
        text = hide_secrets("\n".join(line.rstrip() for line in text.splitlines() if line.strip()))
        if code != 0 or not text.strip():
            return {"error": "I couldn't find any text on the screen."}
        return {"text": text[:TEXT_LIMIT], "truncated": len(text) > TEXT_LIMIT}

    async def describe(question: str, window: bool) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=5)) as client:
                tags = await client.get(f"{ollama}/api/tags")
                have = {m["name"] for m in tags.json().get("models", [])}
                if model not in have and f"{model}:latest" not in have:
                    return {"error": f"The vision model {model!r} isn't installed. Run: ollama pull {model}"}
                try:
                    shot = await capture(window)
                except RuntimeError as e:
                    return {"error": str(e)}
                try:
                    image = base64.b64encode(shot.read_bytes()).decode()
                finally:
                    shot.unlink(missing_ok=True)
                r = await client.post(
                    f"{ollama}/api/chat",
                    json={
                        "model": model,
                        "stream": False,
                        "keep_alive": "10m",
                        "messages": [{"role": "user", "content": question, "images": [image]}],
                    },
                )
                r.raise_for_status()
        except httpx.ConnectError:
            return {"error": f"Couldn't reach Ollama at {ollama}. Is it running?"}
        except httpx.HTTPError as e:
            return {"error": f"The vision model failed: {e!r}"}
        answer = r.json().get("message", {}).get("content", "").strip()
        return {"answer": hide_secrets(answer)} if answer else {"error": "The vision model gave no answer."}

    @api.tool
    def read_screen_text(window_only: bool = False) -> dict:
        """Read the text that is on the user's screen right now, using OCR. Use it for "copy what's on screen" together with copy_to_clipboard.

        Args:
            window_only: True to read only the active window instead of the whole screen.
        """
        if ask_first:
            return api.confirm("Can I look at your screen?", lambda: read_text(window_only))
        return read_text(window_only)

    @api.tool
    def describe_screen(question: str = "Describe what is on the screen.", window_only: bool = False) -> dict:
        """Look at the user's screen and answer a question about it, like "what am I looking at" or "what does this error mean".

        Args:
            question: What to find out about the screen.
            window_only: True to look only at the active window.
        """
        if ask_first:
            return api.confirm("Can I look at your screen?", lambda: describe(question, window_only))
        return describe(question, window_only)
