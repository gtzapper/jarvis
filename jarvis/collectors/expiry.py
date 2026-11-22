"""Certificate and domain expiry: warns before HTTPS certificates or domain registrations run out.
Certificates are read straight from the server (self-signed ones too); domains through RDAP, the public registry lookup."""

import socket
import ssl
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..models import Report

CACHE_SECONDS = 6 * 3600


def cert_expiry(target: str) -> datetime:
    host, _, port = target.strip().removeprefix("https://").split("/")[0].partition(":")
    pem = ssl.get_server_certificate((host, int(port or 443)), timeout=10)
    # decode without trusting it: internal services often use self-signed certificates, and we only want the date
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem)
        path = f.name
    try:
        info = ssl._ssl._test_decode_cert(path)
    finally:
        Path(path).unlink(missing_ok=True)
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)


def domain_expiry(domain: str) -> datetime | None:
    r = requests.get(f"https://rdap.org/domain/{domain.strip().lower()}", timeout=20, headers={"Accept": "application/rdap+json"})
    if r.status_code == 404:
        raise ValueError("not found in the domain registry")
    r.raise_for_status()
    for ev in r.json().get("events") or []:
        if ev.get("eventAction") == "expiration":
            return datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
    return None  # some registries (many country domains) don't publish it


class ExpiryCollector:
    def __init__(self, watch: dict, th: dict | None = None):
        self.name, self.kind = "Certificates & domains", "expiry"
        self.hosts = [h for h in (watch.get("hosts") or []) if str(h).strip()]
        self.domains = [d for d in (watch.get("domains") or []) if str(d).strip()]
        self.warn_days = int(watch.get("warn_days") or 21)
        self._cached: tuple[float, list[Report]] | None = None

    def collect(self) -> list[Report]:
        if self._cached and time.time() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        rep = Report(self.name, self.kind)
        now = datetime.now(timezone.utc)
        for what, check in [(h, cert_expiry) for h in self.hosts] + [(d, domain_expiry) for d in self.domains]:
            label = ("Certificate for " if check is cert_expiry else "Domain ") + str(what)
            try:
                exp = check(str(what))
            except (OSError, socket.timeout, ssl.SSLError, ValueError, requests.RequestException) as e:
                rep.warn(f"err:{what}", f"Couldn't check {label.lower()}: {str(e)[:120]}")
                continue
            if exp is None:
                rep.lines.append(f"{label}: the registry doesn't publish an expiry date")
                continue
            days = (exp - now).days
            rep.lines.append(f"{label}: expires {exp:%d %b %Y} ({days} days)")
            if days < 0:
                rep.crit(f"exp:{what}", f"{label} expired {-days} days ago")
            elif days <= 7:
                rep.crit(f"exp:{what}", f"{label} expires in {days} days")
            elif days <= self.warn_days:
                rep.warn(f"exp:{what}", f"{label} expires in {days} days ({exp:%d %b})")
        self._cached = (time.time(), [rep])
        return [rep]
