import asyncio
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Miles/1.0)"}
MAX_BYTES = 2_000_000
CACHE_SECONDS = 600

DEFAULT_FEEDS = {
    "anthropic": "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "openai": "https://openai.com/news/rss.xml",
    "techcrunch ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "bbc tech": "https://feeds.bbci.co.uk/news/technology/rss.xml",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def link_of(node: ET.Element) -> str:
    for child in node:
        if local_name(child.tag) == "link":
            return (child.get("href") or child.text or "").strip()
    return ""


def when_of(node: ET.Element) -> float:
    for name in ("pubDate", "published", "updated", "date"):
        text = child_text(node, name)
        if not text:
            continue
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return 0.0


def parse_feed(raw: bytes) -> list[dict]:
    if b"<!ENTITY" in raw:
        return []
    root = ET.fromstring(raw)
    items = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]
    return [
        {"title": child_text(n, "title"), "link": link_of(n), "time": when_of(n)}
        for n in items
        if child_text(n, "title")
    ]


def spoken_age(stamp: float) -> str:
    if not stamp:
        return ""
    hours = (time.time() - stamp) / 3600
    if hours < 1:
        return "under an hour ago"
    if hours < 24:
        return f"{int(hours)} hours ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''} ago"


def register(api):
    configured = api.config.get("feeds")
    feeds = {str(k).lower(): str(v) for k, v in configured.items()} if isinstance(configured, dict) and configured else DEFAULT_FEEDS
    cache: dict[str, tuple[float, list[dict]]] = {}

    async def read(name: str, client: httpx.AsyncClient) -> list[dict]:
        hit = cache.get(name)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            r = await client.get(feeds[name], follow_redirects=True)
            r.raise_for_status()
            items = parse_feed(r.content[:MAX_BYTES])
        except (httpx.HTTPError, ET.ParseError):
            return hit[1] if hit else []
        cache[name] = (time.time(), items)
        return items

    @api.tool
    def news_sources() -> dict:
        """List the news sources the user has set up, by name."""
        return {"sources": list(feeds)}

    @api.tool
    async def headlines(source: str = "", count: int = 5) -> dict:
        """Get the latest headlines, newest first. With no source it mixes all of the user's sources.

        Args:
            source: A source name from news_sources, like "anthropic". Leave empty for all of them.
            count: How many headlines, from 1 to 10.
        """
        count = max(1, min(count, 10))
        wanted = source.strip().lower()
        if wanted:
            names = [n for n in feeds if wanted in n]
            if not names:
                return {"error": f"No source called {source!r}. Sources: {', '.join(feeds)}."}
        else:
            names = list(feeds)
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            lists = await asyncio.gather(*(read(n, client) for n in names))
        merged = [
            {"source": n, "title": i["title"], "age": spoken_age(i["time"]), "link": i["link"], "time": i["time"]}
            for n, items in zip(names, lists)
            for i in items
        ]
        merged.sort(key=lambda i: i["time"], reverse=True)
        if not merged:
            return {"error": "Couldn't load any headlines right now."}
        return {"headlines": [{k: v for k, v in i.items() if k != "time"} for i in merged[:count]]}
