"""Network watch: internet speed history (Cloudflare's speed test, no account), public IP changes, and your own names
for devices on the network."""

import json
import statistics
import time
from pathlib import Path

import requests

from .models import Report

DATA = Path("data") / "network.json"
NAMES = Path("data") / "device_names.json"
CF = "https://speed.cloudflare.com"


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p: Path, d) -> None:
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- device names

def device_names() -> dict[str, str]:
    return _load(NAMES, {})


def name_device(mac_or_ip: str, name: str, devices: list[dict] | None = None) -> str:
    """Label a device by MAC (or by IP, looked up in pfSense's device list)."""
    key = mac_or_ip.strip().lower()
    if not key.count(":") == 5:
        match = next((d for d in devices or [] if d.get("ip") == key), None)
        if not match:
            raise ValueError(f"I can't see a device at {mac_or_ip} right now")
        key = match["mac"].lower()
    names = device_names()
    names[key] = name.strip()
    _save(NAMES, names)
    return key


# ---------------------------------------------------------------- speed test

def speed_test(down_mb: int = 25, up_mb: int = 10) -> dict:
    """Download and upload a test file to Cloudflare's speed test and time it. Uses about 35 MB of data."""
    s = requests.Session()
    s.headers["User-Agent"] = "Jarvis"
    # latency: a few tiny requests
    pings = []
    for _ in range(5):
        t = time.perf_counter()
        s.get(f"{CF}/__down?bytes=0", timeout=10)
        pings.append((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    r = s.get(f"{CF}/__down?bytes={down_mb * 1_000_000}", timeout=120, stream=True)
    got = sum(len(c) for c in r.iter_content(256 * 1024))
    down = got * 8 / (time.perf_counter() - t) / 1e6
    payload = b"\0" * (up_mb * 1_000_000)
    t = time.perf_counter()
    s.post(f"{CF}/__up", data=payload, timeout=120)
    up = len(payload) * 8 / (time.perf_counter() - t) / 1e6
    return {"t": time.time(), "down": round(down, 1), "up": round(up, 1), "ping": round(statistics.median(pings), 1)}


def history() -> list[dict]:
    return _load(DATA, {}).get("speed", [])


def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            ip = requests.get(url, timeout=10).text.strip()
            if ip and len(ip) < 60:
                return ip
        except requests.RequestException:
            continue
    raise RuntimeError("couldn't find the public IP")


class NetworkWatchCollector:
    """Public IP every 15 minutes, speed test every N hours (0 = off). Alerts on IP changes and slow internet."""

    def __init__(self, watch: dict, th: dict | None = None):
        self.name, self.kind = "Internet", "network"
        self.every = float(watch.get("speedtest_hours", 12) or 0) * 3600
        self.slow_pct = int(watch.get("slow_percent") or 50)
        self._ip_at = 0.0
        self._last: Report | None = None

    def collect(self) -> list[Report]:
        from .security import record_event
        d = _load(DATA, {})
        rep = Report(self.name, self.kind)
        now = time.time()
        if now - self._ip_at > 900:
            self._ip_at = now
            try:
                ip = public_ip()
                old = d.get("ip")
                if old and ip != old:
                    msg = f"Your public IP changed from {old} to {ip}"
                    rep.events.append(msg)
                    record_event("ip_change", "internet", msg)
                d["ip"], d["ip_since"] = ip, d.get("ip_since") if ip == old else time.strftime("%Y-%m-%d %H:%M")
            except Exception as e:
                rep.warn("noip", f"Couldn't check the public IP ({e}). Is the internet down?")
        if self.every and now - (d.get("speed") or [{"t": 0}])[-1]["t"] > self.every:
            try:
                res = speed_test()
                d["speed"] = (d.get("speed") or [])[-200:] + [res]
            except Exception as e:
                rep.warn("speedfail", f"Speed test failed: {str(e)[:100]}")
        _save(DATA, d)
        if d.get("ip"):
            rep.lines.append(f"Public IP {d['ip']}" + (f" (since {d['ip_since']})" if d.get("ip_since") else ""))
        sp = d.get("speed") or []
        if sp:
            last = sp[-1]
            rep.lines.append(f"Last speed test {time.strftime('%a %H:%M', time.localtime(last['t']))}: "
                             f"{last['down']} down / {last['up']} up Mbps, {last['ping']} ms")
            rep.metrics.update(down_mbps=last["down"], up_mbps=last["up"], ping_ms=last["ping"])
            if len(sp) >= 5:
                usual = statistics.median(x["down"] for x in sp[-15:-1])
                rep.lines.append(f"Usual download speed: {usual:.0f} Mbps")
                if last["down"] < usual * self.slow_pct / 100:
                    rep.warn("slow", f"Internet is slow: {last['down']} Mbps down, usually about {usual:.0f}")
        return [rep]
