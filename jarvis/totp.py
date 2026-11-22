"""Two-factor approval for destructive actions: a 6-digit code from an authenticator app (RFC 6238 TOTP, the same as
Google Authenticator, Aegis, 1Password, Bitwarden...). Standard library only."""

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import urllib.parse
from pathlib import Path

from .config import env

PENDING = Path("data") / "2fa_pending"
USED = Path("data") / "2fa_used.json"
ENV_NAME = "JARVIS_TOTP_SECRET"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def verify(code: str, secret: str | None = None, window: int = 1) -> bool:
    """Check a code (allowing one 30-second step of clock drift). Each code works only once."""
    secret = secret or env(ENV_NAME)
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    now = int(time.time() // 30)
    try:
        used = set(json.loads(USED.read_text()))
    except (OSError, ValueError):
        used = set()
    for c in range(now - window, now + window + 1):
        if hmac.compare_digest(code_at(secret, c), code):
            if c in used:
                return False  # replayed
            used = {u for u in used if u >= now - 5} | {c}
            USED.parent.mkdir(exist_ok=True)
            USED.write_text(json.dumps(sorted(used)))
            return True
    return False


def uri(secret: str, label: str = "Jarvis") -> str:
    return "otpauth://totp/" + urllib.parse.quote(label) + "?" + urllib.parse.urlencode(
        {"secret": secret, "issuer": "Jarvis", "algorithm": "SHA1", "digits": 6, "period": 30})


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("two_factor") or {}).get("enabled")) and bool(env(ENV_NAME))


def start_setup() -> str:
    """A new secret waiting to be confirmed with a first code (the active one, if any, keeps working until then)."""
    s = new_secret()
    PENDING.parent.mkdir(exist_ok=True)
    PENDING.write_text(s)
    return s


def finish_setup(code: str) -> bool:
    from . import setup_schema as S
    try:
        pending = PENDING.read_text().strip()
    except OSError:
        return False
    if not verify(code, pending):
        return False
    S.set_env(ENV_NAME, pending)
    raw = S.load_raw()
    raw["two_factor"] = {"enabled": True}
    S._write_raw(raw)
    PENDING.unlink(missing_ok=True)
    return True


def disable(code: str) -> bool:
    from . import setup_schema as S
    if not verify(code):
        return False
    raw = S.load_raw()
    raw["two_factor"] = {"enabled": False}
    S._write_raw(raw)
    return True
