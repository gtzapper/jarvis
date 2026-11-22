"""Security watch: SSH logins (new source addresses, failed-login bursts) and new user or admin accounts on the machines
Jarvis can log into, plus the weekly security digest. Read-only: it only reads logs and account lists."""

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .models import Report

STATE = Path("data") / "security_state.json"
EVENTS = Path("data") / "security_events.json"
CACHE_SECONDS = 600

# Read the SSH log (journal, or the classic files on Unraid/older systems) and the account lists.
SCRIPT = r"""
echo "##ME ${SSH_CONNECTION%% *}"
{ journalctl -q --no-pager -o short-iso --since "-%(minutes)d min" _COMM=sshd 2>/dev/null \
  || grep -h "sshd" /var/log/auth.log /var/log/secure /var/log/syslog 2>/dev/null | tail -n 3000; } \
  | grep -E "Accepted |Failed password|Invalid user|authentication failure" | tail -n 1000 | sed 's/^/##L /'
echo "##ADMINS $(getent group sudo wheel admin 2>/dev/null | cut -d: -f4 | tr '\n' ',' )"
echo "##USERS $(awk -F: '($3>=1000 && $3<65534) || $3==0 {print $1}' /etc/passwd | tr '\n' ',')"
exit 0
"""

ACCEPT = re.compile(r"Accepted (\w+) for (\S+) from (\S+)")
FAILED = re.compile(r"(?:Failed password for (?:invalid user )?(\S+)|Invalid user (\S+)) from (\S+)")


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _save(p: Path, d: dict) -> None:
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(d))


def record_event(kind: str, host: str, text: str) -> None:
    """Keep 30 days of security events for the weekly digest."""
    ev = _load(EVENTS).get("events", [])
    cutoff = time.time() - 30 * 86400
    ev = [e for e in ev if e["t"] > cutoff] + [{"t": time.time(), "kind": kind, "host": host, "text": text}]
    _save(EVENTS, {"events": ev[-5000:]})


def events(days: int = 7) -> list[dict]:
    cutoff = time.time() - days * 86400
    return [e for e in _load(EVENTS).get("events", []) if e["t"] > cutoff]


class LoginWatchCollector:
    """One per machine: new SSH source addresses, failed-login bursts, new user and admin accounts."""

    def __init__(self, host: dict, th: dict | None = None, watch: dict | None = None):
        self.host = host
        self.name = f"{host['name']} logins"
        self.kind = "security"
        w = watch or {}
        self.burst = int(w.get("failed_burst") or 10)
        self.trusted = {x.strip() for x in (w.get("trusted_ips") or []) if str(x).strip()}
        self._cached: tuple[float, list[Report]] | None = None

    def collect(self) -> list[Report]:
        if self._cached and time.time() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        from . import housekeeping as hk
        rep = Report(self.name, self.kind)
        name = self.host["name"]
        state = _load(STATE)
        st = state.setdefault(name, {"ips": [], "seen": [], "admins": None, "users": None})
        try:
            out = hk._run(self.host, SCRIPT % {"minutes": 60 * 24 if not st["seen"] else 30}, timeout=60)
        except Exception as e:
            rep.reachable = False
            rep.warn("unreachable", f"Couldn't read the login log on {name}: {str(e)[:150]}")
            self._cached = (time.time(), [rep])
            return [rep]
        me = (hk._markers(out, "ME") or [""])[0]
        lines = hk._markers(out, "L")
        seen = set(st["seen"])
        new_lines = []
        for l in lines:
            h = hashlib.sha1(l.encode()).hexdigest()[:16]
            if h not in seen:
                seen.add(h)
                new_lines.append(l)
        first_run = not st["seen"]
        known_ips = set(st["ips"])
        fails = Counter()
        for l in new_lines:
            m = ACCEPT.search(l)
            if m:
                method, user, ip = m.groups()
                if ip == me or ip in self.trusted:
                    continue  # Jarvis itself, or an address you trust
                if ip not in known_ips:
                    known_ips.add(ip)
                    if not first_run:
                        msg = f"New SSH login to {name}: {user} from {ip} ({method})"
                        rep.events.append(msg)
                        record_event("new_login", name, msg)
                record_event("login", name, f"{user} from {ip}")
                continue
            m = FAILED.search(l)
            if m:
                fails[m.group(3)] += 1
        total_fail = sum(fails.values())
        if total_fail:
            record_event("failed", name, f"{total_fail} failed logins, most from {fails.most_common(1)[0][0]}")
        if total_fail >= self.burst and not first_run:
            ip, n = fails.most_common(1)[0]
            rep.warn("failburst", f"{total_fail} failed SSH logins on {name} in the last half hour ({n} from {ip})")
        admins = sorted({u for u in ",".join(hk._markers(out, "ADMINS")).split(",") if u})
        users = sorted({u for u in ",".join(hk._markers(out, "USERS")).split(",") if u})
        if st["admins"] is not None:
            for u in set(admins) - set(st["admins"]):
                msg = f"New admin (sudo) account on {name}: {u}"
                rep.events.append(msg)
                record_event("new_admin", name, msg)
            for u in set(users) - set(st["users"] or []):
                msg = f"New user account on {name}: {u}"
                rep.events.append(msg)
                record_event("new_user", name, msg)
        st.update(ips=sorted(known_ips)[-500:], seen=list(seen)[-3000:], admins=admins, users=users)
        _save(STATE, state)
        rep.lines.append(f"SSH: {len(known_ips)} known source address(es); {total_fail} failed login(s) since last check")
        rep.lines.append(f"Admins: {', '.join(admins) or 'none listed'}")
        if not lines and not first_run and "journalctl" not in out:
            pass
        self._cached = (time.time(), [rep])
        return [rep]


# ---------------------------------------------------------------- the weekly digest

def digest(cfg: dict, reports: list | None = None) -> str:
    """A week of security in a few lines: logins, failed attempts, new accounts and devices, open ports, updates,
    certificates and backups."""
    ev = events(7)
    by = Counter(e["kind"] for e in ev)
    lines = [f"Security digest for the week to {datetime.now():%a %d %b}:"]
    logins = [e for e in ev if e["kind"] == "login"]
    new = [e for e in ev if e["kind"] == "new_login"]
    lines.append(f"- SSH: {len(logins)} login(s)" + (f", {len(new)} from new addresses: " + "; ".join(e["text"].split(": ", 1)[-1] for e in new[:5]) if new else ", none from new addresses") + ".")
    failed = [e for e in ev if e["kind"] == "failed"]
    if failed:
        n = sum(int(re.match(r"(\d+)", e["text"]).group(1)) for e in failed if re.match(r"\d+", e["text"]))
        lines.append(f"- Failed SSH logins: {n} in total across {len({e['host'] for e in failed})} machine(s).")
    for kind, label in (("new_admin", "New admin accounts"), ("new_user", "New user accounts"), ("new_device", "New devices on the network"),
                        ("ip_change", "Public IP changes")):
        if by[kind]:
            lines.append(f"- {label}: " + "; ".join(e["text"] for e in ev if e["kind"] == kind)[:400] + ".")
    try:
        ports = open_ports(cfg)
        if ports is not None:
            lines.append("- Open to the internet (pfSense port forwards): " + ("; ".join(ports[:10]) if ports else "none") + ".")
    except Exception as e:
        lines.append(f"- Couldn't read pfSense port forwards: {str(e)[:100]}")
    probs = [p for r in (reports or []) for p in r.problems if r.kind in ("expiry", "backups", "security")]
    if probs:
        lines.append("- Needs attention: " + "; ".join(p.message for p in probs[:6]) + ".")
    elif reports:
        lines.append("- Backups, certificates and logins: no open problems.")
    return "\n".join(lines)


def open_ports(cfg: dict) -> list[str] | None:
    """Port forwards on pfSense: what's reachable from the internet."""
    from .collectors.pfsense import PfSenseCollector
    entries = cfg.get("pfsense") or []
    if not entries:
        return None
    out = []
    for e in entries:
        pf = PfSenseCollector(e, cfg["thresholds"])
        rules = pf._get("/firewall/nat/port_forwards") or []
        for r in rules:
            if r.get("disabled"):
                continue
            dst = r.get("destination_port") or "?"
            out.append(f"{r.get('interface', 'wan')} {r.get('protocol', 'tcp')} {dst} -> {r.get('target')}:{r.get('local_port', dst)}"
                       + (f" ({r['descr']})" if r.get("descr") else ""))
    return out
