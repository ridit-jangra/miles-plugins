import asyncio
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPLAY_MIN, REPLAY_MAX = 5, 300
RECORDER_HINT = (
    "No screen recorder found. On Linux install gpu-screen-recorder (best for clipping, "
    "on Arch: sudo pacman -S gpu-screen-recorder) or wf-recorder (Sway and Hyprland only)."
)
WINDOWS_SHOT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
    "$g=[System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); $bmp.Save('{path}')"
)


def data_dir() -> Path:
    plugins = Path(os.environ.get("MILES_PLUGINS_DIR") or Path.home() / ".miles" / "plugins")
    return plugins.parent / "data" / "clip"


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


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


def alive(pid: int, name: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return name.encode() in cmdline.read_bytes()
        except OSError:
            return False
    return True


def register(api):
    shots = Path(str(api.config.get("screenshots_dir") or Path.home() / "Pictures" / "Miles")).expanduser()
    clips = Path(str(api.config.get("clips_dir") or Path.home() / "Videos" / "Miles")).expanduser()
    target = str(api.config.get("capture") or ("portal" if os.environ.get("WAYLAND_DISPLAY") else "screen"))
    audio = str(api.config.get("audio", ""))
    fps = int(api.config.get("fps", 60))
    state_file = data_dir() / "recorder.json"
    log_file = data_dir() / "recorder.log"

    def read_state() -> dict | None:
        try:
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            return None
        if alive(state["pid"], state["program"]):
            return state
        state_file.unlink(missing_ok=True)
        return None

    def recorder() -> str | None:
        if sys.platform != "linux":
            return None
        return next((p for p in ("gpu-screen-recorder", "wf-recorder") if shutil.which(p)), None)

    def stop_recorder() -> None:
        state = read_state()
        if state:
            try:
                os.kill(state["pid"], signal.SIGINT)
            except OSError:
                pass
            state_file.unlink(missing_ok=True)

    atexit.register(stop_recorder)

    async def launch(cmd: list[str], mode: str, output: Path, program: str) -> dict:
        data_dir().mkdir(parents=True, exist_ok=True)
        with open(log_file, "wb") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)  # noqa: ASYNC220, S603
        await asyncio.sleep(1.5)
        if proc.poll() is not None:
            tail = log_file.read_text(errors="replace").strip().splitlines()[-3:]
            return {"error": f"The recorder stopped right away. {' '.join(tail)}".strip()}
        state_file.write_text(json.dumps({"pid": proc.pid, "program": program, "mode": mode, "output": str(output), "started": time.time()}))
        return {"ok": True}

    def gsr_command(extra: list[str]) -> list[str]:
        cmd = ["gpu-screen-recorder", "-w", target, "-f", str(fps), "-c", "mp4"]
        if target == "portal":
            cmd += ["-restore-portal-session", "yes"]
        if audio:
            cmd += ["-a", audio]
        return cmd + extra

    def newest(folder: Path, since: float) -> Path | None:
        found = [p for p in folder.glob("*.mp4") if p.stat().st_mtime >= since]
        return max(found, key=lambda p: p.stat().st_mtime) if found else None

    @api.tool
    async def take_screenshot(window_only: bool = False) -> dict:
        """Take a screenshot and save it as a picture file.

        Args:
            window_only: True to capture only the active window instead of the whole screen.
        """
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"screenshot-{stamp()}.png"
        cmd = screenshot_command(path, window_only)
        if not cmd:
            return {"error": "Couldn't take a screenshot. On Linux install spectacle (KDE), grim, gnome-screenshot or scrot."}
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 20)
        except (OSError, asyncio.TimeoutError) as e:
            return {"error": f"The screenshot tool failed: {e!r}"}
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            return {"error": "The screenshot came out empty. Screen capture may need to be allowed."}
        return {"saved": path.name, "folder": path.parent.name, "path": str(path)}

    @api.tool
    async def start_replay_buffer(seconds: int = 30) -> dict:
        """Start keeping the last few seconds of the screen in memory so clip_that can save them. It runs until stopped.

        Args:
            seconds: How much to keep, from 5 to 300 seconds.
        """
        if read_state():
            return {"error": "The recorder is already running. Stop it first."}
        program = recorder()
        if program != "gpu-screen-recorder":
            return {"error": "The replay buffer needs gpu-screen-recorder. On Arch: sudo pacman -S gpu-screen-recorder."}
        seconds = max(REPLAY_MIN, min(seconds, REPLAY_MAX))
        clips.mkdir(parents=True, exist_ok=True)
        result = await launch(gsr_command(["-r", str(seconds), "-o", str(clips)]), "replay", clips, program)
        return result if "error" in result else {"keeping_seconds": seconds}

    @api.tool
    async def clip_that() -> dict:
        """Save the last moments of the screen as a video clip. Needs the replay buffer to be running."""
        state = read_state()
        if not state or state["mode"] != "replay":
            return {"error": "The replay buffer isn't running. Start it first with start_replay_buffer."}
        before = time.time() - 1
        try:
            os.kill(state["pid"], signal.SIGUSR1)
        except OSError as e:
            return {"error": f"Couldn't reach the recorder: {e!r}"}
        for _ in range(20):
            await asyncio.sleep(0.5)
            saved = newest(clips, before)
            if saved:
                return {"saved": saved.name, "folder": saved.parent.name, "path": str(saved)}
        return {"error": "The clip didn't show up. Check the recorder is working."}

    @api.tool
    async def start_recording(window_only: bool = False) -> dict:
        """Start recording the screen to a video file. It runs until stop_recording.

        Args:
            window_only: True to record only the focused window (gpu-screen-recorder only).
        """
        if read_state():
            return {"error": "The recorder is already running. Stop it first."}
        program = recorder()
        if not program:
            return {"error": RECORDER_HINT}
        clips.mkdir(parents=True, exist_ok=True)
        path = clips / f"recording-{stamp()}.mp4"
        if program == "gpu-screen-recorder":
            cmd = gsr_command(["-o", str(path)])
            if window_only:
                cmd[cmd.index("-w") + 1] = "focused"
        else:
            cmd = ["wf-recorder", "-f", str(path)]
        result = await launch(cmd, "record", path, program)
        return result if "error" in result else {"recording": path.name}

    @api.tool
    async def stop_recording() -> dict:
        """Stop the screen recording or the replay buffer and finish the video file."""
        state = read_state()
        if not state:
            return {"error": "Nothing is recording."}
        try:
            os.kill(state["pid"], signal.SIGINT)
        except OSError as e:
            return {"error": f"Couldn't stop the recorder: {e!r}"}
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not alive(state["pid"], state["program"]):
                break
        state_file.unlink(missing_ok=True)
        if state["mode"] == "replay":
            return {"stopped": "replay buffer"}
        path = Path(state["output"])
        return {"saved": path.name, "folder": path.parent.name, "path": str(path)} if path.exists() else {"error": "The recording file wasn't created."}

    @api.tool
    def recorder_status() -> dict:
        """Say whether the screen recorder or replay buffer is running, and for how long."""
        state = read_state()
        if not state:
            return {"running": False}
        return {"running": True, "mode": state["mode"], "seconds": int(time.time() - state["started"])}

