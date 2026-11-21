"""Thin API clients for the media stack. Each method is one real API call, kept small so the
command layer (and later the AI brain) can use them as building blocks."""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from ..config import env


class Service:
    name = "service"
    prefix = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = cfg["url"].rstrip("/")
        self.s = requests.Session()
        self.s.verify = cfg.get("verify_ssl", True)

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, self.url + self.prefix + path, timeout=kw.pop("timeout", 20), **kw)
        r.raise_for_status()
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def get(self, path: str, **params):
        return self._req("GET", path, params=params)

    def post(self, path: str, body=None, **params):
        return self._req("POST", path, json=body, params=params)

    def delete(self, path: str, **params):
        return self._req("DELETE", path, params=params)

    def _key(self, cfg_field: str, header: str) -> None:
        key = env(self.cfg.get(cfg_field))
        if not key:
            raise ValueError(f"{self.name}: set {self.cfg.get(cfg_field)} in .env")
        self.s.headers[header] = key


# ---------------------------------------------------------------- *arr apps

class ArrClient(Service):
    prefix = "/api/v3"
    kind = ""            # what the user calls the thing it manages: movie / show / artist
    lookup_path = ""
    add_path = ""
    missing_cmd = ""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._key("api_key_env", "X-Api-Key")

    def version(self) -> str:
        return self.get("/system/status").get("version", "?")

    def health(self) -> list[dict]:
        return self.get("/health") or []

    def queue(self) -> list[dict]:
        return (self.get("/queue", pageSize=100) or {}).get("records", [])

    def remove_from_queue(self, queue_id: int, blocklist: bool = True) -> None:
        self.delete(f"/queue/{queue_id}", removeFromClient="true", blocklist=str(blocklist).lower())

    def search_missing(self) -> None:
        self.post("/command", {"name": self.missing_cmd})

    def lookup(self, term: str) -> list[dict]:
        return self.get(self.lookup_path, term=term) or []

    def label(self, item: dict) -> str:
        return f"{item.get('title', '?')} ({item.get('year') or '?'})"

    def in_library(self, item: dict) -> bool:
        return bool(item.get("id"))

    def _quality_profile(self) -> int:
        profiles = self.get("/qualityprofile")
        want = (self.cfg.get("quality_profile") or "").lower()
        match = next((p for p in profiles if p["name"].lower() == want), profiles[0])
        return match["id"]

    def _root_folder(self) -> str:
        return self.cfg.get("root_folder") or self.get("/rootfolder")[0]["path"]

    def add(self, item: dict) -> None:
        self.post(self.add_path, self.build_add(item))

    def build_add(self, item: dict) -> dict:
        raise NotImplementedError

    def upcoming(self, days: int) -> list[tuple[str, str]]:
        raise NotImplementedError


def _window(days: int) -> dict:
    now = datetime.now(timezone.utc)
    return {"start": now.strftime("%Y-%m-%d"), "end": (now + timedelta(days=days)).strftime("%Y-%m-%d")}


class SonarrClient(ArrClient):
    name, kind = "Sonarr", "show"
    lookup_path, add_path, missing_cmd = "/series/lookup", "/series", "MissingEpisodeSearch"

    def build_add(self, item: dict) -> dict:
        body = dict(item)
        body.update(qualityProfileId=self._quality_profile(), rootFolderPath=self._root_folder(),
                    monitored=True, seasonFolder=True,
                    addOptions={"monitor": "all", "searchForMissingEpisodes": True})
        try:  # Sonarr v3 requires a language profile; v4 dropped them
            langs = self.get("/languageprofile")
            if langs:
                body["languageProfileId"] = langs[0]["id"]
        except requests.RequestException:
            pass
        return body

    def upcoming(self, days: int) -> list[tuple[str, str]]:
        out = []
        for e in self.get("/calendar", includeSeries="true", **_window(days)) or []:
            show = (e.get("series") or {}).get("title", "?")
            out.append((e.get("airDateUtc", "")[:10],
                        f"{show} S{e.get('seasonNumber', 0):02}E{e.get('episodeNumber', 0):02} {e.get('title', '')}".strip()))
        return out


class RadarrClient(ArrClient):
    name, kind = "Radarr", "movie"
    lookup_path, add_path, missing_cmd = "/movie/lookup", "/movie", "MissingMoviesSearch"

    def build_add(self, item: dict) -> dict:
        body = dict(item)
        body.update(qualityProfileId=self._quality_profile(), rootFolderPath=self._root_folder(),
                    monitored=True, minimumAvailability=self.cfg.get("minimum_availability", "released"),
                    addOptions={"searchForMovie": True})
        return body

    def upcoming(self, days: int) -> list[tuple[str, str]]:
        w = _window(days)
        out = []
        for m in self.get("/calendar", **w) or []:
            for field, what in (("digitalRelease", "digital"), ("physicalRelease", "disc"), ("inCinemas", "cinemas")):
                d = (m.get(field) or "")[:10]
                if w["start"] <= d <= w["end"]:
                    out.append((d, f"{self.label(m)} ({what})"))
                    break
        return out


class LidarrClient(ArrClient):
    prefix = "/api/v1"
    name, kind = "Lidarr", "artist"
    lookup_path, add_path, missing_cmd = "/artist/lookup", "/artist", "MissingAlbumSearch"

    def label(self, item: dict) -> str:
        extra = item.get("disambiguation") or item.get("artistType") or ""
        return item.get("artistName", "?") + (f" ({extra})" if extra else "")

    def build_add(self, item: dict) -> dict:
        body = dict(item)
        body.update(qualityProfileId=self._quality_profile(), rootFolderPath=self._root_folder(),
                    metadataProfileId=self.get("/metadataprofile")[0]["id"], monitored=True,
                    addOptions={"monitor": "all", "searchForMissingAlbums": True})
        return body

    def upcoming(self, days: int) -> list[tuple[str, str]]:
        return [((a.get("releaseDate") or "")[:10],
                 f"{(a.get('artist') or {}).get('artistName', '?')} - {a.get('title', '?')}")
                for a in self.get("/calendar", includeArtist="true", **_window(days)) or []]


class ProwlarrClient(ArrClient):
    prefix = "/api/v1"
    name = "Prowlarr"

    def indexers(self) -> list[dict]:
        return self.get("/indexer") or []

    def indexer_status(self) -> list[dict]:
        return self.get("/indexerstatus") or []

    def test_all(self) -> list[dict]:
        return self.post("/indexer/testall") or []


ARR_CLIENTS = {"sonarr": SonarrClient, "radarr": RadarrClient, "lidarr": LidarrClient}


# ---------------------------------------------------------------- Overseerr / Jellyseerr

class OverseerrClient(Service):
    name = "Overseerr"
    prefix = "/api/v1"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._key("api_key_env", "X-Api-Key")
        self._titles: dict[tuple, str] = {}

    def version(self) -> str:
        return self.get("/status").get("version", "?")

    def counts(self) -> dict:
        return self.get("/request/count") or {}

    def pending(self) -> list[dict]:
        return (self.get("/request", filter="pending", take=50, sort="added") or {}).get("results", [])

    def title_of(self, req: dict) -> str:
        """Request objects carry only a TMDB id, so look the name up (and cache it)."""
        media = req.get("media") or {}
        key = (req.get("type") or media.get("mediaType"), media.get("tmdbId"))
        if key not in self._titles:
            try:
                info = self.get(f"/{key[0]}/{key[1]}")
                year = (info.get("releaseDate") or info.get("firstAirDate") or "")[:4]
                self._titles[key] = f"{info.get('title') or info.get('name')} ({year or '?'})"
            except requests.RequestException:
                self._titles[key] = f"TMDB {key[1]}"
        return self._titles[key]

    def approve(self, request_id: int) -> None:
        self.post(f"/request/{request_id}/approve")

    def decline(self, request_id: int) -> None:
        self.post(f"/request/{request_id}/decline")


# ---------------------------------------------------------------- Plex

class PlexClient(Service):
    name = "Plex"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._key("token_env", "X-Plex-Token")
        self.s.headers["Accept"] = "application/json"

    def version(self) -> str:
        return self.get("/identity")["MediaContainer"].get("version", "?")

    def sessions(self) -> list[dict]:
        return self.get("/status/sessions")["MediaContainer"].get("Metadata", [])

    def libraries(self) -> list[dict]:
        return self.get("/library/sections")["MediaContainer"].get("Directory", [])

    def refresh(self, section_key: str) -> None:
        self.get(f"/library/sections/{quote(str(section_key))}/refresh")

    def recently_added(self, count: int = 10) -> list[dict]:
        data = self.get("/library/recentlyAdded", **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": count})
        return data["MediaContainer"].get("Metadata", [])[:count]

    @staticmethod
    def label(m: dict) -> str:
        t = m.get("type")
        if t == "episode":
            return f"{m.get('grandparentTitle')} S{m.get('parentIndex', 0):02}E{m.get('index', 0):02}"
        if t == "track":
            return f"{m.get('grandparentTitle')} - {m.get('title')}"
        if t == "season":
            return f"{m.get('parentTitle')} {m.get('title')}"
        return f"{m.get('title')} ({m.get('year', '?')})"

    @classmethod
    def describe_session(cls, m: dict) -> str:
        user = (m.get("User") or {}).get("title", "someone")
        player = m.get("Player") or {}
        progress = int(m.get("viewOffset", 0) / m["duration"] * 100) if m.get("duration") else 0
        ts = m.get("TranscodeSession")
        mode = f"transcoding ({ts.get('videoDecision', 'video')})" if ts else "direct play"
        state = player.get("state", "playing")
        return f"{user} is {'paused on' if state == 'paused' else 'watching'} {cls.label(m)} on {player.get('title', '?')}, {progress}%, {mode}"


# ---------------------------------------------------------------- the whole stack

class MediaStack:
    """Every configured media service, built once and shared by monitoring and commands."""

    def __init__(self, cfg: dict):
        from .downloaders import DOWNLOADERS
        m = cfg.get("media") or {}
        self.errors: list[tuple[str, str]] = []  # (app, problem) for half-filled config sections

        def build(key, cls, entry):
            try:
                return cls(entry)
            except (ValueError, KeyError) as e:  # e.g. API key not in .env yet: skip just this app
                self.errors.append((key, str(e) if isinstance(e, ValueError) else f"missing '{e.args[0]}' in config.yaml"))
                return None

        self.arrs = {k: c for k, cls in ARR_CLIENTS.items() if m.get(k) and (c := build(k, cls, m[k]))}
        self.prowlarr = build("prowlarr", ProwlarrClient, m["prowlarr"]) if m.get("prowlarr") else None
        self.overseerr = build("overseerr", OverseerrClient, m["overseerr"]) if m.get("overseerr") else None
        self.plex = build("plex", PlexClient, m["plex"]) if m.get("plex") else None
        self.downloaders = []
        for key, cls in DOWNLOADERS.items():
            entries = m.get(key) or []
            for entry in entries if isinstance(entries, list) else [entries]:  # a list allows several instances
                if (c := build(key, cls, entry)):
                    self.downloaders.append(c)

    def __bool__(self) -> bool:
        return bool(self.arrs or self.prowlarr or self.overseerr or self.plex or self.downloaders or self.errors)

    def for_kind(self, kind: str) -> ArrClient | None:
        return next((a for a in self.arrs.values() if a.kind == kind), None)
