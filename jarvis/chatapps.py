"""Talk to Jarvis from Telegram, Discord, Matrix and Signal.

Each chat account is linked to a Jarvis user (Account > Chat apps > Link, then send '/link 123456' to the bot), so a
family member gets their own limited Jarvis and admins get the full one. Messages from chat apps count as coming from
outside home: with 'Admins away from home: limited', reboots, shell commands, updates and deleting stay off. Anything
that changes the lab still asks first, with Yes/No buttons (Telegram, Discord) or a typed yes/no (Matrix, Signal).

Strangers get one short reply an hour and nothing else. All four connect outwards (long polling or a websocket), so no
ports need opening."""

import json
import random
import re
import secrets
import threading
import time
from pathlib import Path

import requests

from . import users
from .audit import audit
from .config import env

APPS = {"telegram": "Telegram", "discord": "Discord", "matrix": "Matrix", "signal": "Signal"}
STATE_FILE = Path("data/chat_state.json")
LINK_TTL = 600            # link codes work for 10 minutes
STALE = 300               # ignore messages that waited longer than this (Jarvis was down): don't reboot hours later
STRANGER_EVERY = 3600     # how often a stranger gets the "I don't know you" reply

YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "go", "do it", "confirm", "approve", "👍"}
CANCEL = object()  # "no" to a pick-one question
NO = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "deny", "👎"}


def _entry(cfg: dict, app: str) -> dict:
    e = (cfg.get("chat") or {}).get(app) or {}
    return e if e.get("enabled", True) else {}


def chunks(text: str, limit: int) -> list[str]:
    """Split a long reply on line breaks so each piece fits the app's message limit."""
    text = (text or "").strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


class ChatError(Exception):
    pass


# ======================================================================== the apps

class Bridge:
    app = ""
    limit = 3900
    has_buttons = False

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.s: dict = {}
        self.handle = ""        # how people find the bot: @name, a Matrix id, a phone number
        self.connected = False
        self.error = ""

    def configured(self, cfg: dict) -> dict | None:
        raise NotImplementedError

    def run(self, s: dict, alive) -> None:
        raise NotImplementedError

    def send(self, to: str, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        raise NotImplementedError

    def target(self, sender: str) -> str | None:
        """Where to send a message to this person when they haven't written first (notifications)."""
        return sender


class Telegram(Bridge):
    app, limit, has_buttons = "telegram", 4000, True

    def configured(self, cfg):
        e = _entry(cfg, "telegram")
        tok = env(e.get("token_env") or "TELEGRAM_BOT_TOKEN") if e else None
        return {"token": tok, "api": (e.get("api_url") or "https://api.telegram.org").rstrip("/")} if tok else None

    def call(self, method: str, http_timeout: int = 20, **params):
        s = self.s
        try:
            r = requests.post(f"{s['api']}/bot{s['token']}/{method}", json=params, timeout=http_timeout)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ChatError(str(e).replace(s["token"], "***")) from None  # the token is in the URL: never log it
        if not j.get("ok"):
            raise ChatError(f"Telegram {method}: {j.get('description') or r.status_code}")
        return j["result"]

    def run(self, s, alive):
        self.s = s
        me = self.call("getMe")
        self.handle = "@" + me.get("username", "")
        self.connected, offset = True, None
        while alive():
            for u in self.call("getUpdates", 40, offset=offset, timeout=25, allowed_updates=["message", "callback_query"]):
                offset = u["update_id"] + 1
                try:
                    self._update(u)
                except Exception as e:
                    print(f"telegram: {e}")

    def _update(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            self.call("answerCallbackQuery", callback_query_id=cq["id"])
            m = cq.get("message") or {}
            if m:
                try:
                    self.call("editMessageReplyMarkup", chat_id=m["chat"]["id"], message_id=m["message_id"], reply_markup={"inline_keyboard": []})
                except ChatError:
                    pass
            self.hub.button(self, str(cq["from"]["id"]), cq.get("data") or "")
            return
        m = u.get("message") or {}
        if not m or (m.get("chat") or {}).get("type") != "private":
            return  # private chats only: in a group everyone would see the replies
        if time.time() - m.get("date", 0) > STALE:
            return
        frm = m.get("from") or {}
        text = m.get("text") or ""
        if not text:
            self.send(str(m["chat"]["id"]), "I only read text messages for now.")
            return
        self.hub.incoming(self, str(frm.get("id")), str(m["chat"]["id"]), text, frm.get("first_name") or frm.get("username") or "")

    def send(self, to, text, buttons=None):
        parts = chunks(text, self.limit) or ["..."]
        for i, part in enumerate(parts):
            extra = {}
            if buttons and i == len(parts) - 1:
                extra["reply_markup"] = {"inline_keyboard": [[{"text": t[:60], "callback_data": d[:64]}] for t, d in buttons]
                                         if len(buttons) > 2 else [[{"text": t, "callback_data": d} for t, d in buttons]]}
            self.call("sendMessage", chat_id=to, text=part, disable_web_page_preview=True, **extra)


class Discord(Bridge):
    app, limit, has_buttons = "discord", 1900, True
    API = "https://discord.com/api/v10"

    def __init__(self, hub):
        super().__init__(hub)
        self.me = None
        self.dm: dict[str, str] = {}

    def configured(self, cfg):
        e = _entry(cfg, "discord")
        tok = env(e.get("token_env") or "DISCORD_BOT_TOKEN") if e else None
        return {"token": tok, "api": (e.get("api_url") or self.API).rstrip("/")} if tok else None

    def rest(self, method: str, path: str, body=None):
        for _ in range(3):
            try:
                r = requests.request(method, self.s["api"] + path, json=body, timeout=15,
                                     headers={"Authorization": f"Bot {self.s['token']}", "User-Agent": "DiscordBot (jarvis, 1.0)"})
            except requests.RequestException as e:
                raise ChatError(f"Discord: {e}") from None
            if r.status_code == 429:
                time.sleep(min(float((r.json() if r.content else {}).get("retry_after", 1)), 10))
                continue
            if r.status_code >= 400:
                raise ChatError(f"Discord {path}: {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise ChatError("Discord: rate limited")

    def run(self, s, alive):
        import websocket
        self.s = s
        url = self.rest("GET", "/gateway/bot")["url"]
        ws = websocket.create_connection(url.rstrip("/") + "/?v=10&encoding=json", timeout=30)
        try:
            hello = json.loads(ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            ws.send(json.dumps({"op": 2, "d": {"token": s["token"], "intents": 1 << 12,  # direct messages only
                                               "properties": {"os": "linux", "browser": "jarvis", "device": "jarvis"}}}))
            ws.settimeout(1)
            seq, beat, acked = None, time.time() + interval * random.random(), True
            while alive():
                if time.time() >= beat:
                    if not acked:
                        raise ChatError("Discord stopped answering heartbeats")
                    ws.send(json.dumps({"op": 1, "d": seq}))
                    beat, acked = time.time() + interval, False
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise ChatError("Discord closed the connection")
                p = json.loads(raw)
                seq = p.get("s") or seq
                if p["op"] == 11:
                    acked = True
                elif p["op"] == 1:
                    ws.send(json.dumps({"op": 1, "d": seq}))
                elif p["op"] in (7, 9):
                    return  # asked to reconnect / session invalid: start over
                elif p["op"] == 0:
                    try:
                        self.dispatch(p.get("t"), p.get("d") or {})
                    except Exception as e:
                        print(f"discord: {e}")
        finally:
            self.connected = False
            try:
                ws.close()
            except Exception:
                pass

    def dispatch(self, t, d):
        if t == "READY":
            self.me = d["user"]["id"]
            self.handle = d["user"].get("username", "")
            self.connected = True
        elif t == "MESSAGE_CREATE":
            a = d.get("author") or {}
            if d.get("guild_id") or a.get("id") == self.me or a.get("bot"):
                return  # direct messages only
            self.dm[a["id"]] = d["channel_id"]
            if d.get("content"):
                self.hub.incoming(self, a["id"], d["channel_id"], d["content"], a.get("global_name") or a.get("username") or "")
        elif t == "INTERACTION_CREATE" and d.get("type") == 3:
            who = (d.get("user") or (d.get("member") or {}).get("user") or {}).get("id")
            try:  # take the buttons off the question
                self.rest("POST", f"/interactions/{d['id']}/{d['token']}/callback", {"type": 7, "data": {"components": []}})
            except ChatError:
                pass
            self.hub.button(self, str(who), (d.get("data") or {}).get("custom_id", ""))

    def send(self, to, text, buttons=None):
        parts = chunks(text, self.limit) or ["..."]
        for i, part in enumerate(parts):
            body = {"content": part, "allowed_mentions": {"parse": []}}
            if buttons and i == len(parts) - 1:
                btns = [{"type": 2, "style": 4 if d.endswith(":0") and t.lower() == "no" else 1, "label": t[:80], "custom_id": d[:100]}
                        for t, d in buttons[:25]]
                body["components"] = [{"type": 1, "components": btns[k:k + 5]} for k in range(0, len(btns), 5)]
            self.rest("POST", f"/channels/{to}/messages", body)

    def target(self, sender):
        if sender not in self.dm:
            self.dm[sender] = self.rest("POST", "/users/@me/channels", {"recipient_id": sender})["id"]
        return self.dm[sender]


class Matrix(Bridge):
    app, limit = "matrix", 16000

    def __init__(self, hub):
        super().__init__(hub)
        self.warned: set[str] = set()

    def configured(self, cfg):
        e = _entry(cfg, "matrix")
        tok = env(e.get("token_env") or "MATRIX_ACCESS_TOKEN") if e else None
        hs = (e.get("homeserver") or "").rstrip("/")
        return {"token": tok, "hs": hs, "verify": e.get("verify_tls", True)} if tok and hs else None

    def call(self, method: str, path: str, body=None, timeout: int = 20, **params):
        try:
            r = requests.request(method, f"{self.s['hs']}/_matrix/client/v3{path}", json=body, params=params or None, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.s['token']}"}, verify=self.s["verify"])
        except requests.RequestException as e:
            raise ChatError(f"Matrix: {e}") from None
        if r.status_code >= 400:
            raise ChatError(f"Matrix {path.split('?')[0]}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def run(self, s, alive):
        self.s = s
        me = self.call("GET", "/account/whoami")["user_id"]
        self.handle = me
        filt = json.dumps({"room": {"timeline": {"limit": 20}, "state": {"lazy_load_members": True}},
                           "presence": {"types": []}, "account_data": {"types": []}})
        since = self.hub.get_state("matrix_since")
        if not since:  # first start: don't answer old messages
            since = self.call("GET", "/sync", filter=json.dumps({"room": {"timeline": {"limit": 0}}}), timeout=60)["next_batch"]
        self.connected = True
        while alive():
            r = self._sync(since, filt)
            since = r.get("next_batch", since)
            self.hub.set_state("matrix_since", since)
            rooms = r.get("rooms") or {}
            for room_id in (rooms.get("invite") or {}):
                try:
                    self.call("POST", f"/rooms/{requests.utils.quote(room_id)}/join", {})
                except ChatError as e:
                    print(f"matrix join: {e}")
            for room_id, room in (rooms.get("join") or {}).items():
                for ev in (room.get("timeline") or {}).get("events") or []:
                    try:
                        self._event(me, room_id, ev)
                    except Exception as e:
                        print(f"matrix: {e}")

    def _sync(self, since, filt):
        try:
            r = requests.get(f"{self.s['hs']}/_matrix/client/v3/sync", timeout=45, verify=self.s["verify"],
                             params={"since": since, "timeout": 25000, "filter": filt}, headers={"Authorization": f"Bearer {self.s['token']}"})
        except requests.RequestException as e:
            raise ChatError(f"Matrix: {e}") from None
        if r.status_code >= 400:
            raise ChatError(f"Matrix sync: {r.status_code} {r.text[:200]}")
        return r.json()

    def _event(self, me, room_id, ev):
        if ev.get("sender") == me or time.time() - ev.get("origin_server_ts", 0) / 1000 > STALE:
            return
        if ev.get("type") == "m.room.encrypted":
            if room_id not in self.warned:
                self.warned.add(room_id)
                self.send(room_id, "This chat is end-to-end encrypted and I can't read it yet. Start a new chat with me with "
                                   "encryption turned off (Element: New room > Show advanced > turn off end-to-end encryption).")
            return
        c = ev.get("content") or {}
        if ev.get("type") == "m.room.message" and c.get("msgtype") in ("m.text", "m.notice") and c.get("body"):
            body = c["body"]
            if "m.relates_to" in c and body.startswith("> "):  # a reply quotes the original: keep only the new text
                body = body.split("\n\n", 1)[-1]
            self.hub.incoming(self, ev["sender"], room_id, body, ev["sender"].split(":")[0].lstrip("@"))

    def send(self, to, text, buttons=None):
        for part in chunks(text, self.limit) or ["..."]:
            txn = secrets.token_hex(8)
            self.call("PUT", f"/rooms/{requests.utils.quote(to)}/send/m.room.message/{txn}", {"msgtype": "m.text", "body": part})

    def target(self, sender):
        return None  # only rooms the person has written in (remembered by the hub)


class Signal(Bridge):
    app, limit = "signal", 6000

    def configured(self, cfg):
        e = _entry(cfg, "signal")
        url, number = (e.get("api_url") or "").rstrip("/"), (e.get("number") or "").strip()
        return {"url": url, "number": number} if url and number else None

    def run(self, s, alive):
        self.s = s
        self.handle = s["number"]
        self.connected = True
        while alive():
            try:
                r = requests.get(f"{s['url']}/v1/receive/{requests.utils.quote(s['number'])}", params={"timeout": 20}, timeout=40)
            except requests.RequestException as e:
                raise ChatError(f"Signal: {e}") from None
            if r.status_code >= 400:
                raise ChatError(f"Signal receive: {r.status_code} {r.text[:200]}")
            for item in r.json() or []:
                e = item.get("envelope") or {}
                dm = e.get("dataMessage") or {}
                if not dm.get("message") or dm.get("groupInfo") or dm.get("groupV2"):
                    continue  # direct text messages only
                if time.time() - (e.get("timestamp") or dm.get("timestamp") or 0) / 1000 > STALE:
                    continue
                sender = e.get("sourceNumber") or e.get("source") or e.get("sourceUuid")
                if sender:
                    try:
                        self.hub.incoming(self, sender, sender, dm["message"], e.get("sourceName") or "")
                    except Exception as ex:
                        print(f"signal: {ex}")
            if not r.json():
                time.sleep(1)

    def send(self, to, text, buttons=None):
        for part in chunks(text, self.limit) or ["..."]:
            try:
                r = requests.post(f"{self.s['url']}/v2/send", json={"message": part, "number": self.s["number"], "recipients": [to]}, timeout=30)
            except requests.RequestException as e:
                raise ChatError(f"Signal: {e}") from None
            if r.status_code >= 400:
                raise ChatError(f"Signal send: {r.status_code} {r.text[:200]}")


BRIDGES = [Telegram, Discord, Matrix, Signal]


# ======================================================================== the hub: people, jobs, approvals

class Hub:
    def __init__(self, state):
        from .webguard import Lockout
        self.state = state
        self.bridges = {b.app: b(self) for b in BRIDGES}
        self.lock = threading.Lock()
        self.codes: dict[str, tuple[str, float]] = {}       # link code -> (user id, expires)
        self.pending: dict[str, object] = {}                # "app:sender" -> job waiting for an answer
        self.strangers: dict[str, float] = {}
        self.lockout = Lockout()
        self._state = self._load_state()
        self.outbox_seen = 0

    # ---------------------------------------------------------------- small persisted state
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def get_state(self, k, default=None):
        return self._state.get(k, default)

    def set_state(self, k, v):
        with self.lock:
            if self._state.get(k) == v:
                return
            self._state[k] = v
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(json.dumps(self._state))
            except OSError as e:
                print(f"chat state: {e}")

    def remember(self, app, sender, to):
        rooms = dict(self._state.get("reply_to") or {})
        if rooms.get(f"{app}:{sender}") != to:
            rooms[f"{app}:{sender}"] = to
            self.set_state("reply_to", rooms)

    # ---------------------------------------------------------------- running
    def start(self):
        from .organizer import last_outbox_id
        self.outbox_seen = last_outbox_id()
        for b in self.bridges.values():
            threading.Thread(target=self._loop, args=(b,), daemon=True, name=f"chat-{b.app}").start()
        threading.Thread(target=self._family_loop, daemon=True, name="chat-family").start()

    def cfg(self) -> dict:
        with self.state.lock:
            return self.state.cfg

    def _loop(self, b: Bridge):
        wait = 5
        while True:
            s = b.configured(self.cfg())
            if not s:
                b.connected, b.error = False, ""
                time.sleep(10)
                continue
            fp = repr(s)
            started = time.time()
            try:
                b.run(s, lambda: repr(b.configured(self.cfg())) == fp)
                b.error = ""
            except Exception as e:
                b.error = str(e)[:300]
                print(f"{b.app}: {b.error}")
            b.connected = False
            wait = 5 if time.time() - started > 120 else min(wait * 2, 300)
            time.sleep(wait)

    def status(self) -> list[dict]:
        cfg = self.cfg()
        return [{"app": a, "label": APPS[a], "configured": bool(b.configured(cfg)), "connected": b.connected,
                 "handle": b.handle, "error": b.error, "buttons": b.has_buttons} for a, b in self.bridges.items()]

    # ---------------------------------------------------------------- linking
    def link_code(self, uid: str) -> str:
        with self.lock:
            now = time.time()
            self.codes = {c: v for c, v in self.codes.items() if v[1] > now and v[0] != uid}
            code = f"{secrets.randbelow(10 ** 6):06d}"
            while code in self.codes:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            self.codes[code] = (uid, now + LINK_TTL)
            return code

    def _link(self, b: Bridge, sender: str, to: str, code: str) -> str:
        key = f"{b.app}:{sender}"
        if self.lockout.blocked(key):
            return "Too many wrong codes. Try again in 15 minutes."
        with self.lock:
            uid, exp = self.codes.get(code, (None, 0))
            if uid and exp > time.time():
                self.codes.pop(code, None)
            else:
                uid = None
        u = users.get(uid) if uid else None
        if not u or u.get("disabled"):
            if self.lockout.fail(key, None, f"wrong {APPS[b.app]} link code"):
                self._security_alert("Chat app lockout", f"Too many wrong link codes from {APPS[b.app]} account {sender}: ignored for 15 minutes.")
            return "That code didn't work. Get a new one in Jarvis: your name at the bottom of the menu > Chat apps > Link."
        for other in users.all_users():  # one Jarvis account per chat account
            if other["id"] != u["id"] and str((other.get("chat") or {}).get(b.app) or "") == sender:
                users.update(other["id"], chat={k: v for k, v in other["chat"].items() if k != b.app})
        users.update(u["id"], chat={**(u.get("chat") or {}), b.app: sender})
        self.remember(b.app, sender, to)
        audit(f"chat: {APPS[b.app]} {sender} linked to {u['username']}")
        family = u.get("role") == "family"
        return (f"Linked to {u['name']}'s Jarvis account. " + ("Ask me about the shopping list, your reminders, or what to watch. "
                if family else "Ask me anything you'd ask in the dashboard. ") + "Say 'help' for ideas.")

    def unlink(self, uid: str, app: str):
        u = users.get(uid)
        if u and app in (u.get("chat") or {}):
            users.update(uid, chat={k: v for k, v in u["chat"].items() if k != app})
            audit(f"chat: {APPS.get(app, app)} unlinked from {u['username']}")

    def _security_alert(self, title, text):
        from . import organizer, webguard
        audit(f"SECURITY: {text}")
        if webguard.settings(self.cfg())["alerts"]:
            organizer.record("security", title, text)
            self.notify(title, text)

    # ---------------------------------------------------------------- incoming
    def incoming(self, b: Bridge, sender: str, to: str, text: str, name: str = ""):
        text = (text or "").strip()
        key = f"{b.app}:{sender}"
        m = re.fullmatch(r"/?link\s+(\d[\d ]{5,7})", text, re.I)
        if m:
            b.send(to, self._link(b, sender, to, m.group(1).replace(" ", "")))
            return
        u = users.find_chat_user(b.app, sender)
        if not u:
            if time.time() - self.strangers.get(key, 0) > STRANGER_EVERY:
                self.strangers[key] = time.time()
                b.send(to, f"Hi{' ' + name if name else ''}. I'm a private assistant and I don't know you yet. If you have a Jarvis "
                           f"account: open it, go to your name at the bottom of the menu > Chat apps > Link, and send me the code it shows.")
            return
        self.remember(b.app, sender, to)
        job = self.pending.get(key)
        if job is not None and job.status == "input":
            ans = self._answer(job.prompt, text)
            if ans is None:
                b.send(to, self._prompt_text(job.prompt, b, again=True))
            else:
                self._give(job, None if ans is CANCEL else ans)
            return
        low = text.lower()
        if low in ("/start", "/help", "start"):
            text = "help"
        elif low in ("/reset", "/new", "reset"):
            text = "new chat"
        threading.Thread(target=self._run, args=(b, u, sender, to, text), daemon=True).start()

    def button(self, b: Bridge, sender: str, data: str):
        job_id, _, value = data.partition(":")
        job = self.state.jobs.get(job_id)
        if not job or getattr(job, "chat_key", None) != f"{b.app}:{sender}" or job.status != "input":
            return
        p = job.prompt or {}
        if p.get("type") == "choose":
            self._give(job, int(value) if value.isdigit() else None)
        elif p.get("code"):
            if value == "0":
                self._give(job, {"yes": False, "code": ""})
        else:
            self._give(job, value == "1")

    @staticmethod
    def _give(job, answer):
        if job.status != "input":
            return
        job.answer = answer
        job.prompt, job.status = None, "running"
        job.answered.set()

    @staticmethod
    def _answer(prompt: dict, text: str):
        """A typed answer to a question, or None if it doesn't fit."""
        t = text.strip().lower().rstrip(".!")
        if prompt.get("type") == "choose":
            if t.isdigit() and 1 <= int(t) <= len(prompt.get("options") or []):
                return int(t) - 1
            return CANCEL if t in NO else None
        if prompt.get("code"):
            digits = re.sub(r"\s", "", t)
            if re.fullmatch(r"\d{6}", digits):
                return {"yes": True, "code": digits}
            return {"yes": False, "code": ""} if t in NO else None
        if t in YES:
            return True
        return False if t in NO else None

    def _prompt_text(self, p: dict, b: Bridge, again: bool = False) -> str:
        head = "Please answer this first:\n" if again else ""
        if p.get("type") == "choose":
            opts = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(p.get("options") or []))
            return f"{head}Which one?\n{opts}" + ("" if b.has_buttons and not again else "\nReply with the number, or 'no'.")
        if p.get("code"):
            return f"{head}{p['question']}\nReply with the 6-digit code from your authenticator app to approve, or 'no'."
        return f"{head}{p['question']}" + ("" if b.has_buttons and not again else "\nReply yes or no.")

    def _buttons(self, job, p: dict, b: Bridge):
        if not b.has_buttons:
            return None
        if p.get("type") == "choose":
            return [(f"{i + 1}. {str(o)[:70]}", f"{job.id}:{i}") for i, o in enumerate(p.get("options") or [])] + [("Cancel", f"{job.id}:x")]
        if p.get("code"):
            return [("No", f"{job.id}:0")]
        return [("Yes", f"{job.id}:1"), ("No", f"{job.id}:0")]

    def _run(self, b: Bridge, u: dict, sender: str, to: str, text: str):
        from . import webguard
        from .web.server import Job
        key = f"{b.app}:{sender}"
        admin = u.get("role") == "admin"
        job = Job(text)
        job.user = None if admin else u
        job.remote = admin and webguard.settings(self.cfg())["remote_admin"] != "full"
        job.origin = f"{u['name']} on {APPS[b.app]}"
        job.chat_key = key
        self.state.jobs[job.id] = job
        self.state.start_job(job)
        sent, asked, last_note = 0, None, time.time()
        try:
            while True:
                lines = job.progress[sent:]
                if lines:
                    sent += len(lines)
                    b.send(to, "\n".join(lines))
                if job.status == "input" and job.prompt is not asked:
                    asked = job.prompt
                    self.pending[key] = job
                    b.send(to, self._prompt_text(asked, b), self._buttons(job, asked, b))
                if job.status == "done":
                    break
                if job.status == "running" and time.time() - last_note > 90 and not lines:
                    last_note = time.time()  # long jobs (updates, research): say we're still on it, once in a while
                    b.send(to, "Still working on it...")
                time.sleep(0.4)
            lines = job.progress[sent:]
            reply = "\n".join([*lines, job.reply or ""]).strip()
            b.send(to, reply or "Done.")
        except ChatError as e:
            print(f"{b.app}: couldn't reply: {e}")
        finally:
            if self.pending.get(key) is job:
                self.pending.pop(key, None)

    # ---------------------------------------------------------------- outgoing
    def notify(self, title: str, text: str, uid: str | None = None):
        """Admins (uid None), or one person, on every chat app they've linked."""
        people = [users.get(uid)] if uid else [u for u in users.all_users() if u.get("role") == "admin"]
        rooms = self._state.get("reply_to") or {}
        for u in people:
            if not u or u.get("disabled"):
                continue
            for app, sender in (u.get("chat") or {}).items():
                b = self.bridges.get(app)
                if not b or not b.connected or not sender:
                    continue
                try:
                    to = rooms.get(f"{app}:{sender}") or b.target(str(sender))
                    if to:
                        b.send(to, f"{title}\n{text}" if title else text)
                except Exception as e:
                    print(f"{app}: couldn't send to {u['username']}: {e}")

    def notify_hook(self, title: str, text: str):
        """Called for every alert/reminder that goes to the owner (see monitor.Notifier)."""
        if self.state.planner_cfg().get("chat", True):
            threading.Thread(target=self.notify, args=(title, text), daemon=True).start()

    def _family_loop(self):
        """A family member's own reminders go to their linked chat apps."""
        while True:
            time.sleep(20)
            try:
                self.deliver_family()
            except Exception as e:
                print(f"chat family reminders: {e}")

    def deliver_family(self):
        from . import organizer
        rows = organizer.user_outbox_after(self.outbox_seen)
        if rows:
            self.outbox_seen = rows[-1]["id"]
        if self.state.planner_cfg().get("chat", True):
            for r in rows:
                self.notify(r["title"], r["text"], uid=r["user"])
