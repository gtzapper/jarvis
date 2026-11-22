"""Hardening for reaching Jarvis from the internet (behind a reverse proxy or tunnel).

- Real client address: only from proxies you list; forwarded requests from anyone else count as 'outside'.
- Home vs outside: your LAN/VPN ranges are home. Outside, admins are limited (no Setup, shell, reboots...) unless you allow it.
- Login lockout per address and per account.
- Per-person two-factor sign-in (authenticator codes), required from outside by default.
- Alerts for sign-ins from new outside addresses and for lockouts."""

import hmac
import ipaddress
import threading
import time
from collections import defaultdict, deque

DEFAULT_HOME = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"]


def settings(cfg: dict) -> dict:
    s = dict(cfg.get("internet") or {})
    s.setdefault("trusted_proxies", [])
    if not s.get("home_networks"):
        s["home_networks"] = DEFAULT_HOME  # blank in Setup = the usual private ranges
    s["remote_admin"] = s.get("remote_admin") or "limited"      # limited | full
    s["login_2fa"] = s.get("login_2fa") or "outside"         # off | outside | always
    s.setdefault("sso_counts_as_2fa", True)
    s["outside_session_hours"] = s.get("outside_session_hours") or 72
    s.setdefault("alerts", True)
    return s


def _nets(items) -> list:
    out = []
    for x in items if isinstance(items, list) else str(items or "").replace(";", ",").split(","):
        x = str(x).strip()
        if not x:
            continue
        try:
            out.append(ipaddress.ip_network(x, strict=False))
        except ValueError:
            pass
    return out


def _in(ip: str, nets: list) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in nets)


def client(environ: dict, cfg: dict) -> dict:
    """Who is really asking: {ip, https, via_proxy, untrusted_forward}."""
    s = settings(cfg)
    peer = environ.get("REMOTE_ADDR", "")
    headers = {k: v for k, v in environ.items() if k.startswith("HTTP_")}
    forwarded = headers.get("HTTP_X_FORWARDED_FOR") or headers.get("HTTP_CF_CONNECTING_IP") or headers.get("HTTP_X_REAL_IP")
    trusted = _nets(s["trusted_proxies"])
    info = {"ip": peer, "https": environ.get("wsgi.url_scheme") == "https", "via_proxy": False, "untrusted_forward": False}
    if trusted and _in(peer, trusted):
        info["via_proxy"] = True
        chain = [x.strip() for x in (headers.get("HTTP_X_FORWARDED_FOR") or "").split(",") if x.strip()]
        ip = None
        for hop in reversed(chain):  # the nearest address that isn't one of your proxies
            if not _in(hop, trusted):
                ip = hop
                break
        ip = ip or headers.get("HTTP_CF_CONNECTING_IP") or headers.get("HTTP_X_REAL_IP")
        info["ip"] = ip or "unknown"  # a proxy that didn't say who it was forwarding: never treated as home
        proto = (headers.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip().lower()
        if proto in ("http", "https"):
            info["https"] = proto == "https"
    elif forwarded:
        # someone put a proxy/tunnel in front of Jarvis without listing it: we can't know who's really asking
        info["untrusted_forward"] = True
    return info


def is_home(info: dict, cfg: dict) -> bool:
    if info.get("untrusted_forward"):
        return False
    return _in(info["ip"], _nets(settings(cfg)["home_networks"]))


# ---------------------------------------------------------------- what admins can't do from outside

REMOTE_BLOCKED_PATHS = [  # (method, regex) for admin sessions from outside, when remote_admin = limited
    ("*", r"/api/setup(/.*)?"), ("*", r"/api/users(/.*)?"), ("*", r"/api/invites(/.*)?"), ("*", r"/api/security(/.*)?"),
    ("*", r"/api/2fa(/.*)?"), ("*", r"/api/voice/devices(/.*)?"), ("*", r"/api/location/trackers(/.*)?"),
    ("POST", r"/api/mode"), ("POST", r"/api/update(/.*)?"), ("POST", r"/api/media-discover(/.*)?"), ("POST", r"/api/discover(/.*)?"),
    ("POST", r"/api/media/requests/\d+"), ("POST", r"/api/location/history(/.*)?"),
]
# chat tools/commands an admin can't use from outside (the brain also gets told they're away)
REMOTE_BLOCKED_TOOLS = {"run_command", "reboot_host", "create_vm", "install_updates", "update_containers", "restart_container",
                        "docker_cleanup", "delete_media", "remove_download", "check_vm_setup", "discover_media", "name_device"}
REMOTE_BLOCKED_COMMANDS = {"run", "reboot_cmd", "create_vm", "update_host", "update_everything", "update_named_containers",
                           "restart_container_cmd", "docker_cleanup_cmd", "delete_media_cmd", "remove_download", "check_vm_setup",
                           "update_self", "discover", "discover_media", "name_device", "set_mode"}


def remote_blocked(method: str, path: str) -> bool:
    import re
    return any((m == "*" or m == method) and re.fullmatch(rx, path) for m, rx in REMOTE_BLOCKED_PATHS)


# ---------------------------------------------------------------- lockout

class Lockout:
    """5 failures from one address in 15 minutes blocks it for 15 minutes; 10 on one account locks that account."""
    IP_MAX, USER_MAX, WINDOW, BLOCK = 5, 10, 900, 900

    def __init__(self):
        self.lock = threading.Lock()
        self.ip_fail: dict[str, deque] = defaultdict(deque)
        self.user_fail: dict[str, deque] = defaultdict(deque)
        self.ip_until: dict[str, float] = {}
        self.user_until: dict[str, float] = {}
        self.events: deque = deque(maxlen=50)  # recent failures, for the Security card

    def _trim(self, q: deque, now: float):
        while q and now - q[0] > self.WINDOW:
            q.popleft()

    def blocked(self, ip: str, username: str | None = None) -> int:
        """Seconds left if this address (or account) is locked out, else 0."""
        now = time.time()
        with self.lock:
            left = max(self.ip_until.get(ip, 0), self.user_until.get((username or "").lower(), 0) if username else 0) - now
        return int(left) if left > 0 else 0

    def fail(self, ip: str, username: str | None, what: str) -> str | None:
        """Record a failure. Returns an alert text when this triggered a lockout."""
        now = time.time()
        u = (username or "").lower()
        alert = None
        with self.lock:
            self.events.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": ip, "user": u, "what": what})
            q = self.ip_fail[ip]
            q.append(now)
            self._trim(q, now)
            if len(q) >= self.IP_MAX and self.ip_until.get(ip, 0) < now:
                self.ip_until[ip] = now + self.BLOCK
                alert = f"{len(q)} failed sign-in attempts from {ip}: blocked for 15 minutes."
            if u:
                q2 = self.user_fail[u]
                q2.append(now)
                self._trim(q2, now)
                if len(q2) >= self.USER_MAX and self.user_until.get(u, 0) < now:
                    self.user_until[u] = now + self.BLOCK
                    alert = f"{len(q2)} failed sign-ins for the account '{u}': locked for 15 minutes."
        return alert

    def success(self, ip: str, username: str):
        with self.lock:
            self.ip_fail.pop(ip, None)
            self.user_fail.pop((username or "").lower(), None)

    def recent(self) -> list[dict]:
        with self.lock:
            return list(self.events)[::-1]


# ---------------------------------------------------------------- per-person authenticator codes

def check_user_code(user: dict, code: str) -> tuple[bool, int | None]:
    """(ok, counter) for this person's authenticator code; each code works once (the caller saves the counter)."""
    from .totp import code_at
    secret = user.get("totp")
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False, None
    now = int(time.time() // 30)
    last = int(user.get("totp_last") or 0)
    for c in range(now - 1, now + 2):
        if c > last and hmac.compare_digest(code_at(secret, c), code):
            return True, c
    return False, None


def needs_2fa(cfg: dict, user: dict, home: bool, via_sso: bool) -> bool:
    s = settings(cfg)
    if via_sso and s["sso_counts_as_2fa"]:
        return False
    if user.get("totp"):
        return True  # someone who set it up always uses it, wherever they are
    return s["login_2fa"] == "always" or (s["login_2fa"] == "outside" and not home)
