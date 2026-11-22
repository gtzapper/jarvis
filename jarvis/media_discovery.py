"""Find media apps automatically: Docker containers on Unraid and on VMs/LXCs Jarvis can log into, and apps installed
directly in Proxmox guests. Every find is confirmed by asking the app to identify itself, and where Jarvis can log in,
the API key is read from the app's own config file so you don't have to copy it.

Keys never leave the server: scan results sent to the browser only say whether a key was found."""

import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from . import housekeeping as hk

# app -> default port(s), words that identify it in an image or container/guest name, and the Setup service it fills
APPS = {
    "plex": {"label": "Plex", "ports": [32400], "words": ["plex", "pms-docker"]},
    "sonarr": {"label": "Sonarr", "ports": [8989], "words": ["sonarr"]},
    "radarr": {"label": "Radarr", "ports": [7878], "words": ["radarr"]},
    "lidarr": {"label": "Lidarr", "ports": [8686], "words": ["lidarr"]},
    "prowlarr": {"label": "Prowlarr", "ports": [9696], "words": ["prowlarr"]},
    "overseerr": {"label": "Overseerr / Jellyseerr", "ports": [5055], "words": ["overseerr", "jellyseerr", "seerr"]},
    "qbittorrent": {"label": "qBittorrent", "ports": [8080, 8081, 8090], "words": ["qbittorrent", "qbit"]},
    "deluge": {"label": "Deluge", "ports": [8112], "words": ["deluge"]},
    "sabnzbd": {"label": "SABnzbd", "ports": [8080, 8085, 8090], "words": ["sabnzbd", "sabnzb"]},
}
ARR = {"sonarr", "radarr", "lidarr", "prowlarr"}
TIMEOUT = 2.5

# Inside a container: list every container with its image, networking and published ports, and for the ones that look
# like a media app, print the API key from the app's config file (##K lines). Runs as root where docker needs it.
DOCKER_SCRIPT = r"""
docker info >/dev/null 2>&1 || { echo "##NODOCKER"; exit 0; }
docker ps --format '{{.ID}}|{{.Names}}|{{.Image}}' | while IFS='|' read -r id name image; do
  net=$(docker inspect -f '{{.HostConfig.NetworkMode}}|{{range $p, $b := .NetworkSettings.Ports}}{{$p}}={{range $b}}{{.HostPort}},{{end}};{{end}}|{{range .NetworkSettings.Networks}}{{.IPAddress}},{{end}}' "$id" 2>/dev/null)
  echo "##C $name|$image|$net"
  key=""
  case "$(echo "$image $name" | tr 'A-Z' 'a-z')" in
    *sonarr*|*radarr*|*lidarr*|*prowlarr*)
      key=$(docker exec "$id" sh -c 'cat /config/config.xml 2>/dev/null' | sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' | head -n1) ;;
    *plex*|*pms-docker*)
      key=$(docker exec "$id" sh -c 'cat "/config/Library/Application Support/Plex Media Server/Preferences.xml" 2>/dev/null' | sed -n 's:.*PlexOnlineToken="\([^"]*\)".*:\1:p' | head -n1) ;;
    *seerr*)
      key=$(docker exec "$id" sh -c 'cat /app/config/settings.json 2>/dev/null' | sed -n 's:.*"apiKey"[^"]*"\([^"]*\)".*:\1:p' | head -n1) ;;
    *sabnzb*)
      key=$(docker exec "$id" sh -c 'cat /config/sabnzbd.ini 2>/dev/null' | sed -n 's:^api_key *= *\(.*\)$:\1:p' | head -n1) ;;
  esac
  [ -n "$key" ] && echo "##K $name|$key"
done
exit 0
"""

# Apps installed directly in a VM/LXC (Proxmox helper scripts, apt packages): their usual config locations.
NATIVE_SCRIPT = r"""
k() { [ -n "$2" ] && echo "##K $1|$2"; }
for app in sonarr radarr lidarr prowlarr; do
  for f in /var/lib/$app/config.xml /opt/$app/config.xml /home/*/.config/$(echo $app | sed 's/./\U&/')/config.xml /root/.config/$(echo $app | sed 's/./\U&/')/config.xml; do
    [ -f "$f" ] && { k $app "$(sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' "$f" | head -n1)"; break; }
  done
done
for f in "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"; do
  [ -f "$f" ] && k plex "$(sed -n 's:.*PlexOnlineToken="\([^"]*\)".*:\1:p' "$f" | head -n1)"
done
for f in /opt/overseerr/config/settings.json /opt/jellyseerr/config/settings.json /var/lib/jellyseerr/settings.json /etc/jellyseerr/settings.json; do
  [ -f "$f" ] && { k overseerr "$(sed -n 's:.*"apiKey"[^"]*"\([^"]*\)".*:\1:p' "$f" | head -n1)"; break; }
done
for f in /root/.sabnzbd/sabnzbd.ini /home/*/.sabnzbd/sabnzbd.ini /var/lib/sabnzbd/sabnzbd.ini /opt/sabnzbd/sabnzbd.ini; do
  [ -f "$f" ] && { k sabnzbd "$(sed -n 's:^api_key *= *\(.*\)$:\1:p' "$f" | head -n1)"; break; }
done
exit 0
"""


def guess_app(text: str) -> str | None:
    t = text.lower()
    for app, info in APPS.items():
        if any(w in t for w in info["words"]):
            return app
    return None


def _port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def identify(url: str, hint: str | None = None) -> str | None:
    """Ask whatever answers at url what it is. Returns the app id, or None if it isn't a media app we know.
    Checks are strict because several of these apps answer any path with their web page."""
    s = requests.Session()
    s.headers["User-Agent"] = "Jarvis"

    def get(path, follow=True):
        try:
            return s.get(url + path, timeout=TIMEOUT, allow_redirects=follow)
        except requests.RequestException:
            return None

    def js(r):
        try:
            return r.json() if r is not None and r.status_code == 200 else None
        except ValueError:
            return None

    r = get("/identity")
    if r is not None and r.status_code == 200 and "MediaContainer" in r.text and "machineIdentifier" in r.text:
        return "plex"
    ping = js(get("/ping"))
    if isinstance(ping, dict) and str(ping.get("status", "")).lower() == "ok":
        page = get("/")
        text = page.text.lower()[:6000] if page is not None else ""
        for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
            if f"<title>{app}" in text or f"{app}</title>" in text:
                return app
        for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
            if app in text:
                return app
        return hint if hint in ARR else None
    st = js(get("/api/v1/status"))
    if isinstance(st, dict) and "version" in st and ("commitTag" in st or "updateAvailable" in st):
        return "overseerr"
    sab = js(get("/api?mode=version&output=json"))
    if isinstance(sab, dict) and set(sab) <= {"version", "error"} and sab.get("version"):
        return "sabnzbd"
    r = get("/api/v2/app/version", follow=False)
    if r is not None and ((r.status_code == 200 and re.match(r"^v?\d+\.\d+", r.text.strip()))
                          or (r.status_code == 403 and r.text.strip() == "Forbidden")):
        return "qbittorrent"
    page = get("/")
    if page is not None and "deluge" in page.text.lower()[:6000]:
        return "deluge"
    return None


def _norm(url: str) -> str:
    p = urlparse(url if "://" in url else "http://" + url)
    return f"{(p.hostname or '').lower()}:{p.port or (443 if p.scheme == 'https' else 80)}"


class MediaDiscovery:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.found: dict[str, dict] = {}   # key = "host:port"
        self.notes: list[str] = []
        self.lock = threading.Lock()

    # ------------------------------------------------------------ sources

    def _docker_hosts(self) -> list[dict]:
        hosts = [h for h in self.cfg.get("unraid") or []] + [h for h in self.cfg.get("linux_hosts") or [] if h.get("os") != "windows"]
        return [h for h in hosts if h.get("host")]

    def _add(self, app: str, url: str, where: str, key: str | None = None, note: str = ""):
        k = _norm(url)
        with self.lock:
            old = self.found.get(k)
            if old and old.get("key") and not key:
                return
            self.found[k] = {"app": app, "url": url.rstrip("/"), "where": where, "key": key or (old or {}).get("key"), "note": note}

    def _scan_docker(self, host: dict):
        try:
            root = hk._docker_needs_root(host)
            out = hk._run(host, DOCKER_SCRIPT, timeout=180, root=root)
        except Exception as e:
            self.notes.append(f"{host['name']}: couldn't list containers ({e})")
            return
        if "##NODOCKER" in out:
            return
        keys = dict(l.split("|", 1) for l in hk._markers(out, "K") if "|" in l)
        for line in hk._markers(out, "C"):
            name, image, mode, ports, ips = (line.split("|") + [""] * 5)[:5]
            app = guess_app(f"{image} {name}")
            if not app:
                continue
            defaults = APPS[app]["ports"]
            published = {}  # container port -> host port
            for part in ports.split(";"):
                if "=" in part:
                    cport, hports = part.split("=", 1)
                    hp = next((p for p in hports.split(",") if p), None)
                    if hp and cport.endswith("/tcp"):
                        published[int(cport.split("/")[0])] = int(hp)
            url, note = None, ""
            if mode == "host":
                url = f"http://{host['host']}:{defaults[0]}"
            elif any(p in published for p in defaults):
                url = f"http://{host['host']}:{published[next(p for p in defaults if p in published)]}"
            elif published:
                url = f"http://{host['host']}:{next(iter(published.values()))}"
            else:
                own = [ip for ip in ips.split(",") if ip and not ip.startswith(("172.17.", "172.18.", "172.19."))]
                if own:  # e.g. Unraid br0 / macvlan: the container has its own address on your network
                    url = f"http://{own[0]}:{defaults[0]}"
                else:
                    note = "No port published to the host, so Jarvis can't reach it"
            where = f"container {name} on {host['name']}"
            if url:
                self._candidate(app, url, where, keys.get(name))
            else:
                with self.lock:
                    self.found[f"{host['host']}:{name}"] = {"app": app, "url": "", "where": where, "key": None, "note": note}

    def _candidate(self, app_hint: str | None, url: str, where: str, key: str | None = None):
        app = identify(url, app_hint)
        if app is None and app_hint is not None:
            # it may be starting up or behind a login page; keep it but say so
            if _port_open(urlparse(url).hostname, urlparse(url).port or 80):
                self._add(app_hint, url, where, key, "Answers on this port, but didn't identify itself. Check it's the right app")
            return
        if app:
            self._add(app, url, where, key if app == app_hint or app_hint is None else None)

    def _scan_guests(self):
        """Proxmox VMs and LXCs: name hints plus the apps' usual ports on each guest's IP."""
        if not self.cfg.get("proxmox"):
            return
        from .discovery import Discovery
        try:
            guests = Discovery(self.cfg).scan()
        except Exception as e:
            self.notes.append(f"Proxmox: couldn't list guests ({e})")
            return
        jobs = []
        for g in guests:
            if not g.get("ip") or g.get("status") != "running":
                continue
            hint = guess_app(g["name"])
            ports = sorted({p for a in APPS.values() for p in a["ports"]})
            for port in ports:
                jobs.append((g, hint, port))

        def probe(job):
            g, hint, port = job
            if _port_open(g["ip"], port, 0.8):
                kind = "LXC" if g["type"] == "lxc" else "VM"
                self._candidate(hint if hint and port in APPS[hint]["ports"] else None, f"http://{g['ip']}:{port}",
                                f"{kind} {g['vmid']} {g['name']} on {g['node']}")

        with ThreadPoolExecutor(max_workers=24) as ex:
            list(ex.map(probe, jobs))

    def _native_keys(self):
        """For apps found on machines Jarvis can log into, read the key from where a direct install keeps it."""
        by_ip = {h["host"]: h for h in self._docker_hosts()}
        targets = {}
        for f in self.found.values():
            ip = urlparse(f["url"]).hostname if f["url"] else None
            if ip in by_ip and not f["key"] and f["app"] not in ("qbittorrent", "deluge"):
                targets.setdefault(ip, []).append(f)
        for ip, finds in targets.items():
            try:
                out = hk._run(by_ip[ip], NATIVE_SCRIPT, timeout=60)
            except Exception:
                continue
            keys = dict(l.split("|", 1) for l in hk._markers(out, "K") if "|" in l)
            for f in finds:
                if keys.get(f["app"]):
                    f["key"] = keys[f["app"]]

    # ------------------------------------------------------------ results

    def scan(self) -> list[dict]:
        with ThreadPoolExecutor(max_workers=6) as ex:
            jobs = [ex.submit(self._scan_docker, h) for h in self._docker_hosts()]
            jobs.append(ex.submit(self._scan_guests))
            for j in jobs:
                j.result()
        self._native_keys()
        media = self.cfg.get("media") or {}
        out = []
        for f in self.found.values():
            current = (media.get(f["app"]) or {}).get("url")
            f["configured"] = bool(current) and f["url"] and _norm(current) == _norm(f["url"])
            f["replaces"] = current if current and not f["configured"] else None
            f["label"] = APPS[f["app"]]["label"]
            f["needs"] = (None if not f["url"] else "password" if f["app"] in ("qbittorrent", "deluge")
                          else None if f["key"] else ("token" if f["app"] == "plex" else "api key"))
            out.append(f)
        order = list(APPS)
        return sorted(out, key=lambda f: (order.index(f["app"]), f["where"]))


# Results stay on the server for a while so "Add" can use the key without the browser ever seeing it.
_RESULTS: dict[str, dict] = {}
_RESULTS_AT = 0.0


def scan(cfg: dict) -> dict:
    global _RESULTS_AT
    d = MediaDiscovery(cfg)
    finds = d.scan()
    _RESULTS.clear()
    view = []
    for f in finds:
        fid = uuid.uuid4().hex[:10]
        _RESULTS[fid] = f
        view.append({"id": fid, "app": f["app"], "label": f["label"], "url": f["url"], "where": f["where"],
                     "key_found": bool(f["key"]), "needs": f["needs"], "configured": f["configured"],
                     "replaces": f["replaces"], "note": f["note"]})
    _RESULTS_AT = time.time()
    return {"found": view, "notes": d.notes}


def add(find_id: str, secret: str = "", username: str = "") -> tuple[str, str]:
    """Save a found app into Setup. Returns (service id, test message). Raises ValueError with a readable reason."""
    from . import setup_schema as S
    f = _RESULTS.get(find_id)
    if not f or time.time() - _RESULTS_AT > 3600:
        raise ValueError("That scan result has expired. Scan again.")
    if not f["url"]:
        raise ValueError(f["note"] or "Jarvis can't reach this one")
    app = f["app"]
    values = {"url": f["url"]}
    value = secret.strip() or f["key"] or ""
    if app == "plex":
        values["token_env"] = value
    elif app in ("qbittorrent", "deluge"):
        values["password_env"] = value
        if app == "qbittorrent":
            values["username"] = username.strip() or "admin"
    else:
        values["api_key_env"] = value
    index = S.save(app, values, 0 if (S.load_raw().get("media") or {}).get(app) else None)
    try:
        msg = S.test(app, index)
        return app, "Connected. " + msg
    except Exception as e:
        return app, f"Saved, but the test failed: {e}"
