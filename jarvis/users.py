"""User accounts: the owner (admin) and family members, with passwords and/or single sign-on (OpenID Connect).

Stored in data/users.json. Passwords are PBKDF2-SHA256 hashes, never the passwords themselves.

Roles:
  admin   everything (Setup, the lab, email, approvals)
  family  chat with a limited set of things: shared lists, their own reminders and notes, weather,
          what to watch, asking for movies and shows. No Setup, no lab, no email, no approvals."""

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

FILE = Path("data/users.json")
ROLES = {"admin": "Admin: everything", "family": "Family: chat, lists, own reminders and notes, watch suggestions, media requests"}
_lock = threading.Lock()
ITERATIONS = 260_000


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${h}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        algo, it, salt, h = (stored or "").split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return algo == "pbkdf2_sha256" and hmac.compare_digest(got, h)
    except (ValueError, TypeError):
        return False


def _load() -> list[dict]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8")).get("users", [])
    except (OSError, ValueError):
        return []


def _save(users: list[dict]) -> None:
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def public(u: dict | None) -> dict | None:
    """What the browser may see about a user (no hashes)."""
    if not u:
        return None
    return {"id": u["id"], "username": u["username"], "name": u.get("name") or u["username"], "role": u["role"],
            "has_password": bool(u.get("pw")), "sso": bool(u.get("oidc_sub")), "email": u.get("email") or "",
            "has_2fa": bool(u.get("totp")),
            "disabled": bool(u.get("disabled")), "created": u.get("created"), "last_login": u.get("last_login"),
            "chat": {k: v for k, v in (u.get("chat") or {}).items()}}


def all_users() -> list[dict]:
    with _lock:
        return _load()


def any_users() -> bool:
    return bool(all_users())


def get(uid: str | None) -> dict | None:
    if not uid:
        return None
    return next((u for u in all_users() if u["id"] == uid), None)


def by_username(name: str) -> dict | None:
    name = (name or "").strip().lower()
    return next((u for u in all_users() if u["username"].lower() == name), None)


def _clean_username(name: str) -> str:
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", name):
        raise ValueError("Usernames are 2 to 32 letters, numbers, dots, dashes or underscores")
    return name


def create(username: str, name: str = "", role: str = "family", password: str | None = None, email: str = "",
           oidc_sub: str | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}")
    username = _clean_username(username)
    if password is not None and password != "" and len(password) < 8:
        raise ValueError("Passwords need at least 8 characters")
    with _lock:
        users = _load()
        if any(u["username"].lower() == username for u in users):
            raise ValueError(f"There's already a user called {username}")
        u = {"id": "u_" + secrets.token_hex(6), "username": username, "name": (name or username).strip()[:60], "role": role,
             "pw": hash_password(password) if password else None, "email": (email or "").strip().lower()[:200],
             "oidc_sub": oidc_sub, "created": datetime.now().strftime("%Y-%m-%dT%H:%M"), "chat": {}}
        users.append(u)
        _save(users)
        return u


def update(uid: str, **changes) -> dict:
    with _lock:
        users = _load()
        u = next((x for x in users if x["id"] == uid), None)
        if not u:
            raise ValueError("No such user")
        if "role" in changes and changes["role"] not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        new_role = changes.get("role", u["role"])
        disabling = changes.get("disabled", u.get("disabled"))
        if u["role"] == "admin" and (new_role != "admin" or disabling):
            if not any(x["role"] == "admin" and not x.get("disabled") and x["id"] != uid for x in users):
                raise ValueError("Jarvis needs at least one active admin")
        if "password" in changes:
            pw = changes.pop("password")
            if pw and len(pw) < 8:
                raise ValueError("Passwords need at least 8 characters")
            u["pw"] = hash_password(pw) if pw else None
            u["sv"] = int(u.get("sv") or 0) + 1  # a new password signs out everywhere else
        if "username" in changes:
            name = _clean_username(changes.pop("username"))
            if any(x["username"].lower() == name and x["id"] != uid for x in users):
                raise ValueError(f"There's already a user called {name}")
            u["username"] = name
        if changes.pop("bump_sessions", False):
            u["sv"] = int(u.get("sv") or 0) + 1  # every other signed-in device has to sign in again
        for k in ("name", "role", "email", "oidc_sub", "disabled", "last_login", "chat", "invited", "totp", "totp_last",
                  "totp_pending", "seen_ips"):
            if k in changes:
                u[k] = changes[k]
        _save(users)
        return u


def delete(uid: str) -> None:
    with _lock:
        users = _load()
        u = next((x for x in users if x["id"] == uid), None)
        if not u:
            return
        if u["role"] == "admin" and not any(x["role"] == "admin" and not x.get("disabled") and x["id"] != uid for x in users):
            raise ValueError("Jarvis needs at least one active admin")
        _save([x for x in users if x["id"] != uid])


def authenticate(username: str, password: str) -> dict | None:
    """Username + password. A blank username means the admin account (the old password-only login)."""
    if not username:
        admins = [u for u in all_users() if u["role"] == "admin" and u.get("pw") and not u.get("disabled")]
        u = next((x for x in admins if check_password(password, x["pw"])), None)
    else:
        u = by_username(username)
        if not u or u.get("disabled") or not check_password(password, u.get("pw")):
            return None
    if u:
        update(u["id"], last_login=datetime.now().strftime("%Y-%m-%dT%H:%M"))
    return u


def ensure_owner(legacy_password: str | None) -> dict | None:
    """First start with accounts: the old dashboard password becomes the 'admin' account's password."""
    if any_users() or not legacy_password:
        return None
    return create("admin", "Owner", "admin", legacy_password)


def find_chat_user(app: str, chat_id: str) -> dict | None:
    """Who is this Telegram/Discord/Matrix account? (linked in Setup > Users)"""
    return next((u for u in all_users() if str((u.get("chat") or {}).get(app) or "") == str(chat_id) and not u.get("disabled")), None)
