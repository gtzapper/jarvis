"""Invitations: an admin enters someone's email address, Jarvis emails them a one-time link, and they create their
own account (with single sign-on, or a password of their choice).

Links are random 32-byte tokens; only a SHA-256 of each is stored. They expire (7 days by default) and work once."""

import hashlib
import html
import json
import secrets
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from .config import env

FILE = Path("data/invites.json")
_lock = threading.Lock()


class InviteError(Exception):
    pass


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8")).get("invites", [])
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"invites": items}, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def public(i: dict) -> dict:
    state = "used" if i.get("used") else "revoked" if i.get("revoked") else "expired" if i["expires"] < time.time() else "waiting"
    return {"id": i["id"], "email": i["email"], "name": i.get("name") or "", "role": i["role"], "state": state,
            "created": i["created"], "expires": datetime.fromtimestamp(i["expires"]).strftime("%Y-%m-%d %H:%M"),
            "invited_by": i.get("invited_by"), "sent": i.get("sent"), "error": i.get("error")}


def all_invites() -> list[dict]:
    with _lock:
        return _load()


def create(email: str, role: str, name: str, invited_by: str, days: int = 7) -> tuple[dict, str]:
    """A new invitation and its link token (shown only now; never stored)."""
    email = (email or "").strip()
    if "@" not in parseaddr(email)[1]:
        raise InviteError("That doesn't look like an email address")
    if role not in ("admin", "family"):
        raise InviteError("Role must be admin or family")
    token = secrets.token_urlsafe(32)
    inv = {"id": "i_" + secrets.token_hex(5), "hash": _h(token), "email": parseaddr(email)[1].lower(), "name": (name or "").strip()[:60],
           "role": role, "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "expires": time.time() + max(1, min(days, 60)) * 86400,
           "invited_by": invited_by}
    with _lock:
        items = [i for i in _load() if i["expires"] > time.time() - 30 * 86400]  # forget long-dead ones
        for i in items:  # a new invite to the same address replaces any still waiting
            if i["email"] == inv["email"] and not i.get("used"):
                i["revoked"] = True
        items.append(inv)
        _save(items)
    return inv, token


def find(token: str) -> dict | None:
    """A usable invitation for this link, or None."""
    if not token:
        return None
    h = _h(token)
    i = next((x for x in all_invites() if secrets.compare_digest(x["hash"], h)), None)
    if not i or i.get("used") or i.get("revoked") or i["expires"] < time.time():
        return None
    return i


def mark(inv_id: str, **changes) -> None:
    with _lock:
        items = _load()
        for i in items:
            if i["id"] == inv_id:
                i.update(changes)
        _save(items)


def use(token: str, user_id: str) -> dict | None:
    """Spend the invitation (once). Returns it, or None if it was no longer valid."""
    with _lock:
        items = _load()
        h = _h(token)
        i = next((x for x in items if secrets.compare_digest(x["hash"], h)), None)
        if not i or i.get("used") or i.get("revoked") or i["expires"] < time.time():
            return None
        i["used"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        i["user"] = user_id
        _save(items)
        return i


# ---------------------------------------------------------------- sending (Setup > Outgoing email)

def smtp_settings(cfg: dict) -> dict | None:
    s = cfg.get("smtp") or {}
    return s if s.get("host") and s.get("from_address") else None


def send_mail(cfg: dict, to: str, subject: str, text: str, html_body: str | None = None) -> None:
    """Email from Jarvis itself (invitations, tests), through the SMTP server in Setup > Outgoing email."""
    s = smtp_settings(cfg)
    if not s:
        raise InviteError("Set up Outgoing email (SMTP) in Setup first, so Jarvis can send invitations")
    msg = EmailMessage()
    msg["From"] = formataddr((s.get("from_name") or "Jarvis", s["from_address"]))
    msg["To"], msg["Subject"] = to, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    security = s.get("security") or ("ssl" if int(s.get("port") or 587) == 465 else "starttls")
    port = int(s.get("port") or (465 if security == "ssl" else 587 if security == "starttls" else 25))
    ctx = ssl.create_default_context()
    if s.get("verify_tls") is False:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with (smtplib.SMTP_SSL(s["host"], port, context=ctx, timeout=20) if security == "ssl" else smtplib.SMTP(s["host"], port, timeout=20)) as c:
            if security == "starttls":
                c.starttls(context=ctx)
            if s.get("username"):
                c.login(s["username"], env(s.get("password_env") or "SMTP_PASSWORD") or "")
            c.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise InviteError(f"Couldn't send the email: {e}")


def invite_email(link: str, inviter: str, role: str, name: str, expires: str) -> tuple[str, str, str]:
    hello = f"Hi {name}," if name else "Hi,"
    what = ("an admin account (full access to the homelab assistant)" if role == "admin" else
            "a family account: chat with Jarvis, shared shopping and to-do lists, your own reminders and notes, and movie requests")
    subject = f"{inviter} invited you to Jarvis"
    text = (f"{hello}\n\n{inviter} invited you to Jarvis, the household's home assistant. You'll get {what}.\n\n"
            f"Create your account here:\n{link}\n\nThe link works once and expires on {expires}. "
            "If you weren't expecting this, you can ignore it.\n")
    e = html.escape
    body = (f'<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:520px;margin:auto;padding:24px;color:#1b1f24">'
            f'<h2 style="margin:0 0 12px">You\'re invited to Jarvis</h2><p>{e(hello)}</p>'
            f'<p>{e(inviter)} invited you to Jarvis, the household\'s home assistant. You\'ll get {e(what)}.</p>'
            f'<p style="margin:24px 0"><a href="{e(link)}" style="background:#3b82f6;color:#fff;padding:12px 20px;border-radius:8px;'
            f'text-decoration:none;font-weight:600">Create my account</a></p>'
            f'<p style="color:#59636e;font-size:13px">Or open this link: {e(link)}<br>It works once and expires on {e(expires)}. '
            f'If you weren\'t expecting this, you can ignore it.</p></div>')
    return subject, text, body
