import os
import subprocess
import sys
import time
from pathlib import Path

from send2trash import send2trash

SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages", "target", "dist", "build"}
SEARCH_SECONDS = 6
DEFAULT_FOLDERS = ["Documents", "Downloads", "Desktop", "Pictures", "Videos", "Music"]


def spoken_age(stamp: float) -> str:
    days = int((time.time() - stamp) // 86400)
    if days < 1:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    return f"{days // 30} months ago"


def size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return ""


def open_with_system(path: Path) -> dict:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(
            ["open" if sys.platform == "darwin" else "xdg-open", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    return {"opened": path.name}


def register(api):
    configured = api.config.get("roots")
    if isinstance(configured, list) and configured:
        roots = [Path(str(r)).expanduser().resolve() for r in configured]
    else:
        roots = [Path.home() / name for name in DEFAULT_FOLDERS]
    roots = [r for r in roots if r.is_dir()]

    def inside(path: Path) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def walk(folder: str):
        starts = roots
        if folder.strip():
            wanted = (Path(folder).expanduser() if folder.startswith(("~", "/")) else None)
            matches = [wanted.resolve()] if wanted and wanted.is_dir() else [r for r in roots if folder.lower() in r.name.lower()]
            starts = [m for m in matches if inside(m)]
        for start in starts:
            for current, dirs, names in os.walk(start):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
                for name in names:
                    if not name.startswith("."):
                        yield Path(current) / name

    def describe(path: Path, stat: os.stat_result) -> dict:
        return {
            "name": path.name,
            "folder": path.parent.name,
            "path": str(path),
            "modified": spoken_age(stat.st_mtime),
            "size": size_text(stat.st_size),
        }

    def check(path: str) -> tuple[Path | None, dict | None]:
        target = Path(path).expanduser().resolve()
        if not inside(target):
            return None, {"error": "That file isn't in one of the folders I'm allowed to use."}
        if not target.is_file():
            return None, {"error": "That file doesn't exist."}
        return target, None

    @api.tool
    def find_files(query: str, folder: str = "", limit: int = 8) -> dict:
        """Find files by name, newest first. Every word in the query has to appear in the file name.

        Args:
            query: Words from the name, like "tax pdf" or "resume".
            folder: Optional folder name to look in, like "Downloads". Leave empty to search everywhere.
            limit: Max number of files to return.
        """
        words = [w for w in query.lower().split() if w]
        if not words:
            return {"error": "What should I look for?"}
        if not roots:
            return {"error": "None of the folders I search exist. Set roots under [plugin_settings.files] in config.toml."}
        deadline = time.time() + SEARCH_SECONDS
        found = []
        for path in walk(folder):
            lowered = path.name.lower()
            if all(w in lowered for w in words):
                try:
                    found.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            if time.time() > deadline:
                break
        found.sort(reverse=True)
        files = []
        for _, path in found[: max(1, min(limit, 20))]:
            files.append(describe(path, path.stat()))
        return {"files": files, "searched_fully": time.time() <= deadline} if files else {"error": f"No file matching {query!r}."}

    @api.tool
    def recent_files(days: int = 7, folder: str = "", limit: int = 8) -> dict:
        """List the files changed in the last few days, newest first.

        Args:
            days: How far back to look.
            folder: Optional folder name like "Downloads". Leave empty for all folders.
            limit: Max number of files to return.
        """
        cutoff = time.time() - max(1, days) * 86400
        deadline = time.time() + SEARCH_SECONDS
        found = []
        for path in walk(folder):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                found.append((stat.st_mtime, path, stat))
            if time.time() > deadline:
                break
        found.sort(key=lambda item: item[0], reverse=True)
        files = [describe(path, stat) for _, path, stat in found[: max(1, min(limit, 20))]]
        return {"files": files} if files else {"error": "No files changed in that time."}

    @api.tool
    def open_file(path: str) -> dict:
        """Open a file with its normal app, after the user says yes. Use a path from find_files or recent_files.

        Args:
            path: The full path of the file.
        """
        target, error = check(path)
        if error:
            return error
        return api.confirm(f"Open {target.name}?", lambda: open_with_system(target))

    @api.tool
    def trash_file(path: str) -> dict:
        """Move a file to the trash, after the user says yes. It can be restored from the trash. Use a path from find_files.

        Args:
            path: The full path of the file.
        """
        target, error = check(path)
        if error:
            return error

        def move() -> dict:
            send2trash(str(target))
            return {"trashed": target.name}

        return api.confirm(f"Move {target.name} to the trash?", move)
