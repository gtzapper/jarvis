import re
from pathlib import Path

import paramiko

from ..config import env
from ..models import Report, check_disk, check_usage, fmt_uptime, pct
from . import windows

KNOWN_HOSTS = Path("data/known_hosts")

# One round-trip per check: a read-only script whose output is split into "##section" blocks.
LINUX_SCRIPT = r"""
echo "##stat"; head -1 /proc/stat; sleep 1; head -1 /proc/stat
echo "##meminfo"; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "##load"; cat /proc/loadavg; nproc
echo "##uptime"; cat /proc/uptime
echo "##df"; df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs 2>/dev/null | tail -n +2
echo "##failed"; systemctl --failed --no-legend --plain 2>/dev/null
echo "##docker"; docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Status}}' 2>/dev/null
"""

UNRAID_SCRIPT = LINUX_SCRIPT + r"""
echo "##varlog"; df -P -B1 /var/log 2>/dev/null | tail -1
echo "##var"; cat /var/local/emhttp/var.ini 2>/dev/null
echo "##disks"; cat /var/local/emhttp/disks.ini 2>/dev/null
"""

# Unraid df shows every array disk plus some duplicates; these add noise without information.
UNRAID_SKIP_MOUNTS = re.compile(r"^/mnt/user0$|^/mnt/addons|^/etc/libvirt")


def sections(text: str) -> dict[str, list[str]]:
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None and line.strip():
            out[cur].append(line)
    return out


def parse_ini(lines: list[str]) -> dict[str, dict[str, str]]:
    """Parse Unraid's emhttp .ini files (quoted section names and values)."""
    out, out_root = {}, {}
    cur = out_root
    for line in lines:
        m = re.match(r'^\["?(.+?)"?\]$', line.strip())
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"')
    if out_root:
        out[""] = out_root
    return out


def auth_method(cfg: dict) -> str:
    """'key' or 'password'. Older configs without the setting: a password and no key file means password."""
    if cfg.get("auth") in ("key", "password"):
        return cfg["auth"]
    return "password" if cfg.get("password_env") and not cfg.get("key_file") else "key"


def connect(cfg: dict) -> paramiko.SSHClient:
    user = cfg.get("user", "root")
    if auth_method(cfg) == "password":
        password = env(cfg.get("password_env"))
        if not password:
            raise ValueError(f"{cfg['name']} logs in with a password, but none is saved. Enter it in Setup")
        login = {"password": password, "key_filename": None}
    else:
        key = Path(cfg.get("key_file") or "~/.ssh/jarvis_ed25519").expanduser()
        if not key.exists():
            raise ValueError(f"Jarvis's SSH key ({key}) doesn't exist yet. Create it in Setup > Jarvis's SSH key, "
                             "or switch this host to password login")
        login = {"password": None, "key_filename": str(key)}

    c = paramiko.SSHClient()
    KNOWN_HOSTS.parent.mkdir(exist_ok=True)
    if KNOWN_HOSTS.exists():
        c.load_host_keys(str(KNOWN_HOSTS))
    # Trust on first use: the key is saved and any later change raises BadHostKeyException.
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # Only the method chosen for this host: no stray keys from ~/.ssh or an SSH agent.
        c.connect(cfg["host"], port=cfg.get("port", 22), username=user, timeout=10,
                  look_for_keys=False, allow_agent=False, **login)
    except paramiko.AuthenticationException:
        c.close()
        if login["password"]:
            raise PermissionError(f"{cfg['host']} refused the login: wrong user ({user}) or password, "
                                  "or the machine doesn't allow password logins")
        raise PermissionError(f"{cfg['host']} refused the login for {user}: Jarvis's SSH key isn't in that user's "
                              "authorized keys (or use password login)")
    except paramiko.BadHostKeyException:
        c.close()
        raise PermissionError(f"{cfg['host']}'s SSH identity changed since Jarvis first connected. If you rebuilt that "
                              "machine, remove its line from data/known_hosts; otherwise something may be impersonating it")
    c.save_host_keys(str(KNOWN_HOSTS))
    return c


def run_command(cfg: dict, command: str, timeout: int = 120) -> tuple[int | None, str]:
    """Run one command and return (exit code, combined output). Exit code is None on timeout."""
    if cfg.get("os") == "windows":
        command = windows.ps_wrap(command)  # Windows hosts take PowerShell
    c = connect(cfg)
    try:
        _, stdout, _ = c.exec_command(command, timeout=timeout)
        stdout.channel.set_combine_stderr(True)
        try:
            out = stdout.read().decode(errors="replace")
            return stdout.channel.recv_exit_status(), out
        except TimeoutError:
            return None, f"(no answer after {timeout}s, gave up)"
    finally:
        c.close()


class SSHCollector:
    """Monitors a Linux box (VM, Unraid, bare metal) over SSH using only read-only commands."""

    def __init__(self, cfg: dict, th: dict, kind: str = "linux"):
        self.cfg, self.th, self.kind = cfg, th, kind
        self.name = cfg["name"]
        self.must_run_containers = set(cfg.get("must_run_containers", []))
        self.windows = cfg.get("os") == "windows"
        if self.windows:
            self.kind = "windows"
            self.win_state = windows.WindowsState()
            self.ignore_services = {s.lower() for s in cfg.get("ignore_services", [])}

    def _run(self, script: str) -> str:
        c = connect(self.cfg)
        try:
            stdin, stdout, _ = c.exec_command("sh -s", timeout=30)
            stdin.write(script)
            stdin.channel.shutdown_write()
            return stdout.read().decode(errors="replace")
        finally:
            c.close()

    def _collect_windows(self) -> list[Report]:
        check_updates = self.win_state.due()
        c = connect(self.cfg)
        try:
            _, stdout, _ = c.exec_command(windows.ps_wrap(windows.script(check_updates)), timeout=150 if check_updates else 45)
            output = stdout.read().decode(errors="replace")
        finally:
            c.close()
        rep = Report(self.name, "windows")
        rows = windows.report(rep, windows.parse(output), self.th, self.ignore_services, self.win_state, check_updates)
        self._services(rep, {"docker": rows})
        return [rep]

    def collect(self) -> list[Report]:
        if self.windows:
            return self._collect_windows()
        out = sections(self._run(UNRAID_SCRIPT if self.kind == "unraid" else LINUX_SCRIPT))
        rep = Report(self.name, self.kind)
        self._system(rep, out)
        self._disks(rep, out)
        self._services(rep, out)
        if self.kind == "unraid":
            self._unraid(rep, out)
        return [rep]

    def _system(self, rep: Report, out: dict) -> None:
        cpu = None
        stat = out.get("stat", [])
        if len(stat) == 2:
            a, b = ([int(x) for x in s.split()[1:9]] for s in stat)
            idle = (b[3] + b[4]) - (a[3] + a[4])
            total = sum(b) - sum(a)
            cpu = pct(total - idle, total)
        mi = {l.split(":")[0]: int(l.split()[1]) for l in out.get("meminfo", [])}
        mem = pct(mi.get("MemTotal", 0) - mi.get("MemAvailable", 0), mi.get("MemTotal", 0))
        load = out.get("load", ["0", "1"])
        load1, cores = float(load[0].split()[0]), int(load[1])
        uptime = float(out.get("uptime", ["0"])[0].split()[0])
        rep.metrics.update(cpu=cpu, mem=mem, load1=load1, cores=cores, uptime=uptime)
        rep.lines.append(f"CPU {cpu or 0:.0f}%  RAM {mem:.0f}%  load {load1:.2f} ({cores} cores)  up {fmt_uptime(uptime)}")
        check_usage(rep, self.th, cpu, mem, load1, cores)

    def _disks(self, rep: Report, out: dict) -> None:
        rows = out.get("df", []) + out.get("varlog", [])
        seen = set()
        for row in rows:
            parts = row.split()
            if len(parts) < 6:
                continue
            used, avail, mount = int(parts[2]), int(parts[3]), " ".join(parts[5:])
            if mount in seen or (self.kind == "unraid" and UNRAID_SKIP_MOUNTS.search(mount)):
                continue
            seen.add(mount)
            used_pct = pct(used, used + avail)
            rep.lines.append(f"Disk {mount}: {used_pct:.0f}% used")
            check_disk(rep, mount, used_pct, self.th)

    def _services(self, rep: Report, out: dict) -> None:
        for line in out.get("failed", []):
            unit = line.split()[0]
            rep.warn(f"systemd:{unit}", f"Service {unit} has failed on {rep.name}")

        containers = [l.split("\t") for l in out.get("docker", []) if l.count("\t") >= 2]
        if containers:
            up = sum(1 for _, state, _ in containers if state == "running")
            rep.lines.append(f"Containers: {up}/{len(containers)} running")
        for name, state, status in containers:
            if state == "restarting":
                rep.warn(f"docker:{name}", f"Container {name} on {rep.name} is stuck restarting")
            elif "unhealthy" in status:
                rep.warn(f"docker:{name}", f"Container {name} on {rep.name} is unhealthy")
            elif state != "running" and name in self.must_run_containers:
                rep.crit(f"docker:{name}", f"Container {name} on {rep.name} is {state}")

    def _unraid(self, rep: Report, out: dict) -> None:
        var = parse_ini(out.get("var", [])).get("", {})
        state = var.get("mdState", "UNKNOWN")
        rep.lines.append(f"Array: {state}")
        if state != "STARTED":
            rep.crit("array", f"Unraid array on {rep.name} is {state}")
        if var.get("mdResyncPos", "0") not in ("0", ""):
            size = int(var.get("mdResyncSize") or 0)
            done = pct(int(var["mdResyncPos"]), size)
            rep.lines.append(f"Parity check / rebuild in progress: {done:.0f}%")

        for slot, d in parse_ini(out.get("disks", [])).items():
            status = d.get("status", "")
            if status == "DISK_NP" or not slot:
                continue  # empty slot
            label = f"{slot} ({d.get('device') or 'missing'})"
            temp = d.get("temp", "*")
            line = f"Disk {label}: {status}"
            if temp.isdigit():
                line += f", {temp}C"
                if int(temp) >= self.th["disk_temp_c"]:
                    rep.warn(f"temp:{slot}", f"Unraid disk {slot} is running hot at {temp}C")
            else:
                line += ", spun down"
            rep.lines.append(line)
            if status != "DISK_OK":
                rep.crit(f"diskstatus:{slot}", f"Unraid disk {slot} status is {status}")
            errors = int(d.get("numErrors") or 0)
            if errors:
                rep.warn(f"diskerr:{slot}", f"Unraid disk {slot} has {errors} read/write errors")
