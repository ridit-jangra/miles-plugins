import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_PORT = 9333
LINUX_BROWSERS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome")
MAC_BROWSER = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WINDOWS_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def miles_home() -> Path:
    plugins = Path(os.environ.get("MILES_PLUGINS_DIR") or Path.home() / ".miles" / "plugins")
    return plugins.parent


def find_chrome(configured: str) -> str | None:
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    if sys.platform == "darwin":
        return MAC_BROWSER if Path(MAC_BROWSER).exists() else None
    if sys.platform == "win32":
        return next((p for p in WINDOWS_BROWSERS if Path(p).exists()), None)
    return next((shutil.which(name) for name in LINUX_BROWSERS if shutil.which(name)), None)


def same_page(a: str, b: str) -> bool:
    x, y = urlparse(a), urlparse(b)
    return (x.netloc, x.path.rstrip("/"), x.query) == (y.netloc, y.path.rstrip("/"), y.query)


def register(api):
    port = int(api.config.get("port", DEFAULT_PORT))
    base = f"http://127.0.0.1:{port}"
    profile = Path(str(api.config.get("profile_dir") or miles_home() / "browser-profile")).expanduser()
    headless = api.config.get("headless", False) is True
    chrome = find_chrome(str(api.config.get("executable", "")))
    state_file = miles_home() / "data" / "browser" / "chrome.json"

    async def alive(client: httpx.AsyncClient) -> bool:
        try:
            return (await client.get(f"{base}/json/version", timeout=2)).status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure() -> dict:
        async with httpx.AsyncClient() as client:
            if await alive(client):
                return {"ready": True, "launched": False}
            if not chrome:
                return {"error": "I couldn't find Chrome. Install Google Chrome or set executable under [plugin_settings.browser]."}
            cmd = [
                chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check",
            ]
            if headless:
                cmd.append("--headless=new")
            profile.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(  # noqa: S603, ASYNC220
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"pid": proc.pid, "port": port, "started": time.time()}))
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await alive(client):
                    return {"ready": True, "launched": True}
        return {"error": "Chrome didn't open its control port. If a Miles browser window is already open, close it and try again."}

    @api.tool
    async def ensure_browser() -> dict:
        """Make sure the shared browser window is open and ready to be controlled. Call this first, before any other browser tool."""
        return await ensure()

    @api.tool
    async def open_url(url: str) -> dict:
        """Open a web address in the Miles browser window. If that page is already open it switches to that tab instead of opening another. Use it for "open YouTube" or "pull up that page", not for reading or clicking on a page.

        Args:
            url: The full address, starting with http:// or https://. Add https:// to a bare domain.
        """
        url = url.strip()
        if "://" not in url:
            url = f"https://{url}"
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return {"error": "I can only open normal web addresses."}
        ready = await ensure()
        if "error" in ready:
            return ready
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                tabs = [t for t in (await client.get(f"{base}/json/list")).json() if t.get("type") == "page"]
                existing = next((t for t in tabs if same_page(t.get("url", ""), url)), None)
                if existing:
                    await client.get(f"{base}/json/activate/{existing['id']}")
                    return {"opened": parts.netloc, "reused_tab": True}
                response = await client.put(f"{base}/json/new?{url}")
                response.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Couldn't open the page: {e}"}
        return {"opened": parts.netloc, "reused_tab": False}
