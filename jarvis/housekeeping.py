"""Housekeeping on your machines: OS updates, Docker container updates, restarts, cleanup and reboots.

Everything here changes things, so the chat commands that call it always confirm first and write to the
audit log. Linux only for installing updates; Windows hosts can be checked."""

import base64
import shlex

from .collectors.ssh_host import auth_method, run_command
from .config import env

UPDATE_TIMEOUT = 1800   # big upgrades and image pulls can take a while


class HousekeepingError(Exception):
    pass


# ---------------------------------------------------------------- running as root

def _elevated(host: dict, script: str) -> tuple[str, str | None]:
    """Wrap a script so it runs as root: directly if we log in as root, else through sudo.
    Returns (command, stdin). With password login the password is fed to sudo -S on stdin."""
    b64 = base64.b64encode(script.encode()).decode()
    inner = f'sh -c "$(echo {b64} | base64 -d)"'
    if host.get("user", "root") == "root":
        return inner, None
    if auth_method(host) == "password":
        return f"sudo -S -p '' {inner}", (env(host.get("password_env")) or "") + "\n"
    return f"sudo -n {inner}", None


def _run(host: dict, script: str, timeout: int = UPDATE_TIMEOUT, root: bool = True) -> str:
    if host.get("os") == "windows":
        raise HousekeepingError(f"{host['name']} is Windows: installing updates and managing containers there isn't supported yet")
    if root:
        command, stdin = _elevated(host, script)
    else:
        command, stdin = f'sh -c "$(echo {base64.b64encode(script.encode()).decode()} | base64 -d)"', None
    code, out = run_command(host, command, timeout=timeout, stdin_data=stdin)
    if code is None:
        raise HousekeepingError(f"{host['name']} didn't finish within {timeout // 60} minutes")
    low = out.lower()
    if "a password is required" in low or "a terminal is required" in low:
        raise HousekeepingError(f"{host.get('user')} on {host['name']} needs passwordless sudo for this "
                                "(or log in as root, or use password login so Jarvis can answer sudo)")
    if "not in the sudoers" in low or "incorrect password" in low or "sorry, try again" in low:
        raise HousekeepingError(f"sudo was refused on {host['name']}: {host.get('user')} isn't allowed to run commands as root")
    return out


def _markers(out: str, tag: str) -> list[str]:
    prefix = f"##{tag} "
    return [l[len(prefix):].strip() for l in out.splitlines() if l.startswith(prefix)]


def _tail(out: str, n: int = 6) -> str:
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("##")]
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- OS packages

CHECK_PACKAGES = r"""
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
if [ -f /etc/unraid-version ]; then echo "##PM unraid"
elif command -v apt-get >/dev/null 2>&1; then
  echo "##PM apt"
  command -v pveversion >/dev/null 2>&1 && echo "##PROXMOX yes"
  apt-get update -qq >/dev/null 2>&1 || echo "##WARN couldn't refresh the package lists"
  apt-get -s -o Debug::NoLocking=1 upgrade --with-new-pkgs 2>/dev/null | awk '/^Inst /{print "##PKG "$2}'
elif command -v dnf >/dev/null 2>&1; then
  echo "##PM dnf"
  dnf -q check-update 2>/dev/null | awk 'NF==3 && $1 ~ /\./ {print "##PKG "$1}'
elif command -v yum >/dev/null 2>&1; then
  echo "##PM yum"
  yum -q check-update 2>/dev/null | awk 'NF==3 && $1 ~ /\./ {print "##PKG "$1}'
elif command -v apk >/dev/null 2>&1; then
  echo "##PM apk"
  apk update -q >/dev/null 2>&1
  apk list -u 2>/dev/null | awk '{print "##PKG "$1}'
elif command -v pacman >/dev/null 2>&1; then
  echo "##PM pacman"
  if command -v checkupdates >/dev/null 2>&1; then checkupdates 2>/dev/null | awk '{print "##PKG "$1}'
  else echo "##WARN install pacman-contrib so updates can be checked safely"; fi
elif command -v zypper >/dev/null 2>&1; then
  echo "##PM zypper"
  zypper -q -n refresh >/dev/null 2>&1
  zypper -q -n lu 2>/dev/null | awk -F'|' 'NF>4 && $1 !~ /S/ {gsub(/ /,"",$3); if ($3!="Name") print "##PKG "$3}'
else echo "##PM none"; fi
[ -f /var/run/reboot-required ] && echo "##REBOOT yes"
exit 0
"""

APPLY = {
    "apt": "apt-get -y -q -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade --with-new-pkgs",
    "apt-proxmox": "apt-get -y -q -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade",
    "dnf": "dnf -y -q upgrade",
    "yum": "yum -y -q update",
    "apk": "apk upgrade --no-progress",
    "pacman": "pacman -Syu --noconfirm",
    "zypper": "zypper -n -q update",
}
AFTER_APPLY = r"""
[ -f /var/run/reboot-required ] && echo "##REBOOT yes"
if command -v needs-restarting >/dev/null 2>&1; then needs-restarting -r >/dev/null 2>&1; [ $? -eq 1 ] && echo "##REBOOT yes"; fi
exit 0
"""


def check_packages(host: dict) -> dict:
    if host.get("os") == "windows":
        return {"pm": "windows", "packages": [], "reboot": False, "warnings": [],
                "note": "Windows: waiting updates are shown in the dashboard; install them with Windows Update on the machine"}
    out = _run(host, CHECK_PACKAGES, timeout=600)
    pm = (_markers(out, "PM") or ["none"])[0]
    result = {"pm": pm, "proxmox": bool(_markers(out, "PROXMOX")), "packages": _markers(out, "PKG"),
              "reboot": bool(_markers(out, "REBOOT")), "warnings": _markers(out, "WARN"), "note": ""}
    if pm == "unraid":
        result["note"] = "Unraid OS updates are installed from its web UI (Tools > Update OS)"
    elif pm == "none":
        result["note"] = "No package manager Jarvis knows (apt, dnf, yum, apk, pacman, zypper)"
    return result


def apply_packages(host: dict, pm: str, proxmox: bool = False) -> dict:
    key = "apt-proxmox" if pm == "apt" and proxmox else pm  # Proxmox wants dist-upgrade
    if key not in APPLY:
        raise HousekeepingError(f"Jarvis can't install updates with '{pm}' on {host['name']}")
    out = _run(host, f'export DEBIAN_FRONTEND=noninteractive LC_ALL=C\n{APPLY[key]}\necho "##EXIT $?"\n{AFTER_APPLY}')
    code = (_markers(out, "EXIT") or ["?"])[0]
    if code != "0":
        detail = _tail(out)
        if "could not get lock" in out.lower():
            detail = "another update is already running (automatic updates?). Try again in a few minutes"
        raise HousekeepingError(f"OS update failed: {detail}")
    return {"reboot": bool(_markers(out, "REBOOT")), "output": _tail(out, 3)}


# ---------------------------------------------------------------- Docker containers

LIST_CONTAINERS = r"""
docker info >/dev/null 2>&1 || { echo "##NODOCKER"; exit 0; }
docker ps -a --format '##C {{.Names}}|{{.State}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.project.config_files"}}|{{.Label "net.unraid.docker.managed"}}'
"""


def _docker_needs_root(host: dict) -> bool:
    """Use docker directly if this user may (docker group); otherwise go through sudo."""
    if host.get("user", "root") == "root":
        return False
    out = _run(host, "docker info >/dev/null 2>&1 && echo '##OK yes'; exit 0", timeout=60, root=False)
    return not _markers(out, "OK")


def list_containers(host: dict) -> list[dict]:
    root = _docker_needs_root(host)
    out = _run(host, LIST_CONTAINERS, timeout=120, root=root)
    if "##NODOCKER" in out:
        raise HousekeepingError(f"Docker isn't running on {host['name']}, or {host.get('user')} can't use it")
    containers = []
    for line in _markers(out, "C"):
        name, state, project, service, workdir, files, unraid = (line.split("|") + [""] * 7)[:7]
        containers.append({"name": name, "state": state, "project": project, "service": service,
                           "workdir": workdir, "files": [f for f in files.split(",") if f], "unraid": bool(unraid)})
    return containers


def _pick(containers: list[dict], names: list[str] | None) -> list[dict]:
    if not names:
        return containers
    wanted = {n.strip().lower() for n in names if n.strip()}
    chosen = [c for c in containers if c["name"].lower() in wanted or c["service"].lower() in wanted]
    missing = wanted - {c["name"].lower() for c in chosen} - {c["service"].lower() for c in chosen}
    if missing:
        known = ", ".join(sorted(c["name"] for c in containers)) or "none"
        raise HousekeepingError(f"No container called {', '.join(sorted(missing))}. Containers there: {known}")
    return chosen


def check_containers(host: dict, names: list[str] | None = None) -> list[dict]:
    """Pull each container's image (downloads only; running containers aren't touched) and compare."""
    containers = _pick(list_containers(host), names)
    if not containers:
        return []
    root = _docker_needs_root(host)
    loop = " ".join(shlex.quote(c["name"]) for c in containers)
    script = f"""
for c in {loop}; do
  ref=$(docker inspect -f '{{{{.Config.Image}}}}' "$c" 2>/dev/null) || {{ echo "##R $c|missing|"; continue; }}
  cur=$(docker inspect -f '{{{{.Image}}}}' "$c")
  if docker pull -q "$ref" >/dev/null 2>&1; then
    new=$(docker image inspect -f '{{{{.Id}}}}' "$ref" 2>/dev/null)
    if [ "$new" = "$cur" ]; then echo "##R $c|current|$ref"; else echo "##R $c|update|$ref"; fi
  else echo "##R $c|pullfail|$ref"; fi
done
exit 0
"""
    status = {}
    for line in _markers(_run(host, script, root=root), "R"):
        name, st, ref = (line.split("|") + ["", ""])[:3]
        status[name] = (st, ref)
    for c in containers:
        c["status"], c["image"] = status.get(c["name"], ("unknown", ""))
    return containers


def apply_containers(host: dict, containers: list[dict]) -> list[dict]:
    """Recreate containers whose new image is already pulled. Compose-managed ones are recreated with
    docker compose; others (plain docker run, Unraid) keep running and are reported as needing a manual step."""
    root = _docker_needs_root(host)
    results = []
    groups: dict[tuple, list[dict]] = {}
    for c in containers:
        if c.get("status") != "update":
            continue
        if c["project"] and c["service"] and c["workdir"]:
            groups.setdefault((c["project"], c["workdir"], tuple(c["files"])), []).append(c)
        else:
            how = "Unraid's Docker tab (apply update)" if c["unraid"] else "however you originally created it"
            results.append({"name": c["name"], "result": "manual", "detail": f"new image downloaded; recreate it from {how}"})
    for (project, workdir, files), members in groups.items():
        fflags = " ".join(f"-f {shlex.quote(f)}" for f in files)
        services = " ".join(shlex.quote(c["service"]) for c in members)
        script = f"""
cd {shlex.quote(workdir)} || {{ echo "##EXIT 1"; exit 0; }}
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
res=$($DC -p {shlex.quote(project)} {fflags} up -d --no-deps {services} 2>&1); rc=$?
echo "$res" | tail -n 5
echo "##EXIT $rc"
exit 0
"""
        out = _run(host, script, root=root)
        ok = (_markers(out, "EXIT") or ["1"])[0] == "0"
        for c in members:
            results.append({"name": c["name"], "result": "updated" if ok else "failed",
                            "detail": "" if ok else _tail(out, 3)})
    return results


def restart_container(host: dict, name: str) -> str:
    container = _pick(list_containers(host), [name])[0]
    out = _run(host, f'docker restart {shlex.quote(container["name"])} >/dev/null 2>&1; echo "##EXIT $?"; exit 0',
               timeout=300, root=_docker_needs_root(host))
    if (_markers(out, "EXIT") or ["1"])[0] != "0":
        raise HousekeepingError(f"Restarting {container['name']} failed")
    return container["name"]


def docker_cleanup(host: dict) -> str:
    script = r"""
docker info >/dev/null 2>&1 || { echo "##NODOCKER"; exit 0; }
docker image prune -af 2>/dev/null | awk '/Total reclaimed space/{print "##IMAGES "$NF}'
docker builder prune -f 2>/dev/null | awk '/Total/{print "##BUILD "$NF}'
exit 0
"""
    out = _run(host, script, timeout=900, root=_docker_needs_root(host))
    if "##NODOCKER" in out:
        raise HousekeepingError(f"Docker isn't running on {host['name']}")
    images = (_markers(out, "IMAGES") or ["0B"])[0]
    build = (_markers(out, "BUILD") or ["0B"])[0]
    return f"Freed {images} of unused images and {build} of build cache on {host['name']}."


# ---------------------------------------------------------------- reboot

def reboot(host: dict) -> None:
    if host.get("os") == "windows":
        code, out = run_command(host, "shutdown /r /t 5 /c 'Restart requested by Jarvis'", timeout=30)
        if code not in (0, None):
            raise HousekeepingError(f"Windows refused the restart: {out.strip()[:200]}")
        return
    # Detached, so the SSH session can finish before the machine goes down.
    _run(host, "nohup sh -c 'sleep 2; systemctl reboot 2>/dev/null || reboot' >/dev/null 2>&1 &\necho '##OK yes'\nexit 0", timeout=60)
