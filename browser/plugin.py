import asyncio
import os
import subprocess
import sys
from urllib.parse import urlparse


def register(api):
    @api.tool
    async def open_url(url: str) -> dict:
        """Open a web address in the user's normal browser so they can look at it themselves. Use it for "open YouTube" or "pull up that page".

        Args:
            url: The full address, starting with http:// or https://. Add https:// to a bare domain.
        """
        url = url.strip()
        if "://" not in url:
            url = f"https://{url}"
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return {"error": "I can only open normal web addresses."}

        def launch() -> None:
            if sys.platform == "win32":
                os.startfile(url)  # noqa: S606
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )

        try:
            await asyncio.to_thread(launch)
        except OSError as e:
            return {"error": f"Couldn't open the browser: {e}"}
        return {"opened": parts.netloc}
