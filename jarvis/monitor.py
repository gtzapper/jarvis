import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from .config import env
from .models import Problem, Report


def collect_all(collectors: list) -> list[Report]:
    def run(c):
        try:
            return c.collect()
        except Exception as e:  # any failure to reach a host is itself something to report
            rep = Report(c.name, getattr(c, "kind", "host"), reachable=False)
            rep.crit("unreachable", f"Can't reach {c.name}: {type(e).__name__}: {e}")
            return [rep]

    with ThreadPoolExecutor(max_workers=8) as ex:
        return [r for reports in ex.map(run, collectors) for r in reports]


def spoken_summary(reports: list[Report]) -> str:
    """A short, Jarvis-style sentence. This is what the voice layer will read out later."""
    problems = [p for r in reports for p in r.problems]
    if not problems:
        return f"All {len(reports)} systems are running normally."
    crit = [p for p in problems if p.severity == "critical"]
    head = f"{len(problems)} issue{'s' if len(problems) > 1 else ''} need attention"
    if crit:
        head += f", {len(crit)} critical"
    ordered = crit + [p for p in problems if p.severity != "critical"]
    return head + ". " + ". ".join(p.message for p in ordered[:5]) + "."


def print_reports(reports: list[Report]) -> None:
    for r in reports:
        flag = "DOWN" if not r.reachable else ("!!" if r.problems else "OK")
        print(f"\n[{flag}] {r.name} ({r.kind})")
        for line in r.lines:
            print(f"    {line}")
        for e in r.events:
            print(f"    -> NEW: {e}")
        for p in r.problems:
            print(f"    -> {p.severity.upper()}: {p.message}")
    print(f"\n{spoken_summary(reports)}")


class Notifier:
    chat_hook = None  # set by the web server: also send to linked Telegram/Discord/Matrix/Signal accounts

    def __init__(self, cfg: dict):
        self.ntfy_url = env((cfg.get("notify") or {}).get("ntfy_url_env"))

    def send(self, title: str, message: str, critical: bool = False, resolved: bool = False, ntfy: bool = True) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {title}: {message}")
        if Notifier.chat_hook:
            try:
                Notifier.chat_hook(title, message)
            except Exception as e:
                print(f"[{stamp}] (chat apps failed: {e})")
        if not self.ntfy_url or not ntfy:
            return
        tag = "white_check_mark" if resolved else ("rotating_light" if critical else "warning")
        try:
            requests.post(
                self.ntfy_url,
                data=message.encode(),
                headers={"Title": title, "Tags": tag, "Priority": "urgent" if critical else "default"},
                timeout=10,
            )
        except requests.RequestException as e:
            print(f"[{stamp}] (ntfy push failed: {e})")


class AlertTracker:
    """Alerts once when a problem has persisted `alert_after` checks, and once when it clears."""

    def __init__(self, alert_after: int):
        self.alert_after = alert_after
        self.seen: dict[str, int] = {}
        self.active: dict[str, Problem] = {}

    def update(self, reports: list[Report]) -> tuple[list[Problem], list[Problem]]:
        current = {f"{r.name}:{p.key}": p for r in reports for p in r.problems}
        self.seen = {k: self.seen.get(k, 0) + 1 for k in current}
        new = [p for k, p in current.items() if self.seen[k] >= self.alert_after and k not in self.active]
        resolved = [p for k, p in self.active.items() if k not in current]
        self.active = {k: p for k, p in current.items() if self.seen[k] >= self.alert_after}
        return new, resolved


class Monitor:
    """One check of everything plus alerting. Used by the CLI loop and the web dashboard."""

    def __init__(self, cfg: dict, collectors: list):
        self.collectors = collectors
        self.notifier = Notifier(cfg)
        self.tracker = AlertTracker(cfg["alert_after"])
        self.reports: list[Report] = []
        self.events: list[tuple[str, str]] = []  # (time, message) recent alerts/resolutions/new devices
        self.updated: datetime | None = None

    def _note(self, title: str, message: str, **kw) -> None:
        self.notifier.send(title, message, **kw)
        self.events = [(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"{title}: {message}")] + self.events[:199]

    def tick(self) -> tuple[list[Report], bool]:
        reports = collect_all(self.collectors)
        new, resolved = self.tracker.update(reports)
        for p in new:
            self._note("Jarvis alert", p.message, critical=p.severity == "critical")
        for p in resolved:
            self._note("Jarvis: resolved", p.message, resolved=True)
        events = [e for r in reports for e in r.events]
        for e in events:
            self._note("Jarvis: new device", e)
        self.reports, self.updated = reports, datetime.now()
        return reports, bool(new or resolved or events)


def run_monitor(cfg: dict, collectors: list) -> None:
    mon = Monitor(cfg, collectors)
    interval = cfg["interval_seconds"]
    print(f"Jarvis is watching {len(collectors)} source(s), checking every {interval}s. Ctrl+C to stop.")
    while True:
        started = time.monotonic()
        reports, noisy = mon.tick()
        if not noisy:
            print(f"[{datetime.now():%H:%M:%S}] {spoken_summary(reports)}")
        time.sleep(max(0, interval - (time.monotonic() - started)))
