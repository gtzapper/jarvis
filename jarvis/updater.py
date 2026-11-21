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


def _git(*args: str, timeout: int = 90, auth: bool = False) -> str:
    cmd = ["git", "-c", "safe.directory=*", "-C", str(APP_DIR)]
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
    _git("fetch", "--quiet", "origin", auth=True)
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
