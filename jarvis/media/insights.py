"""Phase 5, media: what to watch next, what could go to free up space, and a request queue for the family.

Everything here reads Plex and the *arr apps. Nothing is deleted or added without the person's OK:
delete goes through the 'destructive' approval (always asks), adding a requested title through 'change'."""

import re
import time
from datetime import datetime, timedelta

from .. import organizer

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_requests (id INTEGER PRIMARY KEY, who TEXT, kind TEXT, title TEXT, year INTEGER, ext_id TEXT,
    status TEXT DEFAULT 'pending', arr_id INTEGER, note TEXT, created TEXT, decided TEXT, ready TEXT);
CREATE TABLE IF NOT EXISTS media_keep (key TEXT PRIMARY KEY, title TEXT, added TEXT);
"""


def _db():
    c = organizer.db()
    return c


def _ensure(c):
    organizer.ensure(c, SCHEMA)


def _ts(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ago(ts: float) -> str:
    days = int((time.time() - ts) / 86400)
    if days < 1:
        return "today"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def gb(n) -> str:
    n = float(n or 0) / 1e9
    return f"{n:,.0f} GB" if n >= 10 else f"{n:.1f} GB"


def _guids(item: dict) -> dict:
    """{'tmdb': '603', 'tvdb': '81189', 'imdb': 'tt...'} from Plex's Guid list."""
    out = {}
    for g in item.get("Guid") or []:
        m = re.match(r"(\w+)://(.+)", str(g.get("id", "")))
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _genres(item: dict) -> list[str]:
    return [g.get("tag") for g in item.get("Genre") or [] if g.get("tag")]


# ---------------------------------------------------------------- Plex library

def library(plex, kinds=("movie", "show")) -> dict[str, list[dict]]:
    """Every movie and show in Plex, with watch state (for the account whose token Jarvis uses)."""
    out = {"movie": [], "show": []}
    for sec in plex.libraries():
        t = sec.get("type")
        if t not in kinds:
            continue
        data = plex.get(f"/library/sections/{sec['key']}/all", includeGuids=1)
        out[t] += (data.get("MediaContainer") or {}).get("Metadata", [])
    return out


# ---------------------------------------------------------------- what to watch

def suggestions(stack, kind: str = "any", max_minutes: int | None = None, genre: str | None = None, limit: int = 6) -> dict:
    """Continue-watching first, then unwatched titles that look like what you've been watching lately."""
    plex = stack.plex
    if not plex:
        raise LookupError("Plex isn't set up, so I can't see your library.")
    lib = library(plex)
    g = (genre or "").lower().strip()

    # taste: genres of what was watched recently, newer counts more
    taste: dict[str, float] = {}
    now = time.time()
    for items, watched in ((lib["movie"], lambda x: x.get("viewCount")), (lib["show"], lambda x: x.get("viewedLeafCount"))):
        for it in items:
            if watched(it) and it.get("lastViewedAt"):
                w = 1.0 / (1 + (now - _ts(it["lastViewedAt"])) / (86400 * 60))
                for gen in _genres(it):
                    taste[gen] = taste.get(gen, 0) + w
    top = max(taste.values(), default=1)

    def score(it):
        s = sum(taste.get(x, 0) for x in _genres(it)) / top
        s += _ts(it.get("audienceRating") or it.get("rating")) / 10
        if now - _ts(it.get("addedAt")) < 86400 * 30:
            s += 0.3  # new in the library
        return s

    def fits(it, minutes):
        if g and not any(g in x.lower() for x in _genres(it)):
            return False
        return not (max_minutes and minutes and minutes > max_minutes)

    cont = []
    try:
        deck = (plex.get("/library/onDeck").get("MediaContainer") or {}).get("Metadata", [])
    except Exception:
        deck = []
    for it in deck:
        mins = round(_ts(it.get("duration")) / 60000) or None
        left = round((_ts(it.get("duration")) - _ts(it.get("viewOffset"))) / 60000) if it.get("viewOffset") else mins
        if kind in ("any", "show" if it.get("type") == "episode" else "movie") and fits(it, left):
            cont.append({"title": plex.label(it), "kind": it.get("type"), "minutes": left,
                         "why": "you're part-way through" if it.get("viewOffset") else "next episode"})

    picks = []
    on_deck_shows = {it.get("grandparentTitle") for it in deck if it.get("type") == "episode"}
    if kind in ("any", "movie"):
        for it in lib["movie"]:
            mins = round(_ts(it.get("duration")) / 60000) or None
            if not it.get("viewCount") and not it.get("viewOffset") and fits(it, mins):
                picks.append((score(it), {"title": f"{it.get('title')} ({it.get('year', '?')})", "kind": "movie", "minutes": mins,
                                          "genres": _genres(it)[:3], "rating": it.get("audienceRating") or it.get("rating")}))
    if kind in ("any", "show"):
        for it in lib["show"]:
            ep = round(_ts(it.get("duration")) / 60000) or None
            if fits(it, ep) and it.get("title") not in on_deck_shows:
                started = (it.get("viewedLeafCount") or 0) > 0
                if started and (it.get("viewedLeafCount") or 0) >= (it.get("leafCount") or 0):
                    continue  # finished
                s = score(it) + (0.5 if started else 0)
                picks.append((s, {"title": it.get("title"), "kind": "show", "minutes": ep, "genres": _genres(it)[:3],
                                  "rating": it.get("audienceRating") or it.get("rating"),
                                  "why": f"{it.get('viewedLeafCount')}/{it.get('leafCount')} episodes watched" if started else
                                  f"{it.get('childCount') or '?'} seasons, not started"}))
    picks.sort(key=lambda p: -p[0])
    taste_top = [k for k, _ in sorted(taste.items(), key=lambda x: -x[1])[:4]]
    return {"continue": cont[:4], "picks": [p for _, p in picks[:limit]], "taste": taste_top}


def describe_suggestions(s: dict) -> str:
    out = []
    if s["continue"]:
        out.append("Carry on with:")
        out += [f"  {c['title']} ({c['why']}" + (f", {c['minutes']} min left)" if c.get("minutes") else ")") for c in s["continue"]]
    if s["picks"]:
        out.append("Something new from your library" + (f" (you've been watching {', '.join(s['taste'])})" if s["taste"] else "") + ":")
        for p in s["picks"]:
            bits = [", ".join(p.get("genres") or [])]
            if p.get("minutes"):
                bits.append(f"{p['minutes']} min" + (" episodes" if p["kind"] == "show" else ""))
            if p.get("rating"):
                bits.append(f"rated {float(p['rating']):.1f}")
            if p.get("why"):
                bits.append(p["why"])
            out.append(f"  {p['title']}: " + ", ".join(b for b in bits if b))
    return "\n".join(out) or "Nothing unwatched that fits. Try without a time limit or genre."


# ---------------------------------------------------------------- cleanup suggestions

def _kept() -> set[str]:
    with _db() as c:
        _ensure(c)
        return {r["key"] for r in c.execute("SELECT key FROM media_keep")}


def keep(key: str, title: str) -> None:
    with _db() as c:
        _ensure(c)
        c.execute("INSERT OR REPLACE INTO media_keep (key, title, added) VALUES (?,?,?)", (key, title, datetime.now().strftime("%Y-%m-%dT%H:%M")))


def cleanup(stack, cfg: dict) -> dict:
    """Movies and shows that could go: watched long ago, never watched after a long time, or ended and fully watched.
    Suggestions only, biggest first."""
    opts = cfg.get("media_cleanup") or {}
    watched_days = int(opts.get("watched_days") or 180)
    unwatched_days = int(opts.get("unwatched_days") or 365)
    min_gb = float(opts.get("min_gb") or 1)
    if not stack.plex:
        raise LookupError("Cleanup suggestions need Plex (to know what's been watched).")
    lib = library(stack.plex)
    now = time.time()
    kept = _kept()
    out = []
    radarr, sonarr = stack.for_kind("movie"), stack.for_kind("show")
    if radarr:
        plex_by = {}
        for it in lib["movie"]:
            gid = _guids(it).get("tmdb")
            if gid:
                plex_by[str(gid)] = it
        for mv in radarr.get("/movie") or []:
            size = mv.get("sizeOnDisk") or (mv.get("movieFile") or {}).get("size") or 0
            key = f"movie:{mv.get('tmdbId')}"
            if size < min_gb * 1e9 or key in kept:
                continue
            p = plex_by.get(str(mv.get("tmdbId")))
            if not p:
                continue
            last, added = _ts(p.get("lastViewedAt")), _ts(p.get("addedAt"))
            why = None
            if p.get("viewCount") and last and now - last > watched_days * 86400:
                why = f"watched {_ago(last)}"
            elif not p.get("viewCount") and not p.get("viewOffset") and added and now - added > unwatched_days * 86400:
                why = f"added {_ago(added)}, never watched"
            if why:
                out.append({"key": key, "kind": "movie", "id": mv["id"], "title": f"{mv.get('title')} ({mv.get('year', '?')})", "size": size, "why": why})
    if sonarr:
        plex_by = {}
        for it in lib["show"]:
            gid = _guids(it).get("tvdb")
            if gid:
                plex_by[str(gid)] = it
        for sr in sonarr.get("/series") or []:
            size = (sr.get("statistics") or {}).get("sizeOnDisk") or sr.get("sizeOnDisk") or 0
            key = f"show:{sr.get('tvdbId')}"
            if size < min_gb * 1e9 or key in kept:
                continue
            p = plex_by.get(str(sr.get("tvdbId")))
            if not p:
                continue
            last, added = _ts(p.get("lastViewedAt")), _ts(p.get("addedAt"))
            done = (p.get("leafCount") or 0) and (p.get("viewedLeafCount") or 0) >= p["leafCount"]
            why = None
            if done and sr.get("status") == "ended" and last and now - last > watched_days * 86400:
                why = f"ended, all watched, last {_ago(last)}"
            elif not p.get("viewedLeafCount") and added and now - added > unwatched_days * 86400:
                why = f"added {_ago(added)}, never started"
            if why:
                out.append({"key": key, "kind": "show", "id": sr["id"], "title": sr.get("title"), "size": size, "why": why})
    out.sort(key=lambda x: -x["size"])
    return {"items": out, "total": sum(x["size"] for x in out), "watched_days": watched_days, "unwatched_days": unwatched_days}


def describe_cleanup(c: dict, limit: int = 15) -> str:
    if not c["items"]:
        return "Nothing stands out to clean up. Everything big has been watched recently or is still new."
    lines = [f"  {x['title']}: {gb(x['size'])}, {x['why']}" for x in c["items"][:limit]]
    more = f"\n  ...and {len(c['items']) - limit} more" if len(c["items"]) > limit else ""
    return (f"{len(c['items'])} things could go, freeing about {gb(c['total'])}:\n" + "\n".join(lines) + more +
            "\nSay 'delete movie <name>' or 'delete show <name>' (I'll ask first), or 'keep <name>' to stop suggesting it."
            "\n(Watched means watched on the Plex account Jarvis uses.)")


def find_in_library(stack, kind: str, title: str) -> dict | None:
    arr = stack.for_kind(kind)
    if not arr:
        return None
    q = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    q = re.sub(r"\s*\b(19|20)\d\d$", "", q).strip() or q
    items = arr.get("/movie" if kind == "movie" else "/series") or []
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()
    exact = [i for i in items if norm(i.get("title")) == q]
    return (exact or [i for i in items if q in norm(i.get("title"))] or [None])[0]


def delete_from_library(stack, kind: str, item: dict) -> None:
    arr = stack.for_kind(kind)
    path = f"/movie/{item['id']}" if kind == "movie" else f"/series/{item['id']}"
    arr.delete(path, deleteFiles="true", addImportExclusion="true" if kind == "movie" else "false",
               **({"addImportListExclusion": "true"} if kind == "show" else {}))


# ---------------------------------------------------------------- family requests

def add_request(stack, who: str, kind: str, query: str) -> tuple[int, str]:
    """Someone asks for a movie or show. Jarvis finds it and queues it for the owner's OK."""
    arr = stack.for_kind(kind)
    if not arr:
        raise LookupError(f"{'Radarr' if kind == 'movie' else 'Sonarr'} isn't set up, so I can't take {kind} requests.")
    year = None
    m = re.search(r"\(?\b((?:19|20)\d\d)\)?$", query.strip())
    if m:
        year, query = int(m.group(1)), query[:m.start()].strip()
    results = arr.lookup(query)
    if year:
        results = [r for r in results if r.get("year") == year] or results
    if not results:
        raise LookupError(f"I couldn't find a {kind} called {query}.")
    item = results[0]
    label = arr.label(item)
    if arr.in_library(item):
        return 0, f"{label} is already in the library."
    ext = str(item.get("tmdbId") if kind == "movie" else item.get("tvdbId"))
    with _db() as c:
        _ensure(c)
        old = c.execute("SELECT id, status FROM media_requests WHERE kind=? AND ext_id=? AND status IN ('pending','approved')", (kind, ext)).fetchone()
        if old:
            return old["id"], f"{label} is already requested ({old['status']})."
        rid = c.execute("INSERT INTO media_requests (who, kind, title, year, ext_id, created) VALUES (?,?,?,?,?,?)",
                        (who, kind, item.get("title"), item.get("year"), ext, datetime.now().strftime("%Y-%m-%dT%H:%M"))).lastrowid
    organizer.record("media", "Media request", f"{who} asked for {label}. Say 'requests' to approve or decline.")
    return rid, f"Asked for {label}. You'll hear when it's approved and ready."


def requests_list(status: str = "pending") -> list[dict]:
    with _db() as c:
        _ensure(c)
        return [dict(r) for r in c.execute("SELECT * FROM media_requests WHERE status = ? ORDER BY id", (status,))]


def recent_requests(days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    with _db() as c:
        _ensure(c)
        return [dict(r) for r in c.execute("SELECT * FROM media_requests WHERE created >= ? OR status = 'pending' ORDER BY id DESC", (since,))]


def decide(stack, rid: int, approve: bool, note: str = "") -> str:
    with _db() as c:
        _ensure(c)
        r = c.execute("SELECT * FROM media_requests WHERE id = ?", (rid,)).fetchone()
    if not r:
        raise LookupError(f"There's no request {rid}.")
    r = dict(r)
    label = f"{r['title']} ({r['year'] or '?'})"
    if r["status"] != "pending":
        return f"{label} was already {r['status']}."
    arr_id = None
    if approve:
        arr = stack.for_kind(r["kind"])
        results = arr.lookup(f"{'tmdb' if r['kind'] == 'movie' else 'tvdb'}:{r['ext_id']}") or arr.lookup(r["title"])
        item = next((x for x in results if str(x.get("tmdbId" if r["kind"] == "movie" else "tvdbId")) == r["ext_id"]), results[0] if results else None)
        if not item:
            raise LookupError(f"{arr.name} can't find {label} any more.")
        if arr.in_library(item):
            arr_id = item.get("id")
        else:
            added = arr.add(item) or {}
            arr_id = added.get("id")
    with _db() as c:
        _ensure(c)
        c.execute("UPDATE media_requests SET status=?, arr_id=?, note=?, decided=? WHERE id=?",
                  ("approved" if approve else "declined", arr_id, note, datetime.now().strftime("%Y-%m-%dT%H:%M"), rid))
    return f"{'Approved' if approve else 'Declined'} {label} for {r['who']}." + (" It's downloading now." if approve else "")


def check_ready(stack) -> list[tuple[str, str]]:
    """Approved requests whose files have arrived: tell the person once."""
    out = []
    for r in requests_list("approved"):
        arr = stack.for_kind(r["kind"])
        if not arr or not r["arr_id"]:
            continue
        try:
            item = arr.get(f"/movie/{r['arr_id']}" if r["kind"] == "movie" else f"/series/{r['arr_id']}") or {}
        except Exception:
            continue
        ready = item.get("hasFile") if r["kind"] == "movie" else ((item.get("statistics") or {}).get("episodeFileCount") or 0) > 0
        if ready:
            with _db() as c:
                _ensure(c)
                c.execute("UPDATE media_requests SET status='ready', ready=? WHERE id=?", (datetime.now().strftime("%Y-%m-%dT%H:%M"), r["id"]))
            text = f"{r['title']} is ready to watch on Plex (asked for by {r['who']})."
            organizer.record("media", "Ready to watch", text)
            out.append(("Ready to watch", text))
    return out
