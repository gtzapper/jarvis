"""python -m jarvis web: dashboard, chat, devices, activity and setup in the browser.

Security: listens on this PC only unless a password is set. All changes need the session cookie
(SameSite=Strict) plus a custom header, which blocks cross-site requests. Secrets never reach the browser."""

import hmac
import ipaddress
import os
import re
import secrets as pysecrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, request, send_from_directory, session

from .. import setup_schema as S
from .. import modes
from ..audit import AUDIT_LOG, audit
from ..collectors import build_collectors
from ..commands import Assistant
from ..config import env, load_config
from ..media.clients import MediaStack
from ..monitor import Monitor, spoken_summary

STATIC = Path(__file__).parent / "static"
APP_FILES = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript",
             "/icon-192.png": "image/png", "/icon-512.png": "image/png",
             "/themes.css": "text/css", "/theme.js": "text/javascript"}
SECRET_FILE = Path("data/web_secret")


ANSWER_TIMEOUT = 600  # an unanswered question counts as "no" after 10 minutes


class Job:
    def __init__(self, text: str):
        self.id = uuid.uuid4().hex[:12]
        self.text = text
        self.answer = None
        self.answered = threading.Event()
        self.status = "running"   # running | input | done
        self.progress: list[str] = []
        self.prompt: dict | None = None
        self.reply = ""
        self.created = time.time()
        self.origin: str | None = None      # a listening device's name, for requests made by voice
        self.device_id: str | None = None
        self.user: dict | None = None       # who asked (None = the owner, e.g. a listening device)


class State:
    """Everything that depends on config.yaml; rebuilt after Setup saves."""

    def __init__(self):
        self.lock = threading.RLock()
        self.job_lock = threading.Lock()  # one command at a time keeps 'remove download 2' style references sane
        self.jobs: dict[str, Job] = {}
        self.wake = threading.Event()
        self.reload()

    def reload(self):
        with self.lock:
            self.cfg = load_config(allow_missing=True)
            self.stack = MediaStack(self.cfg)
            self.monitor = Monitor(self.cfg, build_collectors(self.cfg, self.stack))
            self.assistant = Assistant(self.cfg, self.stack)
            self.assistant.report_source = self.build_report
            self.assistant.reports_source = lambda: list(self.monitor.reports)
            self.family_assistants: dict[str, Assistant] = {}  # one per family member (their own chat history)
        self.wake.set()

    def assistant_for(self, user: dict | None) -> Assistant:
        """Admins share the owner's assistant; each family member gets a limited one of their own."""
        if not user or user.get("role") == "admin":
            return self.assistant
        with self.lock:
            a = self.family_assistants.get(user["id"])
            if a is None:
                a = Assistant(self.cfg, self.stack, user=user)
                self.family_assistants[user["id"]] = a
            return a

    def monitor_loop(self):
        while True:
            with self.lock:
                mon, interval = self.monitor, self.cfg["interval_seconds"]
            if mon.collectors:
                try:
                    mon.tick()
                except Exception as e:  # keep the dashboard alive whatever happens
                    print(f"monitor error: {e}")
            self.wake.wait(interval)
            self.wake.clear()

    # ------------------------------------------------------------ reminders, bills and the daily report

    def planner_cfg(self) -> dict:
        with self.lock:
            p = dict(self.cfg.get("planner") or {})
        return {"ntfy": p.get("ntfy", True), "app": p.get("app", True), "spoken": p.get("spoken", True),
                "report_time": p.get("report_time", "08:00") if "report_time" in p else "08:00",
                "quiet_hours": p.get("quiet_hours", "22:00-07:00") if "quiet_hours" in p else "22:00-07:00"}

    def build_report(self) -> str:
        from .. import organizer
        with self.lock:
            mon = self.monitor
        reports = mon.reports
        summary = spoken_summary(reports) if reports else None
        alerts = [m for _, m in (mon.events or [])[:10]]
        from .. import life, mail, weather
        with self.lock:
            cfg = self.cfg
        extra = []
        for part in (lambda: weather.report_lines(cfg), lambda: [life.habit_report()], lambda: mail.report_lines(24)):
            try:
                extra += [x for x in part() if x]
            except Exception:
                pass
        shopping = len(life.items("shopping"))
        if shopping:
            extra.append(f"{shopping} thing{'s' if shopping > 1 else ''} on the shopping list.")
        return organizer.daily_report(summary, alerts, extra)

    def docs_loop(self):
        """Pick up documents dropped into data/documents every 10 minutes."""
        from .. import docs
        while True:
            time.sleep(600)
            with self.lock:
                cfg = self.cfg
            if cfg.get("documents") or (cfg.get("email_ai") or {}).get("ollama_url") or (cfg.get("ai") or {}).get("provider") == "ollama":
                try:
                    docs.index(cfg)
                except Exception as e:
                    print(f"documents: {e}")

    def mail_loop(self):
        """Check each email account on its own schedule; tell the person about important mail and bills."""
        from email.utils import parseaddr
        from .. import mail, organizer
        from ..monitor import Notifier
        last: dict[str, float] = {}
        while True:
            with self.lock:
                cfg = self.cfg
            for entry in cfg.get("email") or []:
                if not entry.get("enabled", True) or not entry.get("name"):
                    continue
                every = max(5, int(entry.get("interval_minutes") or 15)) * 60
                if time.time() - last.get(entry["name"], 0) < every:
                    continue
                last[entry["name"]] = time.time()
                try:
                    new = mail.check_account(cfg, entry)
                except Exception as e:
                    print(f"email {entry['name']}: {e}")
                    continue
                p = self.planner_cfg()
                notifier = Notifier(cfg)
                for r in new:
                    for title, text in r.get("events") or []:
                        organizer.record("mail", title, text)
                        if p["ntfy"]:
                            notifier.send(title, text)
                if not entry.get("notify_important", True):
                    continue
                for r in new:
                    who = parseaddr(r["sender"])
                    who = who[0] or who[1]
                    if r["important"]:
                        title, text = "Important email", f"{who}: {r['summary']}"
                    elif r.get("bill"):
                        title, text = "Bill in your email", f"{who}: {r['summary']} Add it to the calendar in Planner."
                    else:
                        continue
                    organizer.record("mail", title, text)
                    if p["ntfy"]:
                        notifier.send(title, text)
            time.sleep(60)

    def scheduler_loop(self):
        """Every 30 seconds: due reminders, bill notices, events starting soon, and the daily report."""
        from .. import organizer
        from ..monitor import Notifier
        while True:
            try:
                p = self.planner_cfg()
                with self.lock:
                    notifier = Notifier(self.cfg)
                from .. import life
                from .. import mail as _mail
                renewals = [("mail", t, x) for t, x in _mail.renewal_notices()] if self.cfg.get("email") else []
                for kind, title, text in organizer.due_now() + life.habit_checkins() + renewals:
                    if p["ntfy"]:
                        notifier.send(title, text)
                sec = (self.cfg.get("security_watch") or {})
                day, at = sec.get("digest_day", "sunday"), sec.get("digest_time", "10:00")
                if day != "off" and time.strftime("%A").lower() == day and organizer.report_due(at, key="digest"):
                    from .. import security
                    with self.lock:
                        reports = list(self.monitor.reports)
                    text = security.digest(self.cfg, reports)
                    organizer.record("report", "Security digest", text)
                    if p["ntfy"]:
                        notifier.send("Security digest", text)
                with self.lock:
                    keep = int((self.cfg.get("location_tracking") or {}).get("keep_days") or 365)
                if organizer.report_due("03:00", key="locprune"):
                    from .. import location
                    location.prune(keep)
                if time.time() - getattr(self, "_media_ready_at", 0) > 600:
                    self._media_ready_at = time.time()
                    with self.lock:
                        stack = self.stack
                    if stack and (stack.arrs):
                        from ..media import insights
                        try:
                            for title, text in insights.check_ready(stack):
                                if p["ntfy"]:
                                    notifier.send(title, text)
                        except Exception as e:
                            print(f"media requests: {e}")
                if organizer.report_due(p["report_time"]):
                    text = self.build_report()
                    organizer.record("report", "Daily report", text)
                    if p["ntfy"]:
                        notifier.send("Daily report", text)
            except Exception as e:  # keep reminders alive whatever happens
                print(f"scheduler error: {e}")
            time.sleep(30)

    # ------------------------------------------------------------ chat jobs

    def start_job(self, job: Job):
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _run_job(self, job: Job):
        def ask(prompt: dict):
            """Pause this command until the person answers in the browser (or 10 minutes pass)."""
            job.answered.clear()
            job.prompt, job.status = prompt, "input"
            got = job.answered.wait(ANSWER_TIMEOUT)
            job.prompt, job.status = None, "running"
            if not got:
                job.progress.append("(No answer after 10 minutes, so I took that as a no.)")
                return None
            return job.answer

        with self.job_lock:
            a = self.assistant_for(job.user)
            a.confirm = lambda q: bool(ask({"type": "confirm", "question": q}))

            def confirm_code(q):
                r = ask({"type": "confirm", "question": q, "code": True})
                return (bool(r.get("yes")), str(r.get("code") or "")) if isinstance(r, dict) else (False, "")
            a.confirm_code = confirm_code
            a.choose = lambda options: ask({"type": "choose", "options": options})
            a.say = lambda line: job.progress.append(line)
            if a.brain:
                a.brain.latest_reports = lambda: self.monitor.reports if self.monitor.updated and (time.time() - self.monitor.updated.timestamp()) < 180 else None
            try:
                job.reply = a.handle(job.text)
            except Exception as e:
                job.reply = f"Something went wrong: {e}"
            job.status = "done"
        # forget old jobs
        cutoff = time.time() - 3600
        for k in [k for k, j in self.jobs.items() if j.created < cutoff]:
            self.jobs.pop(k, None)


def _is_local(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def create_app(state: State, password: str | None) -> Flask:
    app = Flask(__name__, static_folder=None)
    SECRET_FILE.parent.mkdir(exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(pysecrets.token_hex(32))
    app.secret_key = SECRET_FILE.read_text().strip()
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)
    from .. import invites, oidc, users
    users.ensure_owner(password)  # first start with accounts: the dashboard password becomes the 'admin' account

    def auth_required() -> bool:
        return bool(password) or users.any_users() or bool(oidc.providers(state.cfg))

    OWNER = {"id": None, "username": "owner", "name": "Owner", "role": "admin"}  # when no accounts or password exist (local only)

    def current_user() -> dict | None:
        uid = session.get("uid")
        if uid:
            u = users.get(uid)
            return u if u and not u.get("disabled") else None
        if session.get("ok"):  # signed in before accounts existed: that was the owner
            return next((u for u in users.all_users() if u["role"] == "admin" and not u.get("disabled")), OWNER)
        return None if auth_required() else OWNER

    # What a family member may use. Everything else (Setup, the lab, email, approvals...) is admin only.
    FAMILY = [("GET", r"/api/(me|mode|ai|weather|outbox|pending|life|planner|media/watch|media/requests)"),
              ("POST", r"/api/me/password|/logout|/api/chat(/reset)?|/api/chat/[\w-]+/answer|/api/media/request"),
              ("GET", r"/api/chat/[\w-]+"),
              ("POST", r"/api/life/item(/\d+/(check|uncheck|delete))?|/api/life/clear|/api/planner/(note|reminder)(/\d+/delete)?")]

    def family_may(method: str, path: str) -> bool:
        if not path.startswith("/api/") and path != "/logout":
            return True  # the page itself, styles, icons
        return any(method == m and re.fullmatch(rx, path) for m, rx in FAMILY)

    def who() -> str | None:
        """Whose reminders/notes: None for admins (the household), the user id for a family member."""
        u = getattr(g, "user", None)
        return u["id"] if u and u.get("role") == "family" else None

    @app.before_request
    def guard():
        if request.path in ("/login", "/favicon.ico", "/auth/info", "/auth/sso", "/auth/sso/callback") or request.path in APP_FILES:
            return None  # the app icon and manifest hold no data
        if request.path.startswith(("/invite/", "/api/invite/")):
            return None  # an invitation link is its own permission (checked in the handlers)
        if request.path.startswith("/calendar/"):
            return None  # the private calendar feed checks its own secret link
        if request.path == "/api/location/owntracks":
            # OwnTracks signs in with its own tracker code (HTTP Basic password, or a Bearer header), never the dashboard password
            import base64 as b64
            from ..location import tracker_for
            auth = request.headers.get("Authorization", "")
            token = None
            if auth.lower().startswith("basic "):
                try:
                    token = b64.b64decode(auth[6:]).decode().split(":", 1)[1]
                except Exception:
                    token = None
            elif auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            g.tracker = tracker_for(token)
            if not g.tracker:
                return jsonify(error="unknown or removed tracker code"), 401
            return None
        if request.path.startswith("/api/voice/device/"):
            # listening devices sign in with their own access code (Setup > Listening devices), never the dashboard password
            from ..voice import device_for
            auth = request.headers.get("Authorization", "")
            g.device = device_for(auth[7:].strip() if auth.lower().startswith("bearer ") else None)
            if not g.device:
                return jsonify(error="unknown or revoked device code"), 401
            return None
        g.user = current_user()
        if not g.user:
            return (jsonify(error="login required"), 401) if request.path.startswith("/api/") else redirect("/login")
        if request.method != "GET" and request.headers.get("X-Jarvis") != "1" and request.path != "/logout":
            abort(403)  # cross-site forms can't set this header
        if g.user["role"] != "admin" and not family_may(request.method, request.path):
            return jsonify(error="Your account can't use that. Ask the owner."), 403
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def _sign_in(u: dict):
        session.clear()
        session["uid"] = u["id"]
        session.permanent = True

    def password_login_allowed() -> bool:
        acc = state.cfg.get("accounts") or {}
        old = state.cfg.get("sso") if isinstance(state.cfg.get("sso"), dict) else {}
        return acc.get("password_login", (old or {}).get("password_login", True)) is not False

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_required():
            return redirect("/")
        if request.method == "POST":
            if not password_login_allowed() and oidc.providers(state.cfg):
                u0 = users.by_username(request.form.get("username", "")) if request.form.get("username") else None
                if u0 and u0["role"] != "admin":  # family must use single sign-on; admins keep a way in
                    time.sleep(1.5)
                    return redirect("/login?error=sso")
            u = users.authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
            if u and not password_login_allowed() and oidc.providers(state.cfg) and u["role"] != "admin":
                u = None
            if u:
                _sign_in(u)
                audit(f"login: {u['username']} (password)")
                return redirect("/")
            time.sleep(1.5)  # slow down guessing
            return redirect("/login?error=1")
        return send_from_directory(STATIC, "login.html")

    def _sso_buttons():
        return [{"key": p["_key"], "label": p["_label"], "provider": p.get("provider") or "other"} for p in oidc.providers(state.cfg)]

    @app.get("/auth/info")
    def auth_info():
        b = _sso_buttons()
        return jsonify(providers=b, sso=bool(b), sso_name=b[0]["label"] if b else "single sign-on", password_login=password_login_allowed())

    def _public_base() -> str:
        acc = (state.cfg.get("accounts") or {}).get("public_url")
        old = (state.cfg.get("sso") or {}).get("public_url") if isinstance(state.cfg.get("sso"), dict) else None
        return (acc or old or request.host_url).rstrip("/")

    def _redirect_uri() -> str:
        return _public_base() + "/auth/sso/callback"

    @app.get("/auth/sso")
    def sso_start():
        invite = request.args.get("invite") or None
        if invite and not invites.find(invite):
            return _sso_page("That invitation link has expired or was already used. Ask for a new one.", ok=False), 400
        try:
            return redirect(oidc.start(state.cfg, _redirect_uri(), key=request.args.get("p") or None, invite=invite))
        except oidc.SSOError as e:
            return _sso_page(str(e), ok=False), 400

    def _sso_page(msg: str, ok: bool):
        # The provider sends the browser here from another site, so the (SameSite=Strict) cookie only "takes" on a
        # same-site navigation: this page hops to the dashboard itself.
        import html as _h
        body = (f'<p>{_h.escape(msg)}</p><script>location.replace("/")</script>' if ok else
                f'<p>{_h.escape(msg)}</p><p><a href="/login">Back to the login page</a></p>')
        return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Jarvis</title>'
                f'<link rel="stylesheet" href="/themes.css"><body style="font:15px system-ui;padding:24px;background:var(--bg);color:var(--text)">{body}</body>')

    def _free_username(wanted: str) -> str:
        base = re.sub(r"[^a-z0-9._-]", "", (wanted or "user").lower())[:28] or "user"
        base = base if len(base) > 1 else base + "user"
        name, n = base, 1
        while users.by_username(name):
            n += 1
            name = f"{base}{n}"
        return name

    @app.get("/auth/sso/callback")
    def sso_callback():
        if request.args.get("error"):
            return _sso_page(f"Sign-in was cancelled or refused ({request.args.get('error_description') or request.args['error']}).", ok=False), 400
        try:
            claims, prov, pend = oidc.finish(state.cfg, request.args.get("state", ""), request.args.get("code", ""))
        except oidc.SSOError as e:
            return _sso_page(str(e), ok=False), 400
        sub, email = f"{prov['_key']}|{claims.get('sub')}", (claims.get("email") or "").lower()
        verified = claims.get("email_verified", True) is not False
        role = oidc.role_for(prov, claims)
        inv = invites.find(pend.get("invite")) if pend.get("invite") else None
        if pend.get("invite") and not inv:
            return _sso_page("That invitation link has expired or was already used. Ask for a new one.", ok=False), 400
        # who is this? the same provider account, or an account with this verified email
        u = next((x for x in users.all_users() if x.get("oidc_sub") in (sub, str(claims.get("sub")))), None)
        if not u and email and verified:
            u = next((x for x in users.all_users() if (x.get("email") or "").lower() == email), None)
        if u and u.get("oidc_sub") != sub:
            u = users.update(u["id"], oidc_sub=sub)  # link it
        if not u:
            if inv:
                role = inv["role"]  # the invitation decides, whatever the groups say
            if not role:
                audit(f"SSO login refused for {email or sub}: not in an allowed group and not invited")
                return _sso_page("Your account isn't allowed into Jarvis. Ask the owner for an invitation (or to be put in the Jarvis group).", ok=False), 403
            name = _free_username(claims.get("preferred_username") or email.split("@")[0])
            u = users.create(name, claims.get("name") or (inv or {}).get("name") or name, role, None, email or (inv or {}).get("email", ""), sub)
            if inv:
                users.update(u["id"], invited=True)
            audit(f"SSO: created {role} account {name} for {email or sub}" + (f" (invited by {inv['invited_by']})" if inv else ""))
        elif oidc.decides_roles(prov) and role and role != u["role"]:
            try:
                u = users.update(u["id"], role=role)  # the provider's groups are the source of truth
            except ValueError:
                pass
        elif oidc.decides_roles(prov) and not role and u["role"] != "admin" and not u.get("invited") and not inv:
            audit(f"SSO login refused for {u['username']}: no longer in an allowed group")
            return _sso_page("Your account is no longer allowed into Jarvis.", ok=False), 403
        if inv:
            invites.use(pend["invite"], u["id"])
        if u.get("disabled"):
            return _sso_page("This account is switched off. Ask the owner.", ok=False), 403
        users.update(u["id"], last_login=time.strftime("%Y-%m-%dT%H:%M"))
        _sign_in(u)
        audit(f"login: {u['username']} (single sign-on, {prov['_label']})")
        return _sso_page("Signed in. Opening Jarvis...", ok=True)

    # ------------------------------------------------------------ invitations

    @app.get("/invite/<token>")
    def invite_page(token):
        return send_from_directory(STATIC, "invite.html")

    @app.get("/api/invite/<token>")
    def invite_info(token):
        inv = invites.find(token)
        if not inv:
            return jsonify(ok=False, error="This invitation has expired or was already used. Ask for a new one."), 404
        return jsonify(ok=True, email=inv["email"], name=inv.get("name") or "", role=inv["role"], invited_by=inv.get("invited_by"),
                       providers=_sso_buttons(), password=password_login_allowed() or not oidc.providers(state.cfg) or inv["role"] == "admin",
                       suggested=_free_username(inv["email"].split("@")[0]))

    @app.post("/api/invite/<token>")
    def invite_accept(token):
        if request.headers.get("X-Jarvis") != "1":
            abort(403)
        inv = invites.find(token)
        if not inv:
            return jsonify(ok=False, error="This invitation has expired or was already used. Ask for a new one.")
        if not (password_login_allowed() or not oidc.providers(state.cfg) or inv["role"] == "admin"):
            return jsonify(ok=False, error="Please use the single sign-on button.")
        b = request.json or {}
        if len(b.get("password") or "") < 8:
            return jsonify(ok=False, error="Pick a password of at least 8 characters.")
        try:
            u = users.create(b.get("username", ""), b.get("name") or inv.get("name") or "", inv["role"], b["password"], inv["email"])
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        if not invites.use(token, u["id"]):  # someone was faster with the same link
            users.delete(u["id"])
            return jsonify(ok=False, error="This invitation was just used. Ask for a new one.")
        users.update(u["id"], invited=True, last_login=time.strftime("%Y-%m-%dT%H:%M"))
        _sign_in(u)
        audit(f"invitation accepted: {u['username']} ({u['role']}, invited by {inv.get('invited_by')})")
        return jsonify(ok=True)

    @app.get("/api/invites")
    def invites_list():
        return jsonify(invites=[invites.public(i) for i in reversed(invites.all_invites())][:50],
                       smtp=bool(invites.smtp_settings(state.cfg)))

    def _send_invite(email: str, role: str, name: str):
        days = int((state.cfg.get("accounts") or {}).get("invite_days") or 7)
        inviter = g.user.get("name") or g.user.get("username") or "The owner"
        inv, token = invites.create(email, role, name, inviter, days)
        link = f"{_public_base()}/invite/{token}"
        expires = invites.public(inv)["expires"]
        try:
            invites.send_mail(state.cfg, inv["email"], *invites.invite_email(link, inviter, role, inv.get("name"), expires))
            invites.mark(inv["id"], sent=time.strftime("%Y-%m-%d %H:%M"), error=None)
            audit(f"invitation sent to {inv['email']} ({role})")
            return jsonify(ok=True, sent=True, invite=invites.public({**inv, "sent": True}))
        except invites.InviteError as e:
            invites.mark(inv["id"], error=str(e))
            audit(f"invitation for {inv['email']} not emailed: {e}")
            # the admin can still pass the link on themselves (shown once, never stored)
            return jsonify(ok=True, sent=False, error=str(e), link=link, invite=invites.public({**inv, "error": str(e)}))

    @app.post("/api/invites")
    def invites_create():
        b = request.json or {}
        try:
            return _send_invite(b.get("email", ""), b.get("role", "family"), b.get("name", ""))
        except invites.InviteError as e:
            return jsonify(ok=False, error=str(e))

    @app.post("/api/invites/<inv_id>/<action>")
    def invites_action(inv_id, action):
        inv = next((i for i in invites.all_invites() if i["id"] == inv_id), None) or abort(404)
        if action == "revoke":
            invites.mark(inv_id, revoked=True)
            audit(f"invitation for {inv['email']} revoked")
            return jsonify(ok=True)
        if action == "resend":  # a fresh link (the old one stops working)
            return _send_invite(inv["email"], inv["role"], inv.get("name", ""))
        abort(404)

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        u = g.user
        return jsonify(user=users.public(u) if u.get("id") else {**OWNER, "has_password": False}, auth=auth_required(),
                       roles=users.ROLES)

    @app.post("/api/me/password")
    def me_password():
        b = request.json or {}
        u = g.user
        if not u.get("id"):
            return jsonify(ok=False, error="Create an admin account first (Setup > Users).")
        if u.get("pw") and not users.check_password(b.get("current", ""), u["pw"]):
            time.sleep(1)
            return jsonify(ok=False, error="Your current password is wrong.")
        try:
            users.update(u["id"], password=b.get("new", ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        audit(f"password changed: {u['username']}")
        return jsonify(ok=True)

    @app.get("/api/users")
    def users_list():
        return jsonify(users=[users.public(u) for u in users.all_users()], roles=users.ROLES, sso=bool(oidc.providers(state.cfg)),
                       smtp=bool(invites.smtp_settings(state.cfg)))

    @app.post("/api/users")
    def users_create():
        b = request.json or {}
        try:
            u = users.create(b.get("username", ""), b.get("name", ""), b.get("role", "family"), b.get("password") or None, b.get("email", ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        audit(f"user created: {u['username']} ({u['role']})")
        return jsonify(ok=True, user=users.public(u))

    @app.post("/api/users/<uid>")
    def users_change(uid):
        b = request.json or {}
        allowed = {k: b[k] for k in ("name", "role", "email", "disabled", "password", "username") if k in b}
        if "chat" in b and isinstance(b["chat"], dict):
            allowed["chat"] = {k: str(v).strip() for k, v in b["chat"].items() if k in ("telegram", "discord", "matrix") and str(v).strip()}
        if b.get("unlink_sso"):
            allowed["oidc_sub"] = None
        try:
            u = users.update(uid, **allowed)
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        state.family_assistants.pop(uid, None)  # pick up a role or name change
        audit(f"user changed: {u['username']} ({', '.join(k if k != 'password' else 'password reset' for k in allowed)})")
        return jsonify(ok=True, user=users.public(u))

    @app.post("/api/users/<uid>/delete")
    def users_delete(uid):
        if g.user.get("id") == uid:
            return jsonify(ok=False, error="You can't delete your own account while signed in to it.")
        try:
            u = users.get(uid)
            users.delete(uid)
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        state.family_assistants.pop(uid, None)
        audit(f"user deleted: {u['username'] if u else uid}")
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC, "icon-192.png", mimetype="image/png")

    @app.get("/<path:name>")
    def app_file(name):
        """Installable-app files: manifest, offline service worker, icons."""
        if "/" + name not in APP_FILES:
            abort(404)
        resp = send_from_directory(STATIC, name, mimetype=APP_FILES["/" + name])
        if name == "sw.js":
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ------------------------------------------------------------ dashboard

    @app.get("/api/status")
    def status():
        with state.lock:
            mon = state.monitor
            reports = mon.reports
            configured = bool(mon.collectors)
        problems = [p for r in reports for p in r.problems]
        return jsonify(
            configured=configured,
            updated=mon.updated.strftime("%H:%M:%S") if mon.updated else None,
            summary=spoken_summary(reports) if reports else ("Checking..." if configured else "Nothing set up yet. Open Setup to add your first service."),
            counts={"systems": len(reports), "critical": sum(p.severity == "critical" for p in problems),
                    "warning": sum(p.severity == "warning" for p in problems)},
            reports=[asdict(r) for r in reports],
            events=[{"time": t, "text": m} for t, m in mon.events[:50]],
        )

    @app.post("/api/refresh")
    def refresh():
        state.wake.set()
        return jsonify(ok=True)

    @app.get("/api/devices")
    def devices():
        from ..collectors.pfsense import PfSenseCollector
        out = []
        with state.lock:
            entries = list(state.cfg.get("pfsense") or [])
            th = state.cfg["thresholds"]
        from ..network import device_names
        names = device_names()
        for e in entries:
            try:
                for d in PfSenseCollector(e, th).collect_devices():
                    out.append({**d, "source": e["name"], "name": names.get(d["mac"].lower(), "")})
            except Exception as ex:
                return jsonify(error=f"{e.get('name')}: {ex}", devices=out)
        return jsonify(devices=out)

    @app.get("/api/activity")
    def activity():
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-300:] if AUDIT_LOG.exists() else []
        return jsonify(lines=list(reversed(lines)))

    # ------------------------------------------------------------ chat

    def job_view(job: Job):
        return jsonify(id=job.id, status=job.status, progress=job.progress, prompt=job.prompt, reply=job.reply)

    @app.post("/api/chat")
    def chat():
        text = (request.json or {}).get("text", "").strip()
        if not text:
            abort(400)
        job = Job(text)
        job.user = g.user if g.user.get("role") == "family" else None
        job.origin = None if not g.user.get("id") or g.user["role"] == "admin" else g.user.get("name")
        state.jobs[job.id] = job
        state.start_job(job)
        return job_view(job)

    def _my_job(job_id) -> Job:
        job = state.jobs.get(job_id) or abort(404)
        mine = (job.user or {}).get("id")
        if g.user["role"] != "admin" and mine != g.user.get("id"):
            abort(404)  # family members only see their own conversations
        return job

    @app.get("/api/chat/<job_id>")
    def chat_poll(job_id):
        return job_view(_my_job(job_id))

    @app.post("/api/chat/<job_id>/answer")
    def chat_answer(job_id):
        job = _my_job(job_id)
        if job.status != "input":
            abort(409)
        answer = (request.json or {}).get("answer")
        if job.prompt["type"] == "choose":
            answer = int(answer) if isinstance(answer, int) or str(answer).isdigit() else None
        elif job.prompt.get("code"):
            answer = {"yes": answer is True, "code": str((request.json or {}).get("code") or "")}
        else:
            answer = answer is True
        job.answer = answer
        job.prompt, job.status = None, "running"
        job.answered.set()
        return job_view(job)

    @app.post("/api/chat/reset")
    def chat_reset():
        with state.job_lock:
            a = state.assistant_for(g.user if g.user.get("role") == "family" else None)
            if a.brain:
                a.brain.reset()
        return jsonify(ok=True)

    @app.route("/api/mode", methods=["GET", "POST"])
    def mode():
        from .. import modes
        from ..audit import audit
        if request.method == "POST":
            if g.user["role"] != "admin":
                return jsonify(error="Only an admin can change the mode."), 403
            try:
                new = modes.set_mode((request.json or {}).get("mode", ""))
            except ValueError as e:
                return jsonify(error=str(e)), 400
            audit(f"mode set to {new} (dashboard)")
        current = modes.get(state.cfg)
        return jsonify(mode=current, description=modes.MODES[current], modes=modes.MODES)

    @app.get("/api/ai")
    def ai_info():
        b = state.assistant.brain
        return jsonify(enabled=bool(b), model=b.model if b else None, usage=b.usage if b else None)

    # ------------------------------------------------------------ setup

    @app.get("/api/setup")
    def setup_get():
        pub_path = Path("~/.ssh/jarvis_ed25519.pub").expanduser()
        base = ((state.cfg.get("accounts") or {}).get("public_url") or request.host_url).rstrip("/")
        return jsonify(services=S.public_view(), ssh_public_key=pub_path.read_text().strip() if pub_path.exists() else None,
                       sso_presets=oidc.PRESETS, sso_redirect=base + "/auth/sso/callback")

    @app.post("/api/setup/<sid>")
    def setup_save(sid):
        if sid not in S.BY_ID:
            abort(404)
        body = request.json or {}
        try:
            idx = S.save(sid, body.get("values") or {}, body.get("index"))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        state.reload()
        result = {"ok": True, "index": idx}
        if body.get("test") and S.BY_ID[sid].test:
            try:
                result["test"] = {"ok": True, "message": S.test(sid, idx)}
            except Exception as e:
                result["test"] = {"ok": False, "message": str(e)}
        return jsonify(result)

    @app.post("/api/setup/<sid>/test")
    def setup_test(sid):
        try:
            return jsonify(ok=True, message=S.test(sid, int((request.json or {}).get("index", 0))))
        except Exception as e:
            return jsonify(ok=False, message=str(e))

    @app.post("/api/setup/<sid>/delete")
    def setup_delete(sid):
        if sid not in S.BY_ID:
            abort(404)
        S.remove(sid, int((request.json or {}).get("index", 0)))
        state.reload()
        return jsonify(ok=True)

    # ------------------------------------------------------------ voice: listening devices ("Hey Jarvis")

    def voice_view(job: Job, client, heard: str, with_audio: bool):
        """What a listening device gets back: what Jarvis heard, the state, and the spoken answer as WAV (base64)."""
        import base64
        from ..monitor import Notifier
        out = {"job_id": job.id, "heard": heard, "status": {"running": "running", "input": "waiting", "done": "done"}[job.status]}
        text = ""
        if job.status == "input":
            if not getattr(job, "notified", False):
                job.notified = True
                q = (job.prompt or {}).get("question", "").replace("[destructive] ", "").replace("[sends] ", "").split("\n")[0]
                with state.lock:
                    Notifier(state.cfg).send("Jarvis needs your OK", f"{job.origin}: {q or job.text}. Open Jarvis to answer.")
            text = "That needs your OK. I've put the question on your phone and the dashboard."
        elif job.status == "done":
            text = job.reply
        out["reply"] = text
        if with_audio and text:
            try:
                out["audio"] = base64.b64encode(client.speak(text)).decode()
            except Exception as e:
                out["audio_error"] = str(e)
        return jsonify(out)

    def voice_client():
        from ..voice import VoiceClient
        with state.lock:
            cfg = state.cfg
        v = cfg.get("voice")
        if not v or not v.get("enabled", True):
            abort(jsonify(error="Voice isn't set up in Jarvis yet (Setup > Voice)"), 503)
        return VoiceClient(cfg)

    @app.get("/api/voice/device/hello")
    def voice_hello():
        v = state.cfg.get("voice") or {}
        return jsonify(ok=True, device=g.device["name"], voice_ready=bool(v) and v.get("enabled", True))

    @app.post("/api/voice/device/turn")
    def voice_turn():
        """One spoken request: WAV in, Jarvis's spoken answer out. Long jobs return 'running'; poll /job/<id>."""
        from ..voice import VoiceError
        audio = request.get_data(cache=False)
        if not audio:
            return jsonify(error="no audio"), 400
        if len(audio) > 15 * 1024 * 1024:
            return jsonify(error="audio too long"), 413
        client = voice_client()
        try:
            heard = client.transcribe(audio)
        except VoiceError as e:
            return jsonify(error=str(e)), 502
        if not heard:
            return jsonify(status="empty", heard="", reply="")
        job = Job(heard)
        job.origin, job.device_id = g.device["name"], g.device["id"]
        state.jobs[job.id] = job
        state.start_job(job)
        deadline = time.time() + 60
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.3)
        return voice_view(job, client, heard, with_audio=job.status != "running")

    @app.get("/api/voice/device/job/<job_id>")
    def voice_job(job_id):
        job = state.jobs.get(job_id)
        if not job or getattr(job, "device_id", None) != g.device["id"]:
            abort(404)
        return voice_view(job, voice_client(), job.text, with_audio=job.status in ("done", "input") and request.args.get("speak") == "1")

    @app.get("/api/voice/devices")
    def voice_devices():
        from ..voice import list_devices
        return jsonify(devices=list_devices())

    @app.post("/api/voice/devices")
    def voice_device_add():
        from ..audit import audit
        from ..voice import add_device
        try:
            d = add_device((request.json or {}).get("name", ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        audit(f"voice: added listening device {d['name']}")
        return jsonify(ok=True, **d)

    @app.post("/api/voice/devices/<device_id>/delete")
    def voice_device_delete(device_id):
        from ..audit import audit
        from ..voice import remove_device
        remove_device(device_id)
        audit(f"voice: removed listening device {device_id}")
        return jsonify(ok=True)

    # ------------------------------------------------------------ planner: reminders, notes, events and bills

    def _quiet(window: str) -> bool:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", window or "")
        if not m:
            return False
        a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
        lt = time.localtime()
        t = lt.tm_hour * 60 + lt.tm_min
        return (a <= t < b) if a <= b else (t >= a or t < b)

    @app.get("/calendar/<token>.ics")
    def calendar_feed(token):
        from .. import organizer
        if not organizer.feed_enabled() or not hmac.compare_digest(token, organizer.feed_token()):
            abort(404)
        return app.response_class(organizer.ics(), mimetype="text/calendar", headers={"Cache-Control": "no-cache"})

    @app.get("/api/planner")
    def planner():
        from .. import organizer as O
        days = max(1, min(int(request.args.get("days", 31)), 400))
        if who():
            return jsonify(upcoming=[{**i, "text": O.describe(i)} for i in O.upcoming(days, user=who())], reminders=O.reminders(user=who()),
                           events=[], notes=O.notes(limit=200, user=who()),
                           feed=None, feed_enabled=False, repeats=O.REPEATS, family=True)
        return jsonify(upcoming=[{**i, "text": O.describe(i)} for i in O.upcoming(days)], reminders=O.reminders(),
                       events=O.events(), notes=O.notes(limit=200), feed=f"/calendar/{O.feed_token()}.ics",
                       feed_enabled=O.feed_enabled(), repeats=O.REPEATS)

    @app.post("/api/planner/feed")
    def planner_feed_toggle():
        from .. import organizer as O
        on = bool((request.json or {}).get("enabled"))
        return _planner_change(lambda: O.set_feed_enabled(on) or on, "calendar link " + ("published" if on else "unpublished"))

    @app.get("/api/planner/feed/qr.svg")
    def planner_feed_qr():
        """A QR code of the subscription link, to scan with the phone's camera (webcal:// opens the calendar app)."""
        import io
        import segno
        from .. import organizer as O
        if not O.feed_enabled():
            abort(404)
        url = request.host_url.rstrip("/") + f"/calendar/{O.feed_token()}.ics"
        url = "webcal://" + url.split("://", 1)[1] if request.args.get("webcal") == "1" else url
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff")
        return app.response_class(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})

    def _planner_change(fn, what: str):
        from ..audit import audit
        try:
            result = fn()
        except (ValueError, KeyError, TypeError) as e:
            return jsonify(ok=False, error=str(e))
        audit(f"planner: {what}")
        return jsonify(ok=True, result=result)

    @app.post("/api/planner/note")
    def planner_note():
        from .. import organizer as O
        text = (request.json or {}).get("text", "")
        return _planner_change(lambda: O.add_note(text, user=who()), f"note added: {text[:60]}")

    @app.post("/api/planner/reminder")
    def planner_reminder():
        from datetime import datetime as dt
        from .. import organizer as O
        b = request.json or {}
        return _planner_change(lambda: O.add_reminder(b.get("text", ""), dt.fromisoformat(b["due"]), b.get("repeat", ""), user=who()),
                               f"reminder added: {b.get('text', '')[:60]} at {b.get('due')}")

    @app.post("/api/planner/event")
    def planner_event():
        from datetime import date as d_
        from .. import organizer as O
        b = request.json or {}
        amount = float(b["amount"]) if str(b.get("amount") or "").strip() else None
        rd = int(b["remind_days"]) if str(b.get("remind_days") or "").strip() else None
        return _planner_change(lambda: O.add_event(b.get("title", ""), d_.fromisoformat(b["day"]), b.get("kind", "event"),
                                                   b.get("time") or "", amount, b.get("repeat", ""), rd, b.get("notes", "")),
                               f"{b.get('kind', 'event')} added: {b.get('title', '')[:60]} on {b.get('day')}")

    @app.post("/api/planner/<kind>/<int:item_id>/delete")
    def planner_delete(kind, item_id):
        from .. import organizer as O
        fn = {"note": lambda i: O.delete_note(i, user=who()), "reminder": lambda i: O.cancel_reminder(i, user=who()),
              "event": O.delete_event}.get(kind) or abort(404)
        return _planner_change(lambda: fn(item_id), f"{kind} {item_id} deleted")

    @app.post("/api/planner/event/<int:item_id>/paid")
    def planner_paid(item_id):
        from .. import organizer as O
        return _planner_change(lambda: O.mark_paid(item_id), f"bill {item_id} marked paid")

    @app.post("/api/planner/feed/reset")
    def planner_feed_reset():
        from .. import organizer as O
        return _planner_change(lambda: f"/calendar/{O.reset_feed_token()}.ics", "calendar link replaced")

    # ------------------------------------------------------------ email screening

    @app.get("/api/mail")
    def mail_view():
        from .. import mail
        hours = max(1, min(int(request.args.get("hours", 72)), 24 * 30))
        with state.lock:
            accounts = [{"name": e.get("name"), "address": e.get("address"), "provider": e.get("provider"), "enabled": e.get("enabled", True),
                         "sending": bool(e.get("allow_sending"))}
                        for e in state.cfg.get("email") or []]
            model = mail.local_model(state.cfg)
        return jsonify(messages=mail.recent(hours), accounts=accounts, states=mail.states(), local_model=model[1] if model else None,
                       packages=mail.packages(), subscriptions=mail.subscriptions())

    # ------------------------------------------------------------ media: watch next, cleanup, requests

    def _media_err(e):
        return jsonify(ok=False, error=str(e) if isinstance(e, LookupError) else f"{type(e).__name__}: {e}"[:300])

    @app.get("/api/media/watch")
    def media_watch():
        from ..media import insights
        try:
            mins = request.args.get("minutes")
            return jsonify(ok=True, **insights.suggestions(state.stack, request.args.get("kind", "any"), int(mins) if mins else None,
                                                           request.args.get("genre") or None, limit=12))
        except Exception as e:
            return _media_err(e)

    @app.get("/api/media/cleanup")
    def media_cleanup():
        from ..media import insights
        try:
            with state.lock:
                cfg = state.cfg
            return jsonify(ok=True, **insights.cleanup(state.stack, cfg))
        except Exception as e:
            return _media_err(e)

    @app.post("/api/media/keep")
    def media_keep():
        from ..media import insights
        d = request.get_json(silent=True) or {}
        insights.keep(str(d["key"]), str(d.get("title") or ""))
        return jsonify(ok=True)

    @app.get("/api/media/requests")
    def media_requests():
        from ..media import insights
        return jsonify(requests=insights.recent_requests(30), can_request={k: bool(state.stack.for_kind(k)) for k in ("movie", "show")})

    @app.post("/api/media/request")
    def media_request_new():
        from ..media import insights
        d = request.get_json(silent=True) or {}
        try:
            name = g.user.get("name") if who() else (str(d.get("who") or "you")[:40])
            rid, text = insights.add_request(state.stack, name, d.get("kind", "movie"), str(d.get("title", ""))[:120], uid=who())
            return jsonify(ok=True, id=rid, result=text)
        except Exception as e:
            return _media_err(e)

    @app.post("/api/media/requests/<int:rid>")
    def media_request_decide(rid):
        """Approving adds the title to Radarr/Sonarr: a tap in the dashboard is the owner's OK."""
        from ..media import insights
        d = request.get_json(silent=True) or {}
        if modes.get(state.cfg) == "read-only":
            return jsonify(ok=False, error="Read-only mode is on.")
        try:
            text = insights.decide(state.stack, rid, bool(d.get("approve")))
            audit(f"media request {rid}: {text}")
            return jsonify(ok=True, result=text)
        except Exception as e:
            return _media_err(e)

    @app.post("/api/mail/package/<int:pid>/dismiss")
    def mail_package_dismiss(pid):
        from .. import mail
        mail.dismiss_package(pid)
        return jsonify(ok=True)

    @app.post("/api/mail/subscription/<int:sid>/cancelled")
    def mail_subscription_cancelled(sid):
        from .. import mail
        mail.set_subscription(sid, bool((request.get_json(silent=True) or {}).get("cancelled", True)))
        return jsonify(ok=True)

    @app.post("/api/mail/check")
    def mail_check_now():
        from .. import mail
        with state.lock:
            cfg = state.cfg
        results = []
        for entry in cfg.get("email") or []:
            if not entry.get("enabled", True):
                continue
            try:
                results.append({"account": entry["name"], "new": len(mail.check_account(cfg, entry))})
            except Exception as e:
                results.append({"account": entry.get("name"), "error": str(e)})
        return jsonify(ok=True, results=results)

    @app.post("/api/mail/<int:mail_id>/bill")
    def mail_bill(mail_id):
        from .. import mail
        b = request.json or {}
        amount = float(b["amount"]) if str(b.get("amount") or "").strip() else None
        return _planner_change(lambda: mail.bill_to_calendar(mail_id, b.get("title") or None, amount, b.get("due") or None),
                               f"bill from email {mail_id} added to the calendar")

    @app.post("/api/mail/<int:mail_id>/dismiss")
    def mail_dismiss(mail_id):
        from .. import mail
        return _planner_change(lambda: mail.dismiss_bill(mail_id), f"bill from email {mail_id} dismissed")

    ms_flows: dict[str, dict] = {}

    @app.post("/api/mail/oauth/start")
    def mail_oauth_start():
        """Outlook: Microsoft's device sign-in. Shows a code; the person enters it at microsoft.com/devicelogin."""
        from .. import mail
        idx = int((request.json or {}).get("index", -1))
        accounts = S.load_raw().get("email") or []
        if not 0 <= idx < len(accounts):
            return jsonify(ok=False, error="Save the Outlook account first")
        entry = accounts[idx]
        if not entry.get("client_id"):
            return jsonify(ok=False, error="Enter your Microsoft app (client) ID and save first (README: Outlook)")
        try:
            dev = mail.ms_device_start(entry["client_id"], bool(entry.get("allow_sending")))
        except mail.MailError as e:
            return jsonify(ok=False, error=str(e))
        flow_id = uuid.uuid4().hex[:10]
        flow = ms_flows[flow_id] = {"status": "waiting", "error": None}

        def poll():
            interval, deadline = int(dev.get("interval", 5)), time.time() + int(dev.get("expires_in", 900))
            while time.time() < deadline:
                time.sleep(interval)
                j = mail.ms_device_poll(entry["client_id"], dev["device_code"])
                if j.get("error") == "authorization_pending":
                    continue
                if j.get("error") == "slow_down":
                    interval += 5
                    continue
                if "refresh_token" in j:
                    from ..audit import audit
                    name = "EMAIL_MS_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", str(entry.get("name", idx)).upper())
                    S.set_env(name, j["refresh_token"])
                    raw = S.load_raw()
                    raw["email"][idx]["oauth_env"] = name
                    S._write_raw(raw)
                    audit(f"email: signed in to Microsoft for {entry.get('name')}")
                    state.reload()
                    flow["status"] = "done"
                    return
                flow.update(status="failed", error=j.get("error_description", j.get("error", "unknown error"))[:200])
                return
            flow.update(status="failed", error="The code expired. Try again.")

        threading.Thread(target=poll, daemon=True).start()
        return jsonify(ok=True, flow=flow_id, code=dev["user_code"], url=dev.get("verification_uri", "https://microsoft.com/devicelogin"))

    @app.get("/api/mail/oauth/<flow_id>")
    def mail_oauth_status(flow_id):
        return jsonify(ms_flows.get(flow_id) or abort(404))

    # ------------------------------------------------------------ lists, habits, memory, weather

    @app.get("/api/life")
    def life_view():
        from .. import life
        if who():  # family: the shared lists, not the owner's habits and memories
            return jsonify(items=life.items(include_done=True), lists=life.lists(), habits=[], memories=[])
        return jsonify(items=life.items(include_done=True), lists=life.lists(), habits=life.habits(), memories=life.memories())

    @app.post("/api/life/item")
    def life_add_item():
        from .. import life
        b = request.json or {}
        return _planner_change(lambda: life.add_items(b.get("list") or "shopping", life.split_items(b.get("text", ""))),
                               f"list {b.get('list')}: added {b.get('text', '')[:60]}")

    @app.post("/api/life/item/<int:item_id>/<action>")
    def life_item_action(item_id, action):
        from .. import life
        fn = {"check": lambda: life.check(None, str(item_id)), "uncheck": lambda: life.uncheck(item_id),
              "delete": lambda: life.remove(None, str(item_id))}.get(action) or abort(404)
        return _planner_change(fn, f"list item {item_id} {action}")

    @app.post("/api/life/clear")
    def life_clear():
        from .. import life
        lst = (request.json or {}).get("list")
        return _planner_change(lambda: life.clear_done(lst), f"cleared done items{' from ' + lst if lst else ''}")

    @app.post("/api/life/habit")
    def life_add_habit():
        from .. import life
        b = request.json or {}
        return _planner_change(lambda: life.add_habit(b.get("name", ""), b.get("time", ""), b.get("days") or "daily"), f"habit added: {b.get('name')}")

    @app.post("/api/life/habit/<int:habit_id>/<action>")
    def life_habit_action(habit_id, action):
        from .. import life
        fn = {"done": lambda: life.mark_habit(habit_id), "delete": lambda: life.delete_habit(habit_id)}.get(action) or abort(404)
        return _planner_change(fn, f"habit {habit_id} {action}")

    @app.post("/api/life/memory/<int:memory_id>/delete")
    def life_forget(memory_id):
        from .. import life
        return _planner_change(lambda: life.forget(str(memory_id)), f"memory {memory_id} forgotten")

    @app.get("/api/weather")
    def weather_view():
        from .. import weather
        with state.lock:
            loc = dict(state.cfg.get("location") or {})
        if not loc.get("home"):
            return jsonify(ok=False, error="No location set")
        units = loc.get("units") or "metric"
        try:
            data = weather.forecast(loc["home"], units)
            return jsonify(ok=True, today=weather.describe_day(data, 0, units), tomorrow=weather.describe_day(data, 1, units))
        except weather.WeatherError as e:
            return jsonify(ok=False, error=str(e))

    # ------------------------------------------------------------ two-factor approval

    @app.get("/api/2fa")
    def twofa_state():
        from .. import totp
        return jsonify(enabled=totp.enabled(state.cfg))

    @app.post("/api/2fa/setup")
    def twofa_setup():
        import base64
        import io
        import segno
        from .. import totp
        secret = totp.start_setup()
        buf = io.BytesIO()
        segno.make(totp.uri(secret), error="m").save(buf, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff")
        return jsonify(ok=True, secret=secret, qr="data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode())

    @app.post("/api/2fa/enable")
    def twofa_enable():
        from .. import totp
        from ..audit import audit
        if not totp.finish_setup((request.json or {}).get("code", "")):
            return jsonify(ok=False, error="That code didn't match. Check the time on your phone and try the current code.")
        audit("2FA: two-factor approval turned on")
        state.reload()
        return jsonify(ok=True)

    @app.post("/api/2fa/disable")
    def twofa_disable():
        from .. import totp
        from ..audit import audit
        if not totp.disable((request.json or {}).get("code", "")):
            return jsonify(ok=False, error="Wrong code. Turning two-factor off needs a current code.")
        audit("2FA: two-factor approval turned off")
        state.reload()
        return jsonify(ok=True)

    # ------------------------------------------------------------ documents and receipts

    @app.get("/api/docs")
    def docs_list():
        from .. import docs
        return jsonify(documents=docs.documents())

    @app.post("/api/docs/upload")
    def docs_upload():
        from .. import docs
        from ..audit import audit
        f = request.files.get("file")
        if not f:
            return jsonify(ok=False, error="No file")
        data = f.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024:
            return jsonify(ok=False, error="That file is over 50 MB")
        with state.lock:
            cfg = state.cfg
        try:
            if request.form.get("receipt") == "1":
                info = docs.add_receipt(cfg, f.filename, data)
                audit(f"documents: receipt added ({info.get('merchant')})")
                return jsonify(ok=True, receipt=info)
            p = docs.save_upload(f.filename, data)
            r = docs.index(cfg, only=p)
        except (ValueError, docs.DocsError) as e:
            return jsonify(ok=False, error=str(e))
        audit(f"documents: added {p.name}")
        return jsonify(ok=True, result=r, name=p.name)

    @app.post("/api/docs/ask")
    def docs_ask():
        from .. import docs
        q = (request.json or {}).get("question", "").strip()
        if not q:
            return jsonify(ok=False, error="Ask a question")
        with state.lock:
            cfg = state.cfg
        try:
            return jsonify(ok=True, **docs.ask(cfg, q))
        except docs.DocsError as e:
            return jsonify(ok=False, error=str(e))

    @app.post("/api/docs/<int:doc_id>/delete")
    def docs_delete(doc_id):
        from .. import docs
        return _planner_change(lambda: docs.delete(doc_id), f"document {doc_id} deleted")

    @app.post("/api/docs/<int:doc_id>/warranty")
    def docs_warranty(doc_id):
        from .. import docs
        return _planner_change(lambda: docs.warranty_to_calendar(doc_id, (request.json or {}).get("ends") or None),
                               f"warranty from document {doc_id} added to the calendar")

    @app.post("/api/docs/reindex")
    def docs_reindex():
        from .. import docs
        with state.lock:
            cfg = state.cfg
        try:
            return jsonify(ok=True, result=docs.index(cfg))
        except docs.DocsError as e:
            return jsonify(ok=False, error=str(e))

    # ------------------------------------------------------------ location

    def _location_fired(fired):
        if fired and state.planner_cfg()["ntfy"]:
            from ..monitor import Notifier
            with state.lock:
                n = Notifier(state.cfg)
            for _, title, text in fired:
                n.send(title, text)

    @app.post("/api/location/owntracks")
    def owntracks_in():
        """OwnTracks HTTP mode: one message or a list. Answers with an empty list, as OwnTracks expects."""
        from .. import location
        body = request.get_json(silent=True)
        msgs = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
        for m in msgs:
            if m.get("_type") != "location" or "lat" not in m or "lon" not in m:
                continue
            try:
                fired = location.add_point(g.tracker["name"], float(m["lat"]), float(m["lon"]), m.get("tst"), m.get("acc"), m.get("batt"),
                                           m.get("vel"))
                _location_fired(fired)
            except (ValueError, TypeError):
                continue
        return jsonify([])

    @app.post("/api/location/app")
    def location_from_app():
        """The Jarvis Android app's own location sharing (signed in like the dashboard)."""
        from .. import location
        b = request.json or {}
        try:
            fired = location.add_point(f"Jarvis app ({str(b.get('device') or 'phone')[:30]})", float(b["lat"]), float(b["lon"]),
                                       b.get("t"), b.get("acc"), b.get("batt"), b.get("speed"))
        except (KeyError, ValueError, TypeError) as e:
            return jsonify(ok=False, error=f"bad location: {e}"), 400
        _location_fired(fired)
        return jsonify(ok=True)

    @app.get("/api/location/day")
    def location_day():
        from datetime import date as d_
        from .. import location
        day = d_.fromisoformat(request.args.get("date") or d_.today().isoformat())
        summary = location.day_summary(day)
        summary["stays"] = [{**s, "from": time.strftime("%H:%M", time.localtime(s["start"])), "to": time.strftime("%H:%M", time.localtime(s["end"]))}
                            for s in summary["stays"]]
        return jsonify(day=summary, text=location.describe_day(day), places=location.places(), latest=location.latest(),
                       trackers=location.trackers(), suggestions=location.suggest_places(), reminders=location.location_reminders(),
                       week=location.week_summary())

    @app.post("/api/location/places")
    def location_place_add():
        from .. import location
        b = request.json or {}
        return _planner_change(lambda: location.add_place(b.get("name", ""), float(b["lat"]), float(b["lon"]), b.get("kind", "other"),
                                                          float(b.get("radius") or 150)), f"place added: {b.get('name')}")

    @app.post("/api/location/places/<int:place_id>")
    def location_place_update(place_id):
        from .. import location
        b = request.json or {}
        if b.get("delete"):
            return _planner_change(lambda: location.delete_place(place_id), f"place {place_id} deleted")
        return _planner_change(lambda: location.update_place(place_id, b.get("name"), b.get("kind"),
                                                             float(b["radius"]) if b.get("radius") else None), f"place {place_id} updated")

    @app.post("/api/location/trackers")
    def location_tracker_add():
        import base64
        import io
        import segno
        from .. import location
        from ..audit import audit
        b = request.json or {}
        try:
            t = location.add_tracker(b.get("name", ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        base = request.host_url.rstrip("/")
        link = location.owntracks_config(base, {"name": t["name"]}, t["token"])
        buf = io.BytesIO()
        segno.make(link, error="l").save(buf, kind="svg", scale=4, border=2, dark="#000000", light="#ffffff")
        audit(f"location: tracker added: {t['name']}")
        return jsonify(ok=True, name=t["name"], token=t["token"], url=base + "/api/location/owntracks", link=link,
                       qr="data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode())

    @app.post("/api/location/trackers/<tracker_id>/delete")
    def location_tracker_delete(tracker_id):
        from .. import location
        keep = not (request.json or {}).get("delete_history")
        return _planner_change(lambda: location.remove_tracker(tracker_id, delete_history=not keep), f"location tracker {tracker_id} removed")

    @app.post("/api/location/history/delete")
    def location_history_delete():
        from .. import location
        days = (request.json or {}).get("days")
        return _planner_change(lambda: location.delete_history(int(days) if days else None),
                               "location history deleted" + (f" (last {days} days)" if days else " (all)"))

    @app.get("/api/report")
    def report():
        return jsonify(text=state.build_report())

    @app.get("/api/outbox")
    def app_outbox():
        """Reminders, bill notices and reports for the Jarvis app to show as notifications."""
        from .. import organizer as O
        after = request.args.get("after", type=int)
        p = state.planner_cfg()
        if after is None:  # first contact: start from now, no backlog
            return jsonify(items=[], last=O.last_outbox_id(), enabled=p["app"])
        return jsonify(items=O.outbox(after, user=who()) if p["app"] else [], last=O.last_outbox_id(), enabled=p["app"])

    @app.get("/api/voice/device/announcements")
    def device_announcements():
        """Reminders and the daily report, spoken by listening devices (outside quiet hours)."""
        import base64
        from .. import organizer as O
        after = request.args.get("after", type=int)
        p = state.planner_cfg()
        last = O.last_outbox_id()
        if after is None or not p["spoken"] or _quiet(p["quiet_hours"]):
            return jsonify(items=[], last=last)
        from datetime import datetime as dt, timedelta
        fresh = dt.now() - timedelta(minutes=15)  # a device that was off doesn't read out hours-old reminders
        items = [i for i in O.outbox(after, limit=20) if dt.fromisoformat(i["at"]) >= fresh][:5]
        client = voice_client()
        out = []
        for it in items:
            text = it["text"] if it["kind"] in ("report", "reminder") else f"{it['title']}. {it['text']}"
            if it["kind"] == "reminder":
                text = f"Reminder: {it['text']}"
            entry = {"id": it["id"], "kind": it["kind"], "text": text}
            try:
                entry["audio"] = base64.b64encode(client.speak(text)).decode()
            except Exception as e:
                entry["audio_error"] = str(e)
            out.append(entry)
        return jsonify(items=out, last=last)

    @app.get("/api/pending")
    def pending():
        """Questions waiting for an answer, e.g. a reboot asked for by voice: shown in the dashboard and the app."""
        mine = lambda j: g.user["role"] == "admin" or (j.user or {}).get("id") == g.user.get("id")
        return jsonify(pending=[{"id": j.id, "text": j.text, "origin": getattr(j, "origin", None), "prompt": j.prompt,
                                 "progress": j.progress, "status": j.status}
                                for j in list(state.jobs.values()) if j.status == "input" and mine(j)])

    @app.post("/api/media-discover")
    def media_discover():
        from .. import media_discovery
        with state.lock:
            cfg = state.cfg
        try:
            return jsonify(ok=True, **media_discovery.scan(cfg))
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}")

    @app.post("/api/media-discover/add")
    def media_discover_add():
        from .. import media_discovery
        from ..audit import audit
        b = request.json or {}
        try:
            service, message = media_discovery.add(str(b.get("id", "")), str(b.get("secret") or ""), str(b.get("username") or ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        audit(f"setup: added {service} from media discovery")
        state.reload()
        return jsonify(ok=True, service=service, message=message)

    @app.get("/api/discover")
    def discover():
        from ..discovery import Discovery
        try:
            with state.lock:
                cfg = state.cfg
            d = cfg.get("discovery") or {}
            from .. import setup_schema as sch
            defaults = {"username_mode": d.get("username_mode", "name"), "default_user": d.get("default_user", "root"),
                        "default_auth": d.get("default_auth", "key"),
                        "default_password_set": bool(sch.secrets().get(d.get("default_password_env", "DISCOVERY_PASSWORD")))}
            return jsonify(ok=True, guests=Discovery(cfg).scan(), defaults=defaults)
        except Exception as e:
            return jsonify(ok=False, error=str(e))

    @app.post("/api/discover/add")
    def discover_add():
        from ..discovery import add_guest
        b = request.json or {}
        try:
            idx, message, ok = add_guest(b.get("name", ""), b.get("ip", ""), b.get("user", "root"), os_type=b.get("os", "linux"),
                                         auth=b.get("auth", "key"), password=b.get("password", ""),
                                         use_default_password=bool(b.get("use_default_password")))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        state.reload()
        return jsonify(ok=True, index=idx, login_ok=ok, message=message)

    @app.post("/api/discover/defaults")
    def discover_defaults():
        from ..discovery import save_defaults
        b = request.json or {}
        try:
            save_defaults(b.get("username_mode", "name"), b.get("default_user", "root"),
                          b.get("default_auth", "key"), b.get("default_password", ""))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        state.reload()
        return jsonify(ok=True)

    @app.get("/api/windows-setup")
    def windows_setup():
        from ..collectors.windows import SETUP_SNIPPET
        pub = Path("~/.ssh/jarvis_ed25519.pub").expanduser()
        if not pub.exists():
            return jsonify(ok=False, error="Create Jarvis's SSH key first (the SSH key box above)")
        return jsonify(ok=True, snippet=SETUP_SNIPPET.replace("__KEY__", pub.read_text().strip()))

    @app.post("/api/discover/must_run")
    def discover_must_run():
        from ..discovery import set_must_run
        b = request.json or {}
        try:
            ids = set_must_run(b["source"], int(b["vmid"]), bool(b.get("on")))
        except (ValueError, KeyError) as e:
            return jsonify(ok=False, error=str(e)), 400
        state.reload()
        return jsonify(ok=True, must_run=ids)

    @app.get("/api/update")
    def update_status():
        from .. import updater
        try:
            if request.args.get("check"):
                return jsonify(ok=True, **updater.check())
            return jsonify(ok=True, **updater.current(), in_docker=updater.IN_DOCKER)
        except updater.UpdateError as e:
            return jsonify(ok=False, error=str(e))

    @app.post("/api/update")
    def update_apply():
        from .. import updater
        action = (request.json or {}).get("action")
        try:
            result = updater.rollback() if action == "rollback" else updater.apply()
        except updater.UpdateError as e:
            return jsonify(ok=False, error=str(e))
        changed = result.get("updated", action == "rollback")
        restarting = changed and updater.restart_soon()
        return jsonify(ok=True, restarting=restarting, **result)

    @app.post("/api/sshkey")
    def sshkey():
        pub, created = S.ensure_ssh_key()
        return jsonify(public_key=pub, created=created)

    return app


def serve(host: str | None = None, port: int | None = None, monitor: bool = True) -> None:
    state = State()
    web = state.cfg.get("web") or {}
    host = host or web.get("host", "127.0.0.1")
    port = port or int(web.get("port", 8765))
    password = env(web.get("password_env", "JARVIS_WEB_PASSWORD")) or env("JARVIS_WEB_PASSWORD")
    if not _is_local(host) and not password and os.getenv("JARVIS_GENERATE_PASSWORD") == "1":
        # Docker: the dashboard has to listen on the network, so make a password on first start rather than refuse.
        password = pysecrets.token_urlsafe(12)
        S.set_env("JARVIS_WEB_PASSWORD", password)
        print("=" * 70)
        print(f"First start: your dashboard password is  {password}")
        print("It's saved in /config/.env as JARVIS_WEB_PASSWORD. Change it any time in Setup > Web dashboard.")
        print("=" * 70, flush=True)
    from .. import oidc as _oidc
    from .. import users as _users
    if not _is_local(host) and not password and not _users.any_users() and not _oidc.providers(state.cfg):
        raise SystemExit("Refusing to open the dashboard to the network without a password. "
                         "Set JARVIS_WEB_PASSWORD in .env (or in Setup > Web dashboard), or use --host 127.0.0.1.")
    if monitor:
        threading.Thread(target=state.monitor_loop, daemon=True).start()
    threading.Thread(target=state.scheduler_loop, daemon=True).start()
    threading.Thread(target=state.mail_loop, daemon=True).start()
    threading.Thread(target=state.docs_loop, daemon=True).start()
    from waitress import serve as waitress_serve
    shown = "localhost" if _is_local(host) or host == "0.0.0.0" else host
    print(f"Jarvis dashboard: http://{shown}:{port}" + ("  (also reachable from your network)" if host == "0.0.0.0" else ""))
    waitress_serve(create_app(state, password), host=host, port=port, threads=8)
