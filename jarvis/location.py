"""Location history kept on your own server: OwnTracks (iPhone/Android) and the Jarvis Android app send points here.
Jarvis works out stops, distances, time at home/work and other places, answers "where was I", and runs location
reminders ("when I get home"). Nothing is looked up online: you name your places."""

import hashlib
import json
import math
import re
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path

TRACKERS = Path("data") / "trackers.json"
STAY_METERS, STAY_MINUTES = 120, 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS loc_points (id INTEGER PRIMARY KEY, tracker TEXT NOT NULL, t REAL NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    acc REAL, batt INTEGER, speed REAL);
CREATE INDEX IF NOT EXISTS loc_points_t ON loc_points(t);
CREATE TABLE IF NOT EXISTS loc_places (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT DEFAULT 'other', lat REAL NOT NULL, lon REAL NOT NULL,
    radius REAL DEFAULT 150, created TEXT);
CREATE TABLE IF NOT EXISTS loc_state (tracker TEXT PRIMARY KEY, place INTEGER, since REAL, last_t REAL);
CREATE TABLE IF NOT EXISTS loc_reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, place INTEGER NOT NULL, event TEXT NOT NULL,
    created TEXT, done INTEGER DEFAULT 0);
"""


def _ensure_schema(c):
    from . import organizer
    organizer.ensure(c, SCHEMA)


def _db():
    from . import organizer
    c = organizer.db()
    return c


def meters(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- trackers (each with its own access code)

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(TRACKERS.read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    TRACKERS.parent.mkdir(exist_ok=True)
    TRACKERS.write_text(json.dumps(items, indent=1))


def trackers() -> list[dict]:
    return [{k: t.get(k) for k in ("id", "name", "kind", "created", "last_seen")} for t in _load()]


def add_tracker(name: str, kind: str = "owntracks") -> dict:
    name = re.sub(r"\s+", " ", (name or "").strip())[:40] or "Phone"
    items = _load()
    if any(t["name"].lower() == name.lower() for t in items):
        raise ValueError(f"There's already a tracker called {name}")
    token = "jl_" + secrets.token_urlsafe(24)
    t = {"id": secrets.token_hex(4), "name": name, "kind": kind, "hash": _hash(token), "created": time.strftime("%Y-%m-%d %H:%M"), "last_seen": None}
    items.append(t)
    _save(items)
    return {"id": t["id"], "name": name, "token": token}


def remove_tracker(tracker_id: str, delete_history: bool = False) -> None:
    items = _load()
    gone = next((t for t in items if t["id"] == tracker_id), None)
    _save([t for t in items if t["id"] != tracker_id])
    if gone and delete_history:
        with _db() as c:
            _ensure_schema(c)
            c.execute("DELETE FROM loc_points WHERE tracker = ?", (gone["name"],))


def tracker_for(token: str | None) -> dict | None:
    if not token:
        return None
    h = _hash(token)
    items = _load()
    for t in items:
        if secrets.compare_digest(t["hash"], h):
            now = time.strftime("%Y-%m-%d %H:%M")
            if t.get("last_seen") != now:
                t["last_seen"] = now
                _save(items)
            return t
    return None


def owntracks_config(base_url: str, tracker: dict, token: str) -> str:
    """An owntracks:/// link (shown as a QR code) that sets up the OwnTracks app to send to Jarvis."""
    import base64
    cfg = {"_type": "configuration", "mode": 3, "url": base_url.rstrip("/") + "/api/location/owntracks", "auth": True,
           "username": "jarvis", "password": token, "deviceId": re.sub(r"\W+", "", tracker["name"].lower())[:16] or "phone",
           "tid": "".join(w[0] for w in tracker["name"].split()[:2]).upper() or "ME", "monitoring": 1, "locatorInterval": 300}
    return "owntracks:///config?inline=" + base64.b64encode(json.dumps(cfg).encode()).decode()


# ---------------------------------------------------------------- points in

def add_point(tracker: str, lat: float, lon: float, t: float | None = None, acc: float | None = None,
              batt: int | None = None, speed: float | None = None) -> list[tuple[str, str, str]]:
    """Store a point; returns location reminders that fired (kind, title, text)."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("bad coordinates")
    t = float(t or time.time())
    with _db() as c:
        _ensure_schema(c)
        c.execute("INSERT INTO loc_points (tracker, t, lat, lon, acc, batt, speed) VALUES (?,?,?,?,?,?,?)", (tracker, t, lat, lon, acc, batt, speed))
    if acc is not None and acc > 300:
        return []  # too vague to decide arriving or leaving
    return _geofence(tracker, lat, lon, t)


def place_at(lat: float, lon: float) -> dict | None:
    best = None
    for p in places():
        d = meters(lat, lon, p["lat"], p["lon"])
        if d <= p["radius"] and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def _geofence(tracker: str, lat: float, lon: float, t: float) -> list[tuple[str, str, str]]:
    from . import organizer
    here = place_at(lat, lon)
    fired = []
    with _db() as c:
        _ensure_schema(c)
        st = c.execute("SELECT * FROM loc_state WHERE tracker = ?", (tracker,)).fetchone()
        before = st["place"] if st else None
        now_id = here["id"] if here else None
        if st and before != now_id:
            for event, pid in (("leave", before), ("arrive", now_id)):
                if pid is None:
                    continue
                for r in c.execute("SELECT * FROM loc_reminders WHERE done = 0 AND place = ? AND event = ?", (pid, event)).fetchall():
                    pname = next((p["name"] for p in places() if p["id"] == pid), "the place")
                    fired.append(("reminder", "Reminder", f"{r['text']} ({'you arrived at' if event == 'arrive' else 'you left'} {pname})"))
                    c.execute("UPDATE loc_reminders SET done = 1 WHERE id = ?", (r["id"],))
        c.execute("INSERT INTO loc_state (tracker, place, since, last_t) VALUES (?,?,?,?) ON CONFLICT(tracker) DO UPDATE SET "
                  "place=excluded.place, since=CASE WHEN loc_state.place IS excluded.place THEN loc_state.since ELSE excluded.since END, last_t=excluded.last_t",
                  (tracker, now_id, t, t))
        for kind, title, text in fired:
            c.execute("INSERT INTO outbox (at, kind, title, text) VALUES (?, ?, ?, ?)", (organizer._iso(organizer.now()), kind, title, text))
    return fired


# ---------------------------------------------------------------- places

_places_cache: list[dict] | None = None


def places() -> list[dict]:
    global _places_cache
    if _places_cache is None:
        with _db() as c:
            _ensure_schema(c)
            _places_cache = [dict(r) for r in c.execute("SELECT * FROM loc_places ORDER BY kind = 'home' DESC, kind = 'work' DESC, name")]
    return [dict(p) for p in _places_cache]


def _forget_places() -> None:
    global _places_cache
    _places_cache = None


def add_place(name: str, lat: float, lon: float, kind: str = "other", radius: float = 150) -> int:
    kind = kind if kind in ("home", "work", "other") else "other"
    _forget_places()
    with _db() as c:
        _ensure_schema(c)
        if kind in ("home", "work"):
            c.execute("UPDATE loc_places SET kind = 'other' WHERE kind = ?", (kind,))
        return c.execute("INSERT INTO loc_places (name, kind, lat, lon, radius, created) VALUES (?,?,?,?,?,?)",
                         (name.strip() or kind.title(), kind, lat, lon, max(30, min(float(radius), 2000)), time.strftime("%Y-%m-%d %H:%M"))).lastrowid


def update_place(place_id: int, name: str | None = None, kind: str | None = None, radius: float | None = None) -> None:
    _forget_places()
    with _db() as c:
        _ensure_schema(c)
        if kind in ("home", "work"):
            c.execute("UPDATE loc_places SET kind = 'other' WHERE kind = ? AND id != ?", (kind, place_id))
        for col, v in (("name", name), ("kind", kind), ("radius", radius)):
            if v is not None:
                c.execute(f"UPDATE loc_places SET {col} = ? WHERE id = ?", (v, place_id))


def delete_place(place_id: int) -> None:
    _forget_places()
    with _db() as c:
        _ensure_schema(c)
        c.execute("DELETE FROM loc_places WHERE id = ?", (place_id,))
        c.execute("DELETE FROM loc_reminders WHERE place = ?", (place_id,))


def find_place(name: str) -> dict | None:
    q = name.strip().lower().removeprefix("the ").removeprefix("my ")
    q = {"office": "work", "job": "work", "workplace": "work", "house": "home", "place": "home"}.get(q, q)
    ps = places()
    return next((p for p in ps if p["kind"] == q), None) or next((p for p in ps if p["name"].lower() == q), None) or \
        next((p for p in ps if q in p["name"].lower()), None)


def latest(tracker: str | None = None) -> dict | None:
    with _db() as c:
        _ensure_schema(c)
        q = "SELECT * FROM loc_points" + (" WHERE tracker = ?" if tracker else "") + " ORDER BY t DESC LIMIT 1"
        r = c.execute(q, (tracker,) if tracker else ()).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- understanding the points

def points(start: float, end: float, tracker: str | None = None) -> list[dict]:
    with _db() as c:
        _ensure_schema(c)
        q = "SELECT * FROM loc_points WHERE t >= ? AND t < ?" + (" AND tracker = ?" if tracker else "") + " ORDER BY t"
        return [dict(r) for r in c.execute(q, (start, end, tracker) if tracker else (start, end))]


def _usable(pts: list[dict]) -> list[dict]:
    return [p for p in pts if p.get("acc") is None or p["acc"] <= 150]


def stays(pts: list[dict]) -> list[dict]:
    """Stops: consecutive points within 120 m of each other for at least 8 minutes."""
    pts = _usable(pts)
    out, i = [], 0
    while i < len(pts):
        j = i
        lat, lon = pts[i]["lat"], pts[i]["lon"]
        while j + 1 < len(pts) and meters(lat, lon, pts[j + 1]["lat"], pts[j + 1]["lon"]) <= STAY_METERS:
            j += 1
            n = j - i + 1
            lat += (pts[j]["lat"] - lat) / n
            lon += (pts[j]["lon"] - lon) / n
        if pts[j]["t"] - pts[i]["t"] >= STAY_MINUTES * 60:
            pl = place_at(lat, lon)
            out.append({"start": pts[i]["t"], "end": pts[j]["t"], "lat": lat, "lon": lon, "place": pl["name"] if pl else None,
                        "place_id": pl["id"] if pl else None, "kind": pl["kind"] if pl else None})
            i = j + 1
        else:
            i += 1
    return out


def distance(pts: list[dict]) -> float:
    pts = _usable(pts)
    total, last = 0.0, None
    for p in pts:
        if last is not None:
            d = meters(last["lat"], last["lon"], p["lat"], p["lon"])
            if d >= 30:  # ignore GPS jitter while standing still
                total += d
                last = p
        else:
            last = p
    return total


def day_bounds(d: date) -> tuple[float, float]:
    start = datetime(d.year, d.month, d.day).timestamp()
    return start, start + 86400


def day_summary(d: date, tracker: str | None = None) -> dict:
    s, e = day_bounds(d)
    pts = points(s, e, tracker)
    st = stays(pts)
    by_kind = {"home": 0.0, "work": 0.0}
    named, unnamed = {}, 0
    for x in st:
        dur = x["end"] - x["start"]
        if x["kind"] in by_kind:
            by_kind[x["kind"]] += dur
        if x["place"]:
            named[x["place"]] = named.get(x["place"], 0) + dur
        else:
            unnamed += 1
    return {"date": d.isoformat(), "points": len(pts), "km": round(distance(pts) / 1000, 1), "stays": st,
            "home_h": round(by_kind["home"] / 3600, 1), "work_h": round(by_kind["work"] / 3600, 1),
            "places": {k: round(v / 3600, 1) for k, v in sorted(named.items(), key=lambda kv: -kv[1])}, "unnamed_stops": unnamed,
            "track": [[round(p["lat"], 6), round(p["lon"], 6)] for p in _usable(pts)]}


def describe_day(d: date) -> str:
    s = day_summary(d)
    if not s["points"]:
        return f"No location data for {d:%a %d %b}."
    label = "Today" if d == date.today() else "Yesterday" if d == date.today() - timedelta(days=1) else f"{d:%a %d %b}"
    parts = [f"{label}: {s['km']} km travelled, {len(s['stays'])} stop(s)"]
    if s["home_h"] or s["work_h"]:
        parts.append(f"{s['home_h']} h at home" + (f", {s['work_h']} h at work" if s["work_h"] else ""))
    others = [f"{k} ({v} h)" for k, v in s["places"].items() if not any(p["name"] == k and p["kind"] in ("home", "work") for p in places())]
    if others:
        parts.append("also at " + ", ".join(others[:5]))
    if s["unnamed_stops"]:
        parts.append(f"{s['unnamed_stops']} stop(s) at places you haven't named")
    return "; ".join(parts) + "."


def where_was_i(when: datetime) -> str:
    t = when.timestamp()
    pts = points(t - 14 * 3600, t + 14 * 3600)  # the whole visit, not just the hours around it
    if not pts:
        return f"I have no location data around {when:%a %d %b %H:%M}."
    for st in stays(pts):
        if st["start"] - 300 <= t <= st["end"] + 300:
            where = st["place"] or f"an unnamed place ({st['lat']:.4f}, {st['lon']:.4f})"
            return f"At {when:%a %H:%M} you were at {where} (from {datetime.fromtimestamp(st['start']):%H:%M} to {datetime.fromtimestamp(st['end']):%H:%M})."
    near = min(pts, key=lambda p: abs(p["t"] - t))
    pl = place_at(near["lat"], near["lon"])
    return (f"At {when:%a %H:%M} you were on the move" + (f", near {pl['name']}" if pl else f", around ({near['lat']:.4f}, {near['lon']:.4f})") +
            f" (closest point {abs(near['t'] - t) / 60:.0f} min away).")


def week_summary(end: date | None = None) -> str:
    end = end or date.today()
    days = [day_summary(end - timedelta(days=i)) for i in range(7)]
    km = sum(d["km"] for d in days)
    if not any(d["points"] for d in days):
        return "No location data in the last 7 days."
    work = sum(d["work_h"] for d in days)
    places_ = {}
    for d in days:
        for k, v in d["places"].items():
            places_[k] = places_.get(k, 0) + v
    top = ", ".join(f"{k} {v:.0f} h" for k, v in sorted(places_.items(), key=lambda kv: -kv[1])[:5])
    return f"Last 7 days: {km:.0f} km travelled" + (f", {work:.0f} h at work" if work else "") + (f". Most time at: {top}." if top else ".")


def suggest_places(days: int = 14) -> list[dict]:
    """Likely home (where you are at night) and work (weekday daytime), if they aren't set yet."""
    have = {p["kind"] for p in places()}
    end = time.time()
    pts = _usable(points(end - days * 86400, end))
    out = []
    for kind, test in (("home", lambda dt: dt.hour < 5), ("work", lambda dt: dt.weekday() < 5 and 10 <= dt.hour < 16)):
        if kind in have:
            continue
        cells = {}
        for p in pts:
            dt = datetime.fromtimestamp(p["t"])
            if test(dt):
                key = (round(p["lat"], 3), round(p["lon"], 3))  # about 100 m cells
                cells.setdefault(key, []).append(p)
        if not cells:
            continue
        key, ps = max(cells.items(), key=lambda kv: len(kv[1]))
        if len(ps) < 6:
            continue
        lat = sum(p["lat"] for p in ps) / len(ps)
        lon = sum(p["lon"] for p in ps) / len(ps)
        if kind == "work" and any(meters(lat, lon, s["lat"], s["lon"]) < 300 for s in out):
            continue  # work at home: don't suggest the same place twice
        out.append({"kind": kind, "lat": round(lat, 6), "lon": round(lon, 6), "samples": len(ps)})
    return out


def add_location_reminder(text: str, place_name: str, event: str = "arrive") -> str:
    p = find_place(place_name)
    if not p:
        raise ValueError(f"I don't know a place called '{place_name}'. Name it first (Places page, or 'call this place ...')")
    with _db() as c:
        _ensure_schema(c)
        c.execute("INSERT INTO loc_reminders (text, place, event, created) VALUES (?,?,?,?)", (text.strip(), p["id"], event, time.strftime("%Y-%m-%d %H:%M")))
    return f"OK, I'll remind you to {text.strip()} when you {'get to' if event == 'arrive' else 'leave'} {p['name']}."


def location_reminders() -> list[dict]:
    with _db() as c:
        _ensure_schema(c)
        return [dict(r) for r in c.execute("SELECT r.*, p.name AS place_name FROM loc_reminders r JOIN loc_places p ON p.id = r.place WHERE r.done = 0")]


def prune(keep_days: int) -> int:
    if not keep_days or keep_days <= 0:
        return 0
    with _db() as c:
        _ensure_schema(c)
        return c.execute("DELETE FROM loc_points WHERE t < ?", (time.time() - keep_days * 86400,)).rowcount


def delete_history(since_days: int | None = None) -> int:
    with _db() as c:
        _ensure_schema(c)
        if since_days:
            return c.execute("DELETE FROM loc_points WHERE t >= ?", (time.time() - since_days * 86400,)).rowcount
        return c.execute("DELETE FROM loc_points").rowcount
