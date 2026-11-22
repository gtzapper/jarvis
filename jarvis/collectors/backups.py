"""Backup watchdog: Proxmox backups (every guest in a backup job has a recent one, no failed jobs), ZFS scrubs on Proxmox
nodes, and Unraid parity checks. Read-only: uses the monitoring token and a read-only SSH command."""

import re
import time
from datetime import datetime

from ..models import Report
from .proxmox import ProxmoxCollector

CACHE_SECONDS = 1800  # listing backup storage is heavier than the usual checks, so it runs every 30 minutes


def _ago(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f} days ago"


class BackupCollector:
    def __init__(self, cfg: dict, th: dict, watch: dict | None = None):
        self.pve = ProxmoxCollector(cfg, th)
        self.name = f"{cfg['name']} backups"
        self.kind = "backups"
        w = watch or {}
        self.max_age = float(w.get("max_age_hours") or 30) * 3600
        self.scrub_days = int(w.get("scrub_days") or 35)
        self.ignore = {int(v) for v in (w.get("ignore_vmids") or []) if str(v).strip().isdigit()}
        self._cached: tuple[float, list[Report]] | None = None

    def collect(self) -> list[Report]:
        if self._cached and time.time() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        rep = Report(self.name, self.kind)
        try:
            self._check(rep)
        except Exception as e:
            rep.reachable = False
            rep.warn("unreachable", f"Couldn't check backups on {self.pve.name}: {e}")
        self._cached = (time.time(), [rep])
        return [rep]

    def _check(self, rep: Report) -> None:
        get = self.pve._get
        now = time.time()
        guests = {g["vmid"]: g for g in get("/cluster/resources?type=vm") or [] if not g.get("template")}
        jobs = [j for j in get("/cluster/backup") or [] if j.get("enabled", 1) not in (0, "0")]
        expected = set()
        for j in jobs:
            if str(j.get("all", 0)) == "1":
                excl = {int(x) for x in str(j.get("exclude", "")).split(",") if x.strip().isdigit()}
                expected |= set(guests) - excl
            else:
                expected |= {int(x) for x in str(j.get("vmid", "")).split(",") if x.strip().isdigit()}
        expected = {v for v in expected if v in guests and v not in self.ignore}

        newest: dict[int, float] = {}
        seen_vol = set()
        nodes = [n["node"] for n in get("/nodes") or [] if n.get("status") == "online"]
        for node in nodes:
            for st in get(f"/nodes/{node}/storage?content=backup") or []:
                if not st.get("active", 1) or not st.get("enabled", 1):
                    continue
                for item in get(f"/nodes/{node}/storage/{st['storage']}/content?content=backup") or []:
                    if item.get("volid") in seen_vol:
                        continue  # shared storage (NFS, PBS) is listed by every node
                    seen_vol.add(item.get("volid"))
                    vmid, ctime = item.get("vmid"), item.get("ctime") or 0
                    if vmid is not None:
                        newest[int(vmid)] = max(newest.get(int(vmid), 0), float(ctime))
            for t in get(f"/nodes/{node}/tasks?typefilter=vzdump&limit=10") or []:
                if t.get("status") and t["status"] != "OK" and now - (t.get("endtime") or t.get("starttime") or 0) < 2 * 86400:
                    when = datetime.fromtimestamp(t.get("starttime", now)).strftime("%a %H:%M")
                    rep.warn(f"job:{node}:{t.get('upid', '')[-12:]}", f"Backup job on {node} ({when}) ended with: {t['status'][:120]}")
            try:
                for pool in get(f"/nodes/{node}/disks/zfs") or []:
                    detail = get(f"/nodes/{node}/disks/zfs/{pool['name']}") or {}
                    self._scrub(rep, node, pool["name"], str(detail.get("scan") or ""), now)
            except Exception:
                pass  # not a ZFS node, or no permission: skip quietly

        if not jobs:
            rep.warn("nojobs", f"No backup jobs are set up in Proxmox ({self.pve.name}). Datacenter > Backup")
        stale, ok = [], 0
        for vmid in sorted(expected):
            g = guests[vmid]
            age = now - newest[vmid] if vmid in newest else None
            label = f"{vmid} {g.get('name', '')}".strip()
            if age is None:
                stale.append(label)
                rep.warn(f"never:{vmid}", f"{label} is in a backup job but has no backup on any storage")
            elif age > self.max_age:
                sev = rep.crit if age > 3 * self.max_age else rep.warn
                sev(f"old:{vmid}", f"Newest backup of {label} is {_ago(age)}")
                stale.append(label)
            else:
                ok += 1
        not_covered = [f"{v} {guests[v].get('name', '')}".strip() for v in sorted(guests)
                       if v not in expected and v not in self.ignore and guests[v].get("status") == "running"]
        rep.lines.append(f"Backups: {ok} of {len(expected)} guests in backup jobs are up to date" +
                         (f"; newest {_ago(now - max(newest.values()))}" if newest else ""))
        if not_covered:
            rep.lines.append(f"Running but in no backup job: {', '.join(not_covered[:12])}" + (" ..." if len(not_covered) > 12 else ""))
        rep.metrics.update(backups_ok=ok, backups_expected=len(expected), backups_stale=len(stale))

    def _scrub(self, rep: Report, node: str, pool: str, scan: str, now: float) -> None:
        m = re.search(r"scrub repaired \S+ in \S+ with (\d+) errors on (.+)$", scan.strip())
        if not m:
            if "scrub in progress" in scan:
                rep.lines.append(f"ZFS {pool} on {node}: scrub running")
            elif scan:
                rep.lines.append(f"ZFS {pool} on {node}: {scan[:80]}")
            else:
                rep.warn(f"scrub:{node}:{pool}", f"ZFS pool {pool} on {node} has never been scrubbed")
            return
        errors, when = int(m.group(1)), m.group(2).strip()
        try:
            t = datetime.strptime(re.sub(r"\s+", " ", when), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            t = now
        days = (now - t) / 86400
        rep.lines.append(f"ZFS {pool} on {node}: last scrub {days:.0f} days ago, {errors} errors")
        if errors:
            rep.crit(f"scruberr:{node}:{pool}", f"ZFS scrub of {pool} on {node} found {errors} errors")
        elif days > self.scrub_days:
            rep.warn(f"scrub:{node}:{pool}", f"ZFS pool {pool} on {node} hasn't been scrubbed for {days:.0f} days")


class UnraidParityCollector:
    """When Unraid last ran a parity check, and whether it found errors (from /boot/config/parity-checks.log)."""

    def __init__(self, cfg: dict, th: dict, watch: dict | None = None):
        self.cfg = cfg
        self.name = f"{cfg['name']} parity"
        self.kind = "backups"
        self.days = int((watch or {}).get("parity_days") or 35)
        self._cached: tuple[float, list[Report]] | None = None

    def collect(self) -> list[Report]:
        if self._cached and time.time() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        from .ssh_host import run_command
        rep = Report(self.name, self.kind)
        try:
            code, out = run_command(self.cfg, "tail -n 1 /boot/config/parity-checks.log 2>/dev/null || true", timeout=30)
            line = out.strip().splitlines()[-1] if out.strip() else ""
            if not line:
                rep.warn("noparity", f"No parity check has been recorded on {self.cfg['name']}")
            else:
                parts = line.split("|")
                when = datetime.strptime(re.sub(r"\s+", " ", parts[0].strip()), "%Y %b %d %H:%M:%S")
                days = (datetime.now() - when).days
                status = parts[3].strip() if len(parts) > 3 else "0"
                errors = int(parts[4]) if len(parts) > 4 and parts[4].strip().lstrip("-").isdigit() else 0
                rep.lines.append(f"Last parity check {days} days ago ({when:%d %b}), {errors} errors" + ("" if status == "0" else f", status {status}"))
                if errors:
                    rep.crit("parityerr", f"Unraid parity check on {self.cfg['name']} found {errors} errors")
                elif status not in ("0", ""):
                    rep.warn("paritystatus", f"Last parity check on {self.cfg['name']} didn't finish cleanly (status {status})")
                elif days > self.days:
                    rep.warn("parityold", f"No parity check on {self.cfg['name']} for {days} days")
        except Exception as e:
            rep.reachable = False
            rep.warn("unreachable", f"Couldn't read parity history on {self.cfg['name']}: {e}")
        self._cached = (time.time(), [rep])
        return [rep]
