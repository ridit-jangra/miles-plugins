import html
import re
from urllib.parse import quote

import httpx

HEADERS = {"User-Agent": "Miles/1.0 (voice assistant; https://github.com/ridit-jangra/miles-plugins)"}
SUMMARY_CHARS = 700


def trim(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: stop + 1] if stop > limit // 2 else cut.rstrip() + "..."


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def register(api):
    @api.tool
    async def wikipedia(topic: str) -> dict:
        """Look something up on Wikipedia and get a short summary. Good for quick facts about people, places and things.

        Args:
            topic: What to look up, like "Ada Lovelace" or "photosynthesis".
        """
        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
                found = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/title", params={"q": topic, "limit": 1}
                )
                found.raise_for_status()
                pages = found.json().get("pages", [])
                if not pages:
                    return {"error": f"Wikipedia has nothing on {topic!r}."}
                key = pages[0]["key"]
                r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(key, safe='')}")
                r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Wikipedia lookup failed: {e}"}
        data = r.json()
        if data.get("type") == "disambiguation":
            return {"title": data["title"], "note": "That name could mean several things.", "summary": trim(data.get("extract", ""), 300)}
        return {
            "title": data["title"],
            "description": data.get("description", ""),
            "summary": trim(data.get("extract", "")),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
        }

    @api.tool
    async def define(word: str) -> dict:
        """Get the meaning of a word, with its part of speech and an example when there is one.

        Args:
            word: The word to define.
        """
        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
                r = await client.get(f"https://en.wiktionary.org/api/rest_v1/page/definition/{quote(word.strip(), safe='')}")
            if r.status_code == 404:
                return {"error": f"No dictionary entry for {word!r}."}
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Dictionary lookup failed: {e}"}
        entries = r.json().get("en", [])
        if not entries:
            return {"error": f"No English entry for {word!r}."}
        meanings = []
        for entry in [e for e in entries if e["partOfSpeech"].lower() != "symbol"][:3]:
            definitions = [d for d in entry.get("definitions", []) if plain(d.get("definition", ""))]
            if not definitions:
                continue
            first = definitions[0]
            examples = first.get("examples") or first.get("parsedExamples") or []
            example = examples[0] if examples else ""
            if isinstance(example, dict):
                example = example.get("example", "")
            meanings.append({
                "part_of_speech": entry["partOfSpeech"].lower(),
                "definition": plain(first["definition"]),
                "example": plain(example),
            })
        return {"word": word.strip(), "meanings": meanings}

    @api.tool
    async def translate(text: str, to_language: str, from_language: str = "") -> dict:
        """Translate text into another language.

        Args:
            text: What to translate.
            to_language: The target language as a two letter code, like "es" for Spanish, "hi" for Hindi, "fr" for French.
            from_language: The language it is in as a two letter code. Leave empty to detect it.
        """
        source = from_language.strip().lower() or "Autodetect"
        target = to_language.strip().lower()
        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                r = await client.get(
                    "https://api.mymemory.translated.net/get", params={"q": text[:500], "langpair": f"{source}|{target}"}
                )
                r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Translation failed: {e}"}
        data = r.json()
        if data.get("responseStatus") != 200:
            return {"error": f"Translation failed: {data.get('responseDetails', 'unknown problem')}"}
        return {"translation": data["responseData"]["translatedText"], "to": target}
