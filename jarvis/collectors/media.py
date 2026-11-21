from datetime import datetime, timezone

from ..media.clients import ArrClient, MediaStack
from ..media.downloaders import fmt_speed
from ..models import Report, fmt_bytes


def stuck_reason(item: dict) -> str:
    for sm in item.get("statusMessages") or []:
        if sm.get("messages"):
            return sm["messages"][0]
    return item.get("errorMessage") or item.get("trackedDownloadState") or "needs attention"


class MediaCollector:
    """One report per media service, so one app being down doesn't hide the others."""

    kind = "media"
    name = "media"

    def __init__(self, stack: MediaStack, th: dict):
        self.stack = stack
        self.th = th

    def collect(self) -> list[Report]:
        jobs = [(a, self._arr) for a in self.stack.arrs.values()]
        if self.stack.prowlarr:
            jobs.append((self.stack.prowlarr, self._prowlarr))
        if self.stack.overseerr:
            jobs.append((self.stack.overseerr, self._overseerr))
        if self.stack.plex:
            jobs.append((self.stack.plex, self._plex))
        jobs += [(d, self._downloader) for d in self.stack.downloaders]
        reports = []
        for app, problem in self.stack.errors:
            rep = Report(app, "media", reachable=False)
            rep.warn("config", f"{app} is skipped until its config is complete: {problem}")
            reports.append(rep)
        for client, fn in jobs:
            rep = Report(client.name, "media")
            try:
                fn(client, rep)
            except Exception as e:
                rep.reachable = False
                rep.crit("unreachable", f"Can't reach {client.name}: {type(e).__name__}: {e}")
            reports.append(rep)
        return reports

    def _health(self, client: ArrClient, rep: Report) -> None:
        for h in client.health():
            level = str(h.get("type", "")).lower()
            msg = f"{client.name}: {h.get('message', '?')}"
            key = f"health:{h.get('source', msg)}"
            if level == "error":
                rep.crit(key, msg)
            elif level == "warning":
                rep.warn(key, msg)
            else:
                rep.lines.append(f"Note: {h.get('message')}")

    def _arr(self, client: ArrClient, rep: Report) -> None:
        rep.lines.append(f"{client.name} {client.version()}")
        self._health(client, rep)
        queue = client.queue()
        rep.metrics["queue"] = len(queue)
        rep.lines.append(f"Downloads: {len(queue)} in queue")
        for item in queue[:5]:
            size, left = item.get("size") or 0, item.get("sizeleft") or 0
            done = int((1 - left / size) * 100) if size else 0
            eta = f", {item['timeleft']} left" if item.get("timeleft") else ""
            rep.lines.append(f"  {item.get('title', '?')}: {item.get('status', '?')}, {done}% of {fmt_bytes(size)}{eta}")
        for item in queue:
            if str(item.get("trackedDownloadStatus", "")).lower() in ("warning", "error"):
                rep.warn(f"queue:{item.get('id')}", f"{client.name} download stuck: {item.get('title', '?')}. {stuck_reason(item)}")

    def _prowlarr(self, client, rep: Report) -> None:
        rep.lines.append(f"Prowlarr {client.version()}")
        self._health(client, rep)
        indexers = {i["id"]: i.get("name", i["id"]) for i in client.indexers()}
        enabled = sum(1 for i in client.indexers() if i.get("enable"))
        rep.lines.append(f"Indexers: {enabled}/{len(indexers)} enabled")
        now = datetime.now(timezone.utc).isoformat()
        for st in client.indexer_status():
            until = st.get("disabledTill") or ""
            if until > now:
                name = indexers.get(st.get("indexerId"), st.get("indexerId"))
                rep.warn(f"indexer:{name}", f"Indexer {name} is failing and disabled until {until[:16].replace('T', ' ')} UTC")

    def _overseerr(self, client, rep: Report) -> None:
        rep.lines.append(f"Overseerr {client.version()}")
        c = client.counts()
        pending = c.get("pending", 0)
        rep.metrics["pending_requests"] = pending
        rep.lines.append(f"Requests: {pending} pending, {c.get('processing', 0)} processing, {c.get('available', 0)} available")

    def _plex(self, client, rep: Report) -> None:
        rep.lines.append(f"Plex {client.version()}")
        sessions = client.sessions()
        transcodes = sum(1 for s in sessions if s.get("TranscodeSession"))
        rep.metrics.update(streams=len(sessions), transcodes=transcodes)
        rep.lines.append(f"Streams: {len(sessions)} ({transcodes} transcoding)")
        rep.lines += [f"  {client.describe_session(s)}" for s in sessions]

    def _downloader(self, client, rep: Report) -> None:
        rep.lines.append(f"{client.name} {client.version()}")
        st = client.stats()
        items = client.items()
        active = [i for i in items if i["state"] == "downloading"]
        rep.metrics.update(down=st["down"], up=st["up"], active=len(active), unfinished=len(items))
        line = f"Down {fmt_speed(st['down'])}"
        if client.kind == "torrent":
            line += f", up {fmt_speed(st['up'])}"
        rep.lines.append(f"{line}. {len(active)} downloading, {len(items)} unfinished")
        rep.lines += [f"  {n}" for n in st["notes"]]
        stalled = sum(1 for i in items if i["state"] == "stalled")
        if stalled:
            rep.lines.append(f"  {stalled} stalled (no peers)")
        for key, msg in st["problems"]:
            rep.warn(key, msg)
        for i in items:
            if i["error"]:
                rep.warn(f"item:{i['id']}", f"{client.name}: {i['name']} has an error: {i['error']}")
