"""Weather (Open-Meteo: free, no account, no tracking) and typical commute time (OSRM route planner, no live traffic).
Set the location in Setup > Weather & commute."""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
OSRM = "https://router.project-osrm.org/route/v1/driving"
CACHE = Path("data") / "weather_cache.json"

CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog", 51: "light drizzle",
         53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
         65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
         77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
         95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


class WeatherError(Exception):
    pass


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save(c: dict) -> None:
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(c))


def geocode(place: str) -> dict:
    """'Pune, India' -> {name, lat, lon, country, timezone}. Cached."""
    place = (place or "").strip()
    if not place:
        raise WeatherError("No location set (Setup > Weather & commute)")
    c = _cache()
    if place.lower() in c.get("geo", {}):
        return c["geo"][place.lower()]
    name, _, rest = place.partition(",")
    try:
        r = requests.get(GEO, params={"name": name.strip(), "count": 10, "language": "en", "format": "json"}, timeout=15)
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError) as e:
        raise WeatherError(f"Couldn't look up '{place}': {e}")
    hint = rest.strip().lower()
    if hint:
        results = [x for x in results if hint in " ".join(str(x.get(k, "")) for k in ("country", "admin1", "admin2", "country_code")).lower()] or results
    if not results:
        raise WeatherError(f"Couldn't find a place called '{place}'. Try 'City, Country'")
    x = results[0]
    g = {"name": ", ".join(p for p in (x.get("name"), x.get("admin1"), x.get("country")) if p), "lat": x["latitude"],
         "lon": x["longitude"], "timezone": x.get("timezone", "auto")}
    c.setdefault("geo", {})[place.lower()] = g
    _save(c)
    return g


def forecast(place: str, units: str = "metric") -> dict:
    """Today and the next days, cached for 30 minutes."""
    g = geocode(place)
    key = f"{g['lat']:.3f},{g['lon']:.3f},{units}"
    c = _cache()
    hit = c.get("fc", {}).get(key)
    if hit and time.time() - hit["at"] < 1800:
        return hit["data"]
    params = {"latitude": g["lat"], "longitude": g["lon"], "timezone": "auto", "forecast_days": 4,
              "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
              "hourly": "precipitation_probability,weather_code,temperature_2m",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset"}
    if units == "imperial":
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
    try:
        r = requests.get(FORECAST, params=params, timeout=15)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise WeatherError(f"Couldn't get the weather: {e}")
    if "daily" not in data:
        raise WeatherError(f"Weather service error: {data.get('reason', r.status_code)}")
    data["place"] = g["name"]
    c.setdefault("fc", {})[key] = {"at": time.time(), "data": data}
    _save(c)
    return data


def describe_day(data: dict, offset: int = 0, units: str = "metric") -> str:
    d = data["daily"]
    deg = "°F" if units == "imperial" else "°C"
    i = min(offset, len(d["time"]) - 1)
    day = date.fromisoformat(d["time"][i])
    label = "Today" if offset == 0 else "Tomorrow" if offset == 1 else f"{day:%A}"
    cond = CODES.get(d["weather_code"][i], "mixed")
    line = f"{label} in {data['place'].split(',')[0]}: {cond}, {round(d['temperature_2m_min'][i])} to {round(d['temperature_2m_max'][i])}{deg}"
    rain = d.get("precipitation_probability_max", [None])[i]
    if rain is not None and rain >= 30:
        # when it's likely to rain: the first hour of that day with a 50%+ chance
        h = data.get("hourly", {})
        hours = [(t, p) for t, p in zip(h.get("time", []), h.get("precipitation_probability", [])) if t.startswith(d["time"][i]) and p is not None]
        start = next((t for t, p in hours if p >= 50 and t[11:13] >= ("06" if offset else datetime.now().strftime("%H"))), None)
        line += f", {rain}% chance of rain" + (f" from about {int(start[11:13]) % 12 or 12}{'am' if int(start[11:13]) < 12 else 'pm'}" if start else "")
        if rain >= 50:
            line += ". Take an umbrella"
    if offset == 0 and data.get("current"):
        cur = data["current"]
        line += f". Now {round(cur['temperature_2m'])}{deg}"
        if abs(cur["apparent_temperature"] - cur["temperature_2m"]) >= 3:
            line += f", feels like {round(cur['apparent_temperature'])}{deg}"
    return line + "."


def commute(home: str, work: str) -> str:
    """Typical driving time between two places (OSRM; no live traffic)."""
    a, b = geocode(home), geocode(work)
    try:
        r = requests.get(f"{OSRM}/{a['lon']},{a['lat']};{b['lon']},{b['lat']}", params={"overview": "false"}, timeout=15)
        route = r.json()["routes"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        raise WeatherError(f"Couldn't work out the route: {e}")
    mins = round(route["duration"] / 60)
    km = route["distance"] / 1000
    return f"Commute to {b['name'].split(',')[0]}: about {mins} min by car ({km:.0f} km), without live traffic."


def report_lines(cfg: dict) -> list[str]:
    loc = cfg.get("location") or {}
    home = loc.get("home")
    if not home:
        return []
    units = loc.get("units") or "metric"
    out = []
    try:
        out.append(describe_day(forecast(home, units), 0, units))
    except WeatherError as e:
        out.append(f"(Weather unavailable: {e})")
    if loc.get("work") and datetime.now().weekday() < 5:
        try:
            out.append(commute(home, loc["work"]))
        except WeatherError:
            pass
    return out


def test(cfg: dict) -> str:
    loc = cfg.get("location") or {}
    g = geocode(loc.get("home", ""))
    msg = f"Found {g['name']}. " + describe_day(forecast(loc["home"], loc.get("units") or "metric"), 0, loc.get("units") or "metric")
    if loc.get("work"):
        msg += " " + commute(loc["home"], loc["work"])
    return msg
