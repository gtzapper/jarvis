"""Update Jarvis from its git repo: check what's new, pull it (fast-forward only), roll back if needed.

In Docker the code is a git checkout mounted at /app; after an update Jarvis exits and Docker's restart
policy starts it again on the new code. No Docker socket is needed."""

import base64
import os
import subprocess
from pathlib import Path

from .audit import audit
from .config import env

APP_DIR = Path(os.getenv("JARVIS_APP_DIR") or Path(__file__).resolve().parents[1])
HISTORY = Path("data/update_history")
IN_DOCKER = os.getenv("JARVIS_IN_DOCKER") == "1"


class UpdateError(Exception):
    pass


def _remote_host() -> str:
    try:
        url = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(APP_DIR), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        url = ""
    from urllib.parse import urlparse
    return urlparse(url).hostname or "github.com"


def diagnose(host: str | None = None) -> str:
    """Why can't Jarvis reach GitHub? Tries each step (DNS, connecting, HTTPS) and names the first that fails."""
    import socket
    import ssl
    import time
    host = host or _remote_host()
    try:
        dns = [l.split()[1] for l in Path("/etc/resolv.conf").read_text().splitlines() if l.startswith("nameserver")]
    except OSError:
        dns = []
    proxy = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY") if os.environ.get(k)), "")
    where = f" (DNS server{'s' if len(dns) > 1 else ''}: {', '.join(dns)})" if dns else ""
    t = time.time()
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except socket.gaierror as e:
        return (f"Jarvis can't look up {host}{where}: {e.strerror or e}. The container's DNS isn't working. Check that "
                f"the server (Unraid) and pfSense can resolve names, or set a DNS server for the container (docker-compose: "
                f"dns: [10.0.20.1, 1.1.1.1]).")
    slow = f" (the lookup took {time.time() - t:.0f} s)" if time.time() - t > 3 else ""
    v4 = [a for a in addrs if ":" not in a] or addrs
    ip = v4[0]
    try:
        s = socket.create_connection((ip, 443), timeout=8)
    except OSError as e:
        return (f"{host} is {ip}{slow}, but Jarvis can't connect to it on port 443: {e.strerror or e}. Something between "
                f"the container and the internet is blocking it: a pfSense rule or VLAN for the Unraid server, pfBlockerNG, "
                f"or the Docker network." + (f" A proxy is set ({proxy}); is it right?" if proxy else ""))
    try:
        s.settimeout(10)
        with ssl.create_default_context().wrap_socket(s, server_hostname=host) as tls:
            tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            first = tls.recv(64).decode(errors="replace").split("\r\n")[0]
    except ssl.SSLCertVerificationError as e:
        return (f"Connected to {host} ({ip}), but its certificate isn't trusted: {e.verify_message}. Something on your network "
                f"is intercepting HTTPS (pfSense/Squid SSL inspection, a filtering proxy): let {host} through untouched.")
    except (OSError, ssl.SSLError) as e:
        return (f"Connected to {host} ({ip}), but the secure (HTTPS) handshake {'timed out' if isinstance(e, TimeoutError) else 'failed: ' + str(e)}. "
                f"When the connection opens but HTTPS stalls it's usually the network's MTU (a VLAN, VPN or PPPoE link "
                f"dropping large packets) or a filter on pfSense. Try MSS clamping on pfSense (Interfaces > WAN > MSS 1452) "
                f"or check pfBlockerNG / Snort / Suricata.")
    finally:
        try:
            s.close()
        except OSError:
            pass
    return (f"The network looks fine right now: DNS, connecting and HTTPS to {host} all work ({first or 'answered'}){slow}. "
            f"It may have been a blip: try again. If it keeps failing, git itself may be stuck on a proxy setting.")


def _git(*args: str, timeout: int = 90, auth: bool = False) -> str:
    cmd = ["git", "-c", "safe.directory=*", "-C", str(APP_DIR)]
    if auth:  # talks to GitHub: give up on a stalled connection after 20 s instead of hanging
        cmd += ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20"]
    token = env("JARVIS_GIT_TOKEN") if auth else None
    if token:  # private repos: a GitHub token with read access to the repo; never written to .git/config
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {header}"]
    try:
        r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        raise UpdateError("git isn't installed here")
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} took too long. Is GitHub reachable?")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        text = msg[-1] if msg else f"git {args[0]} failed"
        if "Authentication failed" in text or "could not read Username" in text or "403" in text:
            text = "GitHub refused the login. For a private repo, add a token in Setup > Updates"
        raise UpdateError(text)
    return r.stdout.rstrip()  # keep leading spaces: git status lines start with one


def _check_repo() -> None:
    if not (APP_DIR / ".git").exists():
        raise UpdateError(f"{APP_DIR} isn't a git checkout, so Jarvis can't update itself. Install it with install.sh (git clone)")


def current() -> dict:
    _check_repo()
    line = _git("log", "-1", "--format=%h%x09%cs%x09%s")
    short, date, subject = (line.split("\t") + ["", "", ""])[:3]
    return {"commit": short, "date": date, "subject": subject, "branch": _git("rev-parse", "--abbrev-ref", "HEAD")}


def check() -> dict:
    """Fetch and report what's waiting. Doesn't change any files."""
    _check_repo()
    try:
        _git("fetch", "--quiet", "origin", auth=True, timeout=60)
    except UpdateError as e:
        if "refused the login" in str(e):
            raise
        raise UpdateError(f"{e}. {diagnose()}") from None
    try:
        upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    except UpdateError:
        raise UpdateError("This branch doesn't track a remote branch (run: git branch -u origin/main)")
    log = _git("log", "--format=%h%x09%cs%x09%s", "HEAD..@{u}")
    new = [dict(zip(("commit", "date", "subject"), l.split("\t"))) for l in log.splitlines() if l]
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD") or 0)
    dirty = [l[3:] for l in _git("status", "--porcelain", "--untracked-files=no").splitlines()]
    return {**current(), "upstream": upstream, "new": new, "ahead": ahead, "dirty": dirty,
            "can_update": bool(new) and not dirty and ahead == 0, "in_docker": IN_DOCKER}


def apply() -> dict:
    """Fast-forward to the latest commit. Refuses if files were edited locally or history has diverged."""
    status = check()
    if not status["new"]:
        return {**status, "updated": False, "message": "Already up to date."}
    if status["dirty"]:
        raise UpdateError("Some files were edited locally (" + ", ".join(status["dirty"][:5]) + "), so I won't overwrite them")
    if status["ahead"]:
        raise UpdateError("This copy has commits that aren't on GitHub, so a simple update isn't possible")
    old = _git("rev-parse", "HEAD")
    changed = _git("diff", "--name-only", "HEAD", "@{u}").splitlines()
    _git("merge", "--ff-only", "@{u}")
    new = _git("rev-parse", "HEAD")
    HISTORY.parent.mkdir(exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(f"{old} {new}\n")
    audit(f"update: {old[:7]} -> {new[:7]} ({len(status['new'])} new commit(s))")
    needs_deps = "requirements.txt" in changed
    needs_rebuild = any(f == "Dockerfile" or f.startswith("docker/") for f in changed)
    if needs_deps and not IN_DOCKER:
        install_requirements()
    message = f"Updated to {new[:7]} ({len(status['new'])} change(s))."
    if needs_rebuild and IN_DOCKER:
        message += " This update also changes the Docker image: run ./update.sh --rebuild on the host when you can."
    return {**current(), "updated": True, "count": len(status["new"]), "needs_deps": needs_deps,
            "needs_rebuild": needs_rebuild, "message": message}


def rollback() -> dict:
    """Go back to the version before the last update."""
    _check_repo()
    lines = HISTORY.read_text(encoding="utf-8").split() if HISTORY.exists() else []
    if len(lines) < 2:
        raise UpdateError("No earlier version recorded to roll back to")
    old, new = lines[-2], lines[-1]
    if _git("rev-parse", "HEAD") != new:
        raise UpdateError("The code has changed since the last update, so I won't roll back automatically")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("Some files were edited locally, so I won't roll back over them")
    _git("reset", "--hard", old)
    HISTORY.write_text(" ".join(lines[:-2]) + ("\n" if len(lines) > 2 else ""), encoding="utf-8")
    audit(f"update: rolled back {new[:7]} -> {old[:7]}")
    return {**current(), "message": f"Rolled back to {old[:7]}."}


def install_requirements() -> None:
    import sys
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(APP_DIR / "requirements.txt")],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise UpdateError("Updated, but installing new requirements failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])


def restart_soon(delay: float = 1.5) -> bool:
    """In Docker, exit so the restart policy brings Jarvis back on the new code. Elsewhere, ask for a manual restart."""
    if not IN_DOCKER:
        return False
    import threading
    threading.Timer(delay, lambda: os._exit(0)).start()
    return True
