import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Miles/1.0)"}

CONDITIONS = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

UNITS = {
    "metric": {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"},
    "imperial": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"},
}
LABELS = {"metric": ("°C", "km/h", "mm"), "imperial": ("°F", "mph", "inch")}


def describe(code: int | None) -> str:
    return CONDITIONS.get(code, "unknown conditions")


def register(api):
    default_city = str(api.config.get("city", "")).strip()
    system = "imperial" if str(api.config.get("units", "metric")).lower() == "imperial" else "metric"
    degrees, wind, rain = LABELS[system]
    places: dict[str, dict] = {}

    async def locate(place: str) -> dict | None:
        place = place.strip() or default_city
        if not place:
            return None
        if place.lower() not in places:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                r = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": place, "count": 1, "language": "en", "format": "json"},
                )
                r.raise_for_status()
            found = r.json().get("results") or []
            if not found:
                return {"missing": place}
            hit = found[0]
            label = ", ".join(x for x in (hit["name"], hit.get("admin1"), hit.get("country")) if x)
            places[place.lower()] = {"label": label, "lat": hit["latitude"], "lon": hit["longitude"]}
        return places[place.lower()]

    async def fetch(place: str, days: int) -> dict:
        where = await locate(place)
        if where is None:
            return {"error": "No city given. Ask the user for one, or set city under [plugin_settings.weather] in config.toml."}
        if "missing" in where:
            return {"error": f"Couldn't find a place called {where['missing']!r}."}
        params = {
            "latitude": where["lat"],
            "longitude": where["lon"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": days,
            "timezone": "auto",
            **UNITS[system],
        }
        try:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
                r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"Weather lookup failed: {e}"}
        return {"place": where["label"], "data": r.json()}

    @api.tool
    async def weather_now(place: str = "") -> dict:
        """Get the current weather: conditions, temperature, what it feels like, humidity, wind and rain.

        Args:
            place: A city name. Leave empty to use the user's own city.
        """
        result = await fetch(place, 1)
        if "error" in result:
            return result
        now = result["data"]["current"]
        return {
            "place": result["place"],
            "conditions": describe(now.get("weather_code")),
            "temperature": f"{now['temperature_2m']}{degrees}",
            "feels_like": f"{now['apparent_temperature']}{degrees}",
            "humidity": f"{now['relative_humidity_2m']}%",
            "wind": f"{now['wind_speed_10m']} {wind}",
            "rain_now": f"{now['precipitation']} {rain}",
        }

    @api.tool
    async def weather_forecast(place: str = "", days: int = 3) -> dict:
        """Get the daily forecast: conditions, high and low, and the chance of rain.

        Args:
            place: A city name. Leave empty to use the user's own city.
            days: How many days to cover, from 1 to 7.
        """
        result = await fetch(place, max(1, min(days, 7)))
        if "error" in result:
            return result
        daily = result["data"]["daily"]
        return {
            "place": result["place"],
            "days": [
                {
                    "date": daily["time"][i],
                    "conditions": describe(daily["weather_code"][i]),
                    "high": f"{daily['temperature_2m_max'][i]}{degrees}",
                    "low": f"{daily['temperature_2m_min'][i]}{degrees}",
                    "rain_chance": f"{daily['precipitation_probability_max'][i]}%",
                }
                for i in range(len(daily["time"]))
            ],
        }
