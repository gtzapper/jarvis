"""Download clients. Every client returns items in the same shape so commands and monitoring don't care
which one they're talking to:
    {"id", "name", "state", "progress" (0-100), "down" (bytes/s), "eta" (text), "size" (bytes), "error" (text|None)}
"""

import time

import requests

from ..config import env
from .clients import Service

QBIT_STATES = {
    "downloading": "downloading", "forcedDL": "downloading", "metaDL": "fetching metadata",
    "forcedMetaDL": "fetching metadata", "stalledDL": "stalled", "queuedDL": "queued",
    "pausedDL": "paused", "stoppedDL": "paused", "checkingDL": "checking", "allocating": "allocating",
    "uploading": "seeding", "forcedUP": "seeding", "stalledUP": "seeding", "queuedUP": "seeding",
    "pausedUP": "done", "stoppedUP": "done", "checkingUP": "checking", "checkingResumeData": "checking",
    "moving": "moving", "error": "error", "missingFiles": "missing files", "unknown": "unknown",
}


def fmt_eta(seconds) -> str:
    if not seconds or seconds < 0 or seconds >= 8640000:  # qBittorrent uses 8640000 for "never"
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m" if h else f"{rem // 60}m {rem % 60}s"


def fmt_speed(bps: float) -> str:
    return f"{bps / 1e6:.1f} MB/s" if bps >= 1e6 else f"{bps / 1e3:.0f} KB/s"


class Downloader(Service):
    kind = "torrent"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = cfg.get("name", self.name)

    # Every client implements these.
    def version(self) -> str: ...
    def stats(self) -> dict: ...                     # {"down", "up", "notes": [str], "problems": [(key, msg)]}
    def items(self) -> list[dict]: ...               # unfinished or errored items only
    def pause(self, ids: list[str] | None = None) -> None: ...   # None = everything
    def resume(self, ids: list[str] | None = None) -> None: ...
    def set_slow(self, on: bool) -> None: ...


# ---------------------------------------------------------------- qBittorrent

class QBittorrentClient(Downloader):
    name = "qBittorrent"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.username = cfg.get("username", "admin")
        self.password = env(cfg.get("password_env"))  # leave unset if you whitelist Jarvis's IP in qBittorrent
        self.s.headers["Referer"] = self.url          # qBittorrent's CSRF check wants this
        self._logged_in = False

    def _login(self) -> None:
        r = self.s.post(self.url + "/api/v2/auth/login", data={"username": self.username, "password": self.password}, timeout=15)
        r.raise_for_status()
        if self._text(r) != "Ok.":
            raise ValueError("qBittorrent login failed. Check the username and password")
        self._logged_in = True

    @staticmethod
    def _text(r: requests.Response) -> str:
        return r.content.decode("utf-8", errors="replace").strip()  # never let requests guess the encoding

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        if self.password and not self._logged_in:
            self._login()
        r = self.s.request(method, self.url + "/api/v2" + path, timeout=20, **kw)
        if r.status_code == 403 and self.password:  # session expired
            self._login()
            r = self.s.request(method, self.url + "/api/v2" + path, timeout=20, **kw)
        r.raise_for_status()
        return r

    def version(self) -> str:
        return self._text(self._call("GET", "/app/version"))

    def stats(self) -> dict:
        info = self._call("GET", "/transfer/info").json()
        slow = self._text(self._call("GET", "/transfer/speedLimitsMode")) == "1"
        notes, problems = [], []
        conn = info.get("connection_status", "")
        if conn == "disconnected":
            problems.append(("conn", f"{self.name} has no network connection"))
        elif conn == "firewalled":
            notes.append("Port looks firewalled (fewer peers can connect)")
        if slow:
            notes.append("Slow mode (alternative speed limits) is ON")
        return {"down": info.get("dl_info_speed", 0), "up": info.get("up_info_speed", 0), "notes": notes, "problems": problems}

    def items(self) -> list[dict]:
        out = []
        for t in self._call("GET", "/torrents/info").json():
            state = QBIT_STATES.get(t.get("state", ""), t.get("state", "?"))
            error = state if state in ("error", "missing files") else None
            if t.get("progress", 0) >= 1 and not error:
                continue
            out.append({"id": t["hash"], "name": t.get("name", "?"), "state": state,
                        "progress": t.get("progress", 0) * 100, "down": t.get("dlspeed", 0),
                        "eta": fmt_eta(t.get("eta")), "size": t.get("size", 0), "error": error})
        return out

    def _start_stop(self, new: str, old: str, ids) -> None:
        data = {"hashes": "|".join(ids) if ids else "all"}
        try:
            self._call("POST", f"/torrents/{new}", data=data)   # qBittorrent 5.x
        except requests.HTTPError as e:
            if e.response.status_code != 404:
                raise
            self._call("POST", f"/torrents/{old}", data=data)   # qBittorrent 4.x

    def pause(self, ids=None) -> None:
        self._start_stop("stop", "pause", ids)

    def resume(self, ids=None) -> None:
        self._start_stop("start", "resume", ids)

    def set_slow(self, on: bool) -> None:
        current = self._text(self._call("GET", "/transfer/speedLimitsMode")) == "1"
        if current != on:
            self._call("POST", "/transfer/toggleSpeedLimitsMode")


# ---------------------------------------------------------------- Deluge

class DelugeClient(Downloader):
    name = "Deluge"
    FIELDS = ["name", "state", "progress", "download_payload_rate", "eta", "total_size", "message"]

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.password = env(cfg.get("password_env")) or "deluge"
        self.slow_kbps = cfg.get("slow_limit_kbps", 1024)
        self._id = 0
        self._ready = False

    def _raw(self, method: str, *params):
        self._id += 1
        r = self.s.post(self.url + "/json", json={"method": method, "params": list(params), "id": self._id}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Deluge {method}: {data['error'].get('message', data['error'])}")
        return data.get("result")

    def _ensure(self) -> None:
        if not self._raw("auth.check_session"):
            if not self._raw("auth.login", self.password):
                raise ValueError("Deluge login failed. Check the web UI password")
        if not self._raw("web.connected"):
            hosts = self._raw("web.get_hosts") or []
            if not hosts:
                raise RuntimeError("Deluge web UI has no daemon configured")
            self._raw("web.connect", hosts[0][0])
        self._ready = True

    def _rpc(self, method: str, *params):
        if not self._ready:
            self._ensure()
        try:
            return self._raw(method, *params)
        except RuntimeError as e:
            if "Not authenticated" not in str(e):
                raise
            self._ensure()
            return self._raw(method, *params)

    def _rpc_either(self, new: str, old: str, *params):
        """Deluge 2.x renamed several methods; try the new name, fall back to 1.3's."""
        try:
            return self._rpc(new, *params)
        except RuntimeError as e:
            if "Unknown method" not in str(e) and "not found" not in str(e).lower():
                raise
            return self._rpc(old, *params)

    def _ui(self) -> dict:
        return self._rpc("web.update_ui", self.FIELDS, {}) or {}

    def version(self) -> str:
        try:
            return str(self._rpc("daemon.get_version"))
        except RuntimeError:
            return "?"

    def stats(self) -> dict:
        ui = self._ui()
        st = ui.get("stats") or {}
        problems = [] if ui.get("connected", True) else [("conn", "Deluge web UI isn't connected to its daemon")]
        return {"down": st.get("download_rate", 0), "up": st.get("upload_rate", 0), "notes": [], "problems": problems}

    def items(self) -> list[dict]:
        out = []
        for tid, t in (self._ui().get("torrents") or {}).items():
            state = str(t.get("state", "?")).lower()
            error = (t.get("message") or "error") if state == "error" else None
            if t.get("progress", 0) >= 100 and not error:
                continue
            out.append({"id": tid, "name": t.get("name", "?"), "state": state, "progress": t.get("progress", 0),
                        "down": t.get("download_payload_rate", 0), "eta": fmt_eta(t.get("eta")),
                        "size": t.get("total_size", 0), "error": error})
        return out

    def _all_ids(self) -> list[str]:
        return list((self._ui().get("torrents") or {}).keys())

    def pause(self, ids=None) -> None:
        self._rpc_either("core.pause_torrents", "core.pause_torrent", ids or self._all_ids())

    def resume(self, ids=None) -> None:
        self._rpc_either("core.resume_torrents", "core.resume_torrent", ids or self._all_ids())

    def set_slow(self, on: bool) -> None:
        self._rpc("core.set_config", {"max_download_speed": float(self.slow_kbps) if on else -1.0})


# ---------------------------------------------------------------- SABnzbd

class SabnzbdClient(Downloader):
    name = "SABnzbd"
    kind = "usenet"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.api_key = env(cfg.get("api_key_env"))
        if not self.api_key:
            raise ValueError(f"{self.name}: set {cfg.get('api_key_env')} in .env")
        self.slow_percent = cfg.get("slow_limit_percent", 25)
        self.min_free_gb = cfg.get("min_free_gb", 20)

    def _api(self, mode: str, **params):
        r = self.s.get(self.url + "/api", params={"mode": mode, "output": "json", "apikey": self.api_key, **params}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") is False:
            raise ValueError(f"SABnzbd: {data.get('error', 'request refused')}")
        return data

    def version(self) -> str:
        return self._api("version").get("version", "?")

    def stats(self) -> dict:
        q = self._api("queue", limit=1)["queue"]
        notes, problems = [], []
        if q.get("paused"):
            notes.append("Queue is PAUSED")
        try:
            free = float(q.get("diskspace1") or 0)
            notes.append(f"{free:.0f} GB free in the download folder")
            if free < self.min_free_gb:
                problems.append(("disk", f"SABnzbd download folder has only {free:.0f} GB free"))
        except ValueError:
            pass
        day_ago = time.time() - 86400
        for h in self._api("history", limit=30).get("history", {}).get("slots", []):
            if h.get("status") == "Failed" and (h.get("completed") or 0) > day_ago:
                problems.append((f"failed:{h.get('nzo_id')}", f"SABnzbd download failed: {h.get('name', '?')}. {h.get('fail_message', '')}".strip()))
        return {"down": float(q.get("kbpersec") or 0) * 1024, "up": 0, "notes": notes, "problems": problems}

    def items(self) -> list[dict]:
        out = []
        for s in self._api("queue", limit=100)["queue"].get("slots", []):
            mb = float(s.get("mb") or 0)
            out.append({"id": s["nzo_id"], "name": s.get("filename", "?"), "state": str(s.get("status", "?")).lower(),
                        "progress": float(s.get("percentage") or 0), "down": 0,
                        "eta": s.get("timeleft", "") if s.get("timeleft") not in ("0:00:00", None) else "",
                        "size": mb * 1024 * 1024, "error": None})
        return out

    def pause(self, ids=None) -> None:
        if not ids:
            self._api("pause")
        for i in ids or []:
            self._api("queue", name="pause", value=i)

    def resume(self, ids=None) -> None:
        if not ids:
            self._api("resume")
        for i in ids or []:
            self._api("queue", name="resume", value=i)

    def set_slow(self, on: bool) -> None:
        self._api("config", name="speedlimit", value=self.slow_percent if on else 100)


DOWNLOADERS = {"qbittorrent": QBittorrentClient, "deluge": DelugeClient, "sabnzbd": SabnzbdClient}
