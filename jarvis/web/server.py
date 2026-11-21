"""python -m jarvis web: dashboard, chat, devices, activity and setup in the browser.

Security: listens on this PC only unless a password is set. All changes need the session cookie
(SameSite=Strict) plus a custom header, which blocks cross-site requests. Secrets never reach the browser."""

import hmac
import ipaddress
import os
import secrets as pysecrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session

from .. import setup_schema as S
from ..audit import AUDIT_LOG
from ..collectors import build_collectors
from ..commands import Assistant
from ..config import env, load_config
from ..media.clients import MediaStack
from ..monitor import Monitor, spoken_summary

STATIC = Path(__file__).parent / "static"
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
        self.wake.set()

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
            a = self.assistant
            a.confirm = lambda q: bool(ask({"type": "confirm", "question": q}))
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

    @app.before_request
    def guard():
        if request.path in ("/login", "/favicon.ico"):
            return None
        if password and not session.get("ok"):
            return (jsonify(error="login required"), 401) if request.path.startswith("/api/") else redirect("/login")
        if request.method != "GET" and request.headers.get("X-Jarvis") != "1":
            abort(403)  # cross-site forms can't set this header
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password:
            return redirect("/")
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session["ok"] = True
                session.permanent = True
                return redirect("/")
            time.sleep(1.5)  # slow down guessing
        return send_from_directory(STATIC, "login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

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
        for e in entries:
            try:
                for d in PfSenseCollector(e, th).collect_devices():
                    out.append({**d, "source": e["name"]})
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
        state.jobs[job.id] = job
        state.start_job(job)
        return job_view(job)

    @app.get("/api/chat/<job_id>")
    def chat_poll(job_id):
        job = state.jobs.get(job_id) or abort(404)
        return job_view(job)

    @app.post("/api/chat/<job_id>/answer")
    def chat_answer(job_id):
        job = state.jobs.get(job_id) or abort(404)
        if job.status != "input":
            abort(409)
        answer = (request.json or {}).get("answer")
        if job.prompt["type"] == "choose":
            answer = int(answer) if isinstance(answer, int) or str(answer).isdigit() else None
        else:
            answer = answer is True
        job.answer = answer
        job.prompt, job.status = None, "running"
        job.answered.set()
        return job_view(job)

    @app.post("/api/chat/reset")
    def chat_reset():
        with state.job_lock:
            if state.assistant.brain:
                state.assistant.brain.reset()
        return jsonify(ok=True)

    @app.get("/api/ai")
    def ai_info():
        b = state.assistant.brain
        return jsonify(enabled=bool(b), model=b.model if b else None, usage=b.usage if b else None)

    # ------------------------------------------------------------ setup

    @app.get("/api/setup")
    def setup_get():
        pub_path = Path("~/.ssh/jarvis_ed25519.pub").expanduser()
        return jsonify(services=S.public_view(), ssh_public_key=pub_path.read_text().strip() if pub_path.exists() else None)

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

    @app.get("/api/discover")
    def discover():
        from ..discovery import Discovery
        try:
            with state.lock:
                cfg = state.cfg
            return jsonify(ok=True, guests=Discovery(cfg).scan())
        except Exception as e:
            return jsonify(ok=False, error=str(e))

    @app.post("/api/discover/add")
    def discover_add():
        from ..discovery import add_guest
        b = request.json or {}
        try:
            idx, message, ok = add_guest(b.get("name", ""), b.get("ip", ""), b.get("user", "root"))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        state.reload()
        return jsonify(ok=True, index=idx, login_ok=ok, message=message)

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
    if not _is_local(host) and not password:
        raise SystemExit("Refusing to open the dashboard to the network without a password. "
                         "Set JARVIS_WEB_PASSWORD in .env (or in Setup > Web dashboard), or use --host 127.0.0.1.")
    if monitor:
        threading.Thread(target=state.monitor_loop, daemon=True).start()
    from waitress import serve as waitress_serve
    shown = "localhost" if _is_local(host) or host == "0.0.0.0" else host
    print(f"Jarvis dashboard: http://{shown}:{port}" + ("  (also reachable from your network)" if host == "0.0.0.0" else ""))
    waitress_serve(create_app(state, password), host=host, port=port, threads=8)
