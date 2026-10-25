import asyncio
import ipaddress
import json
import mimetypes
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from miles.watchers.inbox import Alert

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Miles/1.0)"}
MAX_REDIRECTS = 5
KEEP_RECENT = 20
RISKY = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".js", ".jar", ".sh", ".run",
    ".appimage", ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".app",
}


def miles_home() -> Path:
    home = Path(os.environ.get("MILES_HOME") or Path.home() / ".miles")
    return home


def is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def size_text(size: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return ""


def safe_name(name: str) -> str:
    name = unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "", name).strip(" .")
    return name[:120]


def header_name(disposition: str) -> str:
    star = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.I)
    if star:
        return star.group(1).strip().strip('"')
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
    return plain.group(1).strip() if plain else ""


def pick_name(given: str, response: httpx.Response, url: str) -> str:
    name = safe_name(given) or safe_name(header_name(response.headers.get("content-disposition", "")))
    if not name:
        name = safe_name(urlparse(url).path)
    if not name:
        name = "download"
    if "." not in name:
        extension = mimetypes.guess_extension(response.headers.get("content-type", "").split(";")[0].strip())
        name += extension or ""
    return name


def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = path.stem, path.suffix
    count = 1
    while path.exists() or path.with_name(path.name + ".part").exists():
        path = folder / f"{stem} ({count}){suffix}"
        count += 1
    return path


def register(api):
    folder = Path(str(api.config.get("folder") or Path.home() / "Downloads" / "Miles")).expanduser()
    cap = float(api.config.get("max_mb", 1000)) * 1_000_000
    allow_private = api.config.get("allow_private", False) is True
    state_file = miles_home() / "data" / "downloads.json"
    lock = threading.Lock()
    running: set[asyncio.Task] = set()

    def load() -> list[dict]:
        try:
            return json.loads(state_file.read_text())
        except (OSError, ValueError):
            return []

    def save(entries: list[dict]) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(entries[-KEEP_RECENT:], indent=2))

    def update(entry_id: int, **fields) -> None:
        with lock:
            entries = load()
            for e in entries:
                if e["id"] == entry_id:
                    e.update(fields)
            save(entries)

    def add(entry: dict) -> dict:
        with lock:
            entries = load()
            entry["id"] = max((e["id"] for e in entries), default=0) + 1
            entries.append(entry)
            save(entries)
        return entry

    def fail_stale() -> None:
        with lock:
            entries = load()
            for e in entries:
                if e["state"] == "downloading" and e["pid"] != os.getpid():
                    e.update(state="failed", error="it was interrupted when Miles restarted", announced=False)
            save(entries)

    fail_stale()

    async def open_stream(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str]:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlparse(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("I can only download from normal web addresses.")
            if not allow_private and not await asyncio.to_thread(is_public, parts.hostname):
                raise ValueError("That address points at a private or unknown computer, so I won't download from it.")
            response = await client.send(client.build_request("GET", url), stream=True, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location", "")
                await response.aclose()
                if not location:
                    raise ValueError("The link redirected nowhere.")
                url = urljoin(url, location)
                continue
            return response, url
        raise ValueError("The link redirected too many times.")

    async def pull(entry: dict, client: httpx.AsyncClient, response: httpx.Response, final: Path) -> None:
        part = final.with_name(final.name + ".part")
        written = 0
        last = 0.0
        try:
            with open(part, "wb") as out:
                async for chunk in response.aiter_bytes(1 << 16):
                    written += len(chunk)
                    if written > cap:
                        raise ValueError(f"it is bigger than the {cap / 1e6:.0f} MB limit")
                    out.write(chunk)
                    if time.time() - last > 1:
                        last = time.time()
                        update(entry["id"], bytes=written)
            target = final if not final.exists() else unique_path(folder, final.name)
            part.rename(target)
            update(entry["id"], state="done", bytes=written, name=target.name, path=str(target), announced=False)
        except (httpx.HTTPError, OSError, ValueError) as e:
            part.unlink(missing_ok=True)
            update(entry["id"], state="failed", error=str(e) or type(e).__name__, announced=False)
        finally:
            await response.aclose()
            await client.aclose()

    @api.tool
    async def download_file(url: str, filename: str = "") -> dict:
        """Download a file from a web address into the user's Miles downloads folder. It runs in the background and the user is told when it finishes, so don't wait for it.

        Args:
            url: The full address of the file, starting with http:// or https://.
            filename: Optional name to save it as. Leave empty to use the file's own name.
        """
        url = url.strip()
        if "://" not in url:
            url = f"https://{url}"
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), headers=HEADERS)
        try:
            response, final_url = await open_stream(client, url)
            if response.status_code >= 400:
                await response.aclose()
                await client.aclose()
                return {"error": f"The server said {response.status_code}, so there's nothing to download there."}
            length = int(response.headers.get("content-length") or 0)
            if length > cap:
                await response.aclose()
                await client.aclose()
                return {"error": f"That file is {size_text(length)}, over the {cap / 1e6:.0f} MB limit."}
            name = pick_name(filename, response, final_url)
            folder.mkdir(parents=True, exist_ok=True)
            final = unique_path(folder, name)
        except (ValueError, httpx.HTTPError, OSError) as e:
            await client.aclose()
            return {"error": f"Couldn't start the download: {e}"}
        entry = add({
            "name": final.name, "path": str(final), "host": urlparse(final_url).netloc, "state": "downloading",
            "bytes": 0, "total": length, "pid": os.getpid(), "started": time.time(), "announced": True, "error": "",
        })
        task = asyncio.create_task(pull(entry, client, response, final))
        running.add(task)
        task.add_done_callback(running.discard)
        result = {"started": final.name, "id": entry["id"], "size": size_text(length) if length else "unknown size", "folder": folder.name}
        if final.suffix.lower() in RISKY:
            result["warning"] = "This is a program file. Don't run it unless you trust where it came from."
        return result

    @api.tool
    def download_status() -> dict:
        """Say how the recent downloads are doing: still going, finished or failed."""
        with lock:
            entries = load()[-5:]
        rows = []
        for e in reversed(entries):
            row = {"id": e["id"], "name": e["name"], "state": e["state"]}
            if e["state"] == "downloading":
                row["progress"] = f"{e['bytes'] * 100 // e['total']}%" if e["total"] else size_text(e["bytes"])
            elif e["state"] == "done":
                row["size"] = size_text(e["bytes"])
                row["path"] = e["path"]
            else:
                row["error"] = e["error"]
            rows.append(row)
        return {"downloads": rows} if rows else {"error": "No downloads yet."}

    @api.watch(every="3s")
    def announce() -> list[Alert]:
        alerts = []
        with lock:
            entries = load()
            for e in entries:
                if e["state"] == "downloading" and e["pid"] != os.getpid():
                    e.update(state="failed", error="it was interrupted when Miles restarted", announced=False)
                if e["state"] in ("done", "failed") and not e["announced"]:
                    e["announced"] = True
                    if e["state"] == "done":
                        text = f"Finished downloading {e['name']}, {size_text(e['bytes'])}"
                    else:
                        text = f"The download of {e['name']} failed because {e['error']}"
                    alerts.append(Alert(text, "download", "normal", f"download-{e['id']}-{e['state']}"))
            if alerts:
                save(entries)
        return alerts
